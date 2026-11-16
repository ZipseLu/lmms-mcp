"""MCP server exposing LMMS song creation and editing tools."""

from __future__ import annotations

import copy
import functools
import json
import math
import re
from pathlib import Path

try:  # mcp >= 2
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer
    from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__, catalog, dialect as dialects, lmms_env, midi_import, theory
from .project import Project, ProjectError, TICKS_PER_BEAT
from .theory import NoteEvent

INSTRUCTIONS = """\
Create and edit LMMS songs (.mmpz / .mmp project files). The server edits project files \
directly; LMMS itself is only needed to listen (open_in_lmms) or export audio (render_project).

WORKFLOW
1. new_project (or open_project)        4. add_notes / add_chords / set_beat
2. add_instrument_track, add_drum_kit    5. arrange: arrange_pattern, copy_section
3. list_presets / list_samples for sounds 6. mix: add_effect, mixer, automation -> save_project

CONVENTIONS
- bar: 1-based position on the song timeline (bar 1 = start). Fractions allowed.
- note start/length: beats (quarter notes); start is 0-based from the given bar.
- pitch: note names "C4", "F#3", "Bb2" (C4 = MIDI 60 = middle C) or MIDI numbers.
- velocity / volume: 0-200, 100 = normal. pan: -100 (left) .. 100 (right).
- tracks are referenced by name or index; mixer channels by number (0 = master).
- Drums live in the beat editor: add_drum_kit once, write patterns with set_beat \
("x...x...x...x..." = 16 sixteenth steps), then place them on the timeline with arrange_pattern.
- Melodic parts go on instrument tracks (add_notes / add_chords). Write a section once and \
repeat it with copy_section.
- Nothing is written to disk until save_project. undo reverts the last change.
- A good default sound palette without presets: tripleoscillator (+ set_envelope/set_filter), \
lb302 (acid bass), kicker (kicks), bitinvader (chiptune). With LMMS installed, list_presets \
offers hundreds of ready sounds (ZynAddSubFX pianos, strings, pads...).
"""

mcp = MCPServer("lmms", instructions=INSTRUCTIONS)

_INSTRUMENT_ALIASES = {
    "3osc": "tripleoscillator", "triple": "tripleoscillator", "tripleosc": "tripleoscillator",
    "afp": "audiofileprocessor", "sampler": "audiofileprocessor", "sample": "audiofileprocessor",
    "zyn": "zynaddsubfx", "zynaddsubfx": "zynaddsubfx", "sf2": "sf2player", "soundfont": "sf2player",
    "papu": "freeboy", "gameboy": "freeboy", "opl2": "opulenz", "vibed": "vibedstrings",
    "nescaline": "nes", "tb303": "lb302", "303": "lb302", "kick": "kicker", "vst": "vestige",
}
_GM_DRUMS = {35: "kick", 36: "kick", 37: "rim", 38: "snare", 39: "clap", 40: "snare",
             41: "tom", 43: "tom", 45: "tom", 47: "tom", 48: "tom", 50: "tom",
             42: "hihat", 44: "hihat", 46: "openhat", 49: "crash", 57: "crash",
             51: "ride", 59: "ride", 53: "ride", 70: "shaker", 82: "shaker"}


class _State:
    project: Project | None = None
    undo: list[tuple] = []


def _project() -> Project:
    if _State.project is None:
        raise ProjectError("No project is open. Call new_project or open_project first.")
    return _State.project


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def tool(*, mutates: bool = False, title: str | None = None):
    """Register an MCP tool. Mutating tools are undoable and atomic."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            project = _State.project
            snapshot = None
            if mutates and project is not None:
                snapshot = (project, copy.deepcopy(project.root), project.modified, project.path)
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                if snapshot is not None:  # leave the project exactly as it was
                    project.root, project.modified, project.path = snapshot[1], snapshot[2], snapshot[3]
                    _State.project = project
                if isinstance(exc, (ProjectError, ValueError, OSError)):
                    # expected: the message tells the model how to fix the call
                    raise ToolError(str(exc)) from exc
                raise ToolError(f"Internal error ({type(exc).__name__}: {exc}). "
                                f"The project was not changed.") from exc
            if snapshot is not None and _State.project is project:
                _State.undo.append(snapshot)
                del _State.undo[:-40]
            return _json(result)
        annotations = ToolAnnotations(title=title, readOnlyHint=not mutates, openWorldHint=False)
        return mcp.tool(annotations=annotations)(wrapper)
    return decorator


def _project_path(path: str) -> Path:
    p = Path(path.strip().strip('"')).expanduser()
    if p.suffix.lower() not in (".mmpz", ".mmp", ".mpt"):
        p = p.with_name(p.name + ".mmpz")
    if not p.is_absolute():
        p = lmms_env.projects_dir() / p
    return p


def _tracks(p: Project, refs) -> list | None:
    if refs is None:
        return None
    if isinstance(refs, (str, int)):
        refs = [refs]
    return [p.find_track(r) for r in refs]


def _events(notes, sequence, velocity: int, gate: float, start_beat: float = 0.0) -> list[NoteEvent]:
    events: list[NoteEvent] = []
    if sequence:
        events += theory.parse_sequence(sequence, start_beat, velocity, gate)[0]
    for item in notes or []:
        if isinstance(item, (list, tuple)):
            if not 2 <= len(item) <= 4:
                raise ProjectError("List notes must be [pitch, start, length] or [pitch, start, length, velocity]")
            item = dict(zip(("pitch", "start", "length", "velocity"), item))
        if not isinstance(item, dict):
            raise ProjectError(f"Invalid note {item!r}: use {{'pitch': 'C4', 'start': 0, 'length': 1}}")
        unknown = set(item) - {"pitch", "pitches", "start", "length", "velocity", "pan"}
        if unknown:
            raise ProjectError(f"Unknown note field(s) {sorted(unknown)}. Allowed: pitch, pitches, "
                               f"start, length, velocity, pan")
        pitches = item.get("pitches", item.get("pitch"))
        if pitches is None:
            raise ProjectError(f"Note {item!r} has no pitch")
        if not isinstance(pitches, (list, tuple)):
            pitches = [pitches]
        try:
            start = float(item.get("start", 0)) + start_beat
            length = theory.parse_duration(item.get("length", 1)) * gate
        except (TypeError, ValueError) as exc:
            raise ProjectError(f"Invalid note {item!r}: {exc}") from None
        if start < 0:
            raise ProjectError("Note start must not be negative")
        for pitch in pitches:
            events.append(NoteEvent(theory.parse_pitch(pitch), start, length,
                                    int(item.get("velocity", velocity)), int(item.get("pan", 0))))
    return events


def _configure_instrument(p: Project, track, *, instrument=None, preset=None, sample=None,
                          soundfont=None, patch=None, bank=None, params=None) -> dict:
    """Shared implementation of 'which sound does this track make'."""
    info: dict = {}
    warnings: list[str] = []
    name = None
    if instrument:
        key = instrument.strip().lower().replace(" ", "").replace("-", "")
        name = _INSTRUMENT_ALIASES.get(key, key)
        if name not in catalog.INSTRUMENTS and name != "vestige":
            raise ProjectError(f"Unknown instrument '{instrument}'. Available: "
                               f"{', '.join(sorted(catalog.INSTRUMENTS))} (see list_instruments)")
    if sample and soundfont:
        raise ProjectError("Give either sample or soundfont, not both")
    if sample:
        name = name or "audiofileprocessor"
        if name != "audiofileprocessor":
            raise ProjectError("'sample' needs the audiofileprocessor instrument")
    if soundfont:
        name = name or "sf2player"
        if name != "sf2player":
            raise ProjectError("'soundfont' needs the sf2player instrument")
    preset_path = None
    if preset:
        preset_instrument, preset_path = lmms_env.find_preset(preset, name)
        if name and preset_instrument and preset_instrument != name:
            raise ProjectError(f"Preset '{preset}' belongs to {preset_instrument}, not {name}")
        name = name or preset_instrument or None
    if name:
        spec = catalog.INSTRUMENTS.get(name, {})
        if spec.get("needs_preset") and not preset_path:
            raise ProjectError(f"{name} is silent without a preset. Pick one with "
                               f"list_presets(instrument='{name}') and pass preset=...")
        if spec.get("min_version") == "1.3" and not p.d.extended_keys:
            warnings.append(f"{name} requires LMMS 1.3 or newer")
        if not preset_path or name == "zynaddsubfx":
            p.set_instrument(track, name)
    if preset_path:
        info["preset"] = preset_path.stem
        p.apply_preset(track, preset_path)
    if sample:
        stored, _absolute, warning = lmms_env.resolve_sample(sample)
        p.set_instrument_params(track, {"src": stored})
        info["sample"] = stored
        if warning:
            warnings.append(warning)
    if soundfont:
        path = Path(soundfont.strip().strip('"')).expanduser()
        if not path.is_file():
            warnings.append(f"Soundfont not found on this machine: {path}")
        p.set_instrument_params(track, {"src": path.as_posix()})
    for key, value in (("patch", patch), ("bank", bank)):
        if value is not None:
            if p.instrument_name(track) != "sf2player":
                raise ProjectError("patch/bank only apply to the sf2player instrument")
            p.set_instrument_params(track, {key: int(value)})
    if params:
        warnings += p.set_instrument_params(track, params)
    info["instrument"] = p.instrument_name(track)
    if warnings:
        info["warnings"] = warnings
    return info


def _route(p: Project, track, mixer_channel, new_mixer_channel: bool) -> int | None:
    if new_mixer_channel:
        channel = p.add_channel(track.get("name", "Channel"))
        p.update_track(track, mixer_channel=channel)
        return channel
    if mixer_channel is not None:
        p.update_track(track, mixer_channel=int(mixer_channel))
        return int(mixer_channel)
    return None


def _beat_track(p: Project, ref):
    return p.find_track(ref, scope="beat")


# ---------------------------------------------------------------------------
# project
# ---------------------------------------------------------------------------

@tool(mutates=True, title="New project")
def new_project(name: str | None = None, tempo: float = 120, time_signature: str = "4/4",
                lmms_version: str = "auto") -> dict:
    """Start a new, empty LMMS song. Replaces the currently open project (save it first).

    Args:
        name: Song / file name. With a name, save_project() needs no path and writes
            "<projects folder>/<name>.mmpz".
        tempo: Beats per minute (10-999).
        time_signature: e.g. "4/4", "3/4", "6/8". Set it now - changing it later moves bar lines.
        lmms_version: File format: "1.2" (LMMS 1.2.x, also opens fine in 1.3), "1.3", or
            "auto" (matches the installed LMMS; 1.2 if LMMS is not found).
    """
    m = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", time_signature)
    if not m:
        raise ProjectError("time_signature must look like '4/4'")
    target = lmms_env.default_target() if lmms_version.strip().lower() == "auto" else lmms_version
    project = Project.new(dialects.for_target(target), tempo, int(m.group(1)), int(m.group(2)))
    if name:
        project.path = _project_path(name)
    _State.project = project
    _State.undo.clear()
    result = {"created": True, "lmms_format": project.d.label, "tempo": project.tempo,
              "time_signature": time_signature.strip(), "beats_per_bar": project.beats_per_bar,
              "will_save_to": str(project.path) if project.path else None}
    if project.path and project.path.exists():
        result["warning"] = f"{project.path.name} already exists and will be overwritten by save_project"
    return result


@tool(mutates=True, title="Open project")
def open_project(path: str) -> dict:
    """Open an existing LMMS project (.mmpz, .mmp) or template (.mpt) for editing.

    Args:
        path: Absolute path, or a path relative to the projects folder (see get_environment).
    """
    target = _project_path(path)
    if not target.is_file() and Path(path).expanduser().is_file():
        target = Path(path).expanduser()
    _State.project = Project.open(target)
    _State.undo.clear()
    return _State.project.summary()


@tool(mutates=True, title="Save project")
def save_project(path: str | None = None) -> dict:
    """Write the project to disk. Returns the file path and any problems found by validation.

    Args:
        path: Target file. ".mmpz" (compressed, default) or ".mmp" (plain XML). Relative paths
            go to the projects folder. Omit to save to the current file.
    """
    p = _project()
    saved = p.save(_project_path(path) if path else None)
    result = {"saved": str(saved), "length_bars": p.summary()["length_bars"]}
    issues = p.validate(lmms_env.sample_exists)
    if issues:
        result["issues"] = issues
    return result


@tool(title="Project overview")
def get_project(detail: bool = False) -> dict:
    """Overview of the open project: settings, tracks with their clips, beat patterns, mixer.

    Args:
        detail: Also include instrument parameters, filter and effect settings.
    """
    return _project().summary(detail)


@tool(mutates=True, title="Song settings")
def set_song(tempo: float | None = None, time_signature: str | None = None,
             master_volume: float | None = None, master_pitch: int | None = None,
             loop_start_bar: float | None = None, loop_end_bar: float | None = None,
             loop_enabled: bool | None = None, notes: str | None = None) -> dict:
    """Change global song settings. Only the given values are changed.

    Args:
        tempo: BPM (10-999).
        time_signature: e.g. "3/4". Existing clips keep their absolute positions.
        master_volume: 0-200 (100 = default).
        master_pitch: -12..12 semitones, transposes all instruments.
        loop_start_bar / loop_end_bar / loop_enabled: Loop markers of the song editor.
        notes: Free text stored in the project notes window.
    """
    p = _project()
    if tempo is not None:
        p.set_tempo(tempo)
    if time_signature is not None:
        m = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", time_signature)
        if not m:
            raise ProjectError("time_signature must look like '4/4'")
        p.set_time_signature(int(m.group(1)), int(m.group(2)))
    if master_volume is not None:
        p.set_master_volume(master_volume)
    if master_pitch is not None:
        p.set_master_pitch(master_pitch)
    if any(v is not None for v in (loop_start_bar, loop_end_bar, loop_enabled)):
        p.set_loop(loop_start_bar, loop_end_bar, loop_enabled)
    if notes is not None:
        p.set_notes_text(notes)
    s = p.summary()
    return {k: s[k] for k in ("tempo", "time_signature", "master_volume", "master_pitch")}


@tool(title="Undo")
def undo() -> dict:
    """Revert the last change made to the project (up to 40 steps)."""
    if not _State.undo:
        raise ProjectError("Nothing to undo")
    project, root, modified, path = _State.undo.pop()
    project.root, project.modified, project.path = root, modified, path
    _State.project = project
    return {"undone": True, "remaining_undo_steps": len(_State.undo)}


@tool(title="Validate project")
def validate_project() -> dict:
    """Check the project for problems (silent tracks, missing samples, unplaced patterns...)."""
    issues = _project().validate(lmms_env.sample_exists)
    return {"ok": not issues, "issues": issues}


# ---------------------------------------------------------------------------
# tracks and instruments
# ---------------------------------------------------------------------------

@tool(mutates=True, title="Add instrument track")
def add_instrument_track(name: str, instrument: str | None = None, preset: str | None = None,
                         sample: str | None = None, soundfont: str | None = None,
                         patch: int | None = None, bank: int | None = None,
                         params: dict | None = None, volume: float = 100, pan: float = 0,
                         mixer_channel: int | None = None, new_mixer_channel: bool = False) -> dict:
    """Add a melodic instrument track to the song (synth, sampler, soundfont).

    Choose the sound with ONE of: instrument (+ optional params), preset, sample, soundfont.
    Default is the tripleoscillator synth.

    Args:
        name: Track name, e.g. "Bass", "Lead", "Pad".
        instrument: Plugin name (list_instruments), e.g. "tripleoscillator", "lb302", "kicker",
            "bitinvader", "zynaddsubfx", "organic".
        preset: Preset name from list_presets, e.g. "TripleOscillator/SuperSaw" or just "Rhodes".
            The instrument is taken from the preset.
        sample: Audio file for the sample player: factory sample ("instruments/piano01.ogg",
            see list_samples) or absolute path. The sample's own pitch plays at A4.
        soundfont: Absolute path of a .sf2 file (uses sf2player); select the sound with patch/bank.
        patch, bank: SoundFont program (0-127) and bank.
        params: Instrument parameters (describe_plugin shows them), e.g. {"wavetype0": 2}.
        volume: 0-200. pan: -100..100.
        mixer_channel: Existing mixer channel number to route to (default 0 = master).
        new_mixer_channel: Create a dedicated mixer channel named like the track.
    """
    p = _project()
    track = p.add_instrument_track(name, volume, pan)
    info = _configure_instrument(p, track, instrument=instrument, preset=preset, sample=sample,
                                 soundfont=soundfont, patch=patch, bank=bank, params=params)
    channel = _route(p, track, mixer_channel, new_mixer_channel)
    return {"track": track.get("name"), "index": p.track_index(track), **info,
            "mixer_channel": channel or 0}


@tool(mutates=True, title="Change instrument")
def set_instrument(track: int | str, instrument: str | None = None, preset: str | None = None,
                   sample: str | None = None, soundfont: str | None = None,
                   patch: int | None = None, bank: int | None = None, params: dict | None = None,
                   beat_instrument: bool = False) -> dict:
    """Change the sound of an existing instrument track: swap the plugin, load a preset,
    replace the sample or tweak parameters (params alone keeps everything else).

    Args:
        track: Track name or index.
        instrument / preset / sample / soundfont / patch / bank / params: as in add_instrument_track.
        beat_instrument: True if `track` is an instrument of the beat editor (drum kit row).
    """
    p = _project()
    target = p.find_track(track, scope="beat" if beat_instrument else "song")
    if not any(v is not None for v in (instrument, preset, sample, soundfont, patch, bank, params)):
        raise ProjectError("Nothing to change")
    info = _configure_instrument(p, target, instrument=instrument, preset=preset, sample=sample,
                                 soundfont=soundfont, patch=patch, bank=bank, params=params)
    return {"track": target.get("name"), **info}


@tool(mutates=True, title="Update track")
def update_track(track: int | str, name: str | None = None, volume: float | None = None,
                 pan: float | None = None, muted: bool | None = None, solo: bool | None = None,
                 mixer_channel: int | None = None, pitch_cents: float | None = None,
                 base_note: str | None = None, color: str | None = None,
                 beat_instrument: bool = False) -> dict:
    """Change track settings. Only the given values are changed.

    Args:
        track: Track name or index.
        name: New name. volume: 0-200. pan: -100..100. muted / solo: booleans.
        mixer_channel: Mixer channel number to route the track to.
        pitch_cents: Detune the whole track in cents (100 = 1 semitone, 1200 = 1 octave).
        base_note: For samples: the pitch at which the sample plays unchanged (default "A4").
            E.g. "C4" if the sample is a recorded C4.
        color: "#rrggbb" track colour (LMMS 1.3), "" to reset.
        beat_instrument: True if `track` is an instrument of the beat editor.
    """
    p = _project()
    scope = "beat" if beat_instrument else "song"
    target = p.find_track(track, scope=scope)
    p.update_track(target, name=name, volume=volume, pan=pan, muted=muted, solo=solo,
                   mixer_channel=mixer_channel, pitch=pitch_cents, color=color,
                   base_note=theory.parse_pitch(base_note) if base_note is not None else None)
    return p.describe_track(target, p.track_index(target, scope), scope=scope)


@tool(mutates=True, title="Remove track")
def remove_track(track: int | str, beat_instrument: bool = False) -> dict:
    """Delete a track with all its clips. Removing a pattern track deletes that beat pattern.

    Args:
        track: Track name or index.
        beat_instrument: True to remove an instrument row of the beat editor instead.
    """
    p = _project()
    scope = "beat" if beat_instrument else "song"
    target = p.find_track(track, scope=scope)
    p.remove_track(target, scope=scope)
    return {"removed": target.get("name"), "tracks": [t.get("name") for t in p.tracks()]}


@tool(mutates=True, title="Duplicate track")
def duplicate_track(track: int | str, new_name: str | None = None, with_clips: bool = True) -> dict:
    """Copy an instrument/sample track including sound, effects and (optionally) its clips.
    Handy for layering (duplicate, then transpose_notes or change the instrument).

    Args:
        track: Track name or index. new_name: Name of the copy.
        with_clips: False copies only the sound, not the notes.
    """
    p = _project()
    clone = p.duplicate_track(p.find_track(track), new_name)
    if not with_clips:
        p.remove_clips(clone)
    return {"track": clone.get("name"), "index": p.track_index(clone)}


@tool(mutates=True, title="Reorder track")
def move_track(track: int | str, new_index: int) -> dict:
    """Move a track to another position in the song editor (purely visual order).

    Args:
        track: Track name or index. new_index: 0-based target position.
    """
    p = _project()
    p.move_track(p.find_track(track), new_index)
    return {"tracks": [t.get("name") for t in p.tracks()]}


@tool(title="Track details")
def get_track(track: int | str, beat_instrument: bool = False) -> dict:
    """Full details of one track: instrument parameters, filter, effects, clips.

    Args:
        track: Track name or index. beat_instrument: True for a beat editor instrument.
    """
    p = _project()
    scope = "beat" if beat_instrument else "song"
    target = p.find_track(track, scope=scope)
    return p.describe_track(target, p.track_index(target, scope), scope=scope, detail=True)


@tool(mutates=True, title="Envelope / LFO")
def set_envelope(track: int | str, target: str = "volume", attack: float | None = None,
                 hold: float | None = None, decay: float | None = None,
                 sustain: float | None = None, release: float | None = None,
                 amount: float | None = None, lfo_amount: float | None = None,
                 lfo_speed_hz: float | None = None, lfo_wave: str | None = None,
                 beat_instrument: bool = False) -> dict:
    """Shape a track's sound over time with an ADSR envelope and/or LFO.

    target "volume" shapes loudness - this is how you make plucks (attack 0, hold 0,
    decay 0.3, sustain 0), pads (attack 0.8, sustain 1, release 1.5) or stabs.
    target "cutoff"/"resonance" modulates the track filter (see set_filter); amount sets depth.

    Args:
        track: Track name or index.
        target: "volume", "cutoff" or "resonance".
        attack, hold, decay, release: Seconds (0-20). sustain: level 0-1.
        amount: Envelope depth -1..1 (set to 1 automatically when you give ADSR values).
        lfo_amount: LFO depth -1..1 (tremolo on volume, wobble on cutoff).
        lfo_speed_hz: LFO frequency in Hz. lfo_wave: sine, triangle, saw, square, random.
        beat_instrument: True if `track` is a beat editor instrument.
    """
    p = _project()
    t = p.find_track(track, scope="beat" if beat_instrument else "song")
    values = p.set_envelope(t, target, attack=attack, hold=hold, decay=decay, sustain=sustain,
                            release=release, amount=amount, lfo_amount=lfo_amount,
                            lfo_speed_hz=lfo_speed_hz, lfo_wave=lfo_wave)
    keys = ("att", "hold", "dec", "sustain", "rel", "amt", "lamt", "lspd", "lshp")
    return {"track": t.get("name"), "target": target, "stored": {k: values.get(k) for k in keys}}


@tool(mutates=True, title="Track filter")
def set_filter(track: int | str, filter_type: str | None = None, cutoff: float | None = None,
               resonance: float | None = None, enabled: bool | None = None,
               beat_instrument: bool = False) -> dict:
    """Set the per-note filter of an instrument track (enabled automatically).

    Args:
        track: Track name or index.
        filter_type: lowpass, highpass, bandpass, notch, moog, sv_lowpass, rc_lowpass_24,
            vocal_formant, ... (describe_plugin("filters") lists all).
        cutoff: 1-14000 Hz. resonance: 0.01-10 (0.5 neutral, >2 clearly resonant).
        enabled: Switch the filter on/off.
        beat_instrument: True if `track` is a beat editor instrument.
    """
    p = _project()
    t = p.find_track(track, scope="beat" if beat_instrument else "song")
    values = p.set_filter(t, enabled=enabled, filter_type=filter_type, cutoff=cutoff, resonance=resonance)
    return {"track": t.get("name"), "enabled": values.get("fwet") == "1",
            "type": catalog.FILTER_TYPES[int(float(values.get("ftype", 0)))],
            "cutoff": values.get("fcut"), "resonance": values.get("fres")}


@tool(mutates=True, title="Arpeggiator")
def set_arpeggiator(track: int | str, enabled: bool = True, chord: str | None = None,
                    direction: str | None = None, octaves: int | None = None,
                    time_ms: float | None = None, gate: float | None = None) -> dict:
    """Turn held notes into arpeggios (LMMS instrument function).

    Args:
        track: Track name or index. enabled: on/off.
        chord: Notes the arp cycles through for each played note: "octave", "major", "minor",
            "m7", "Maj7", "sus4", "5", ... (LMMS chord table).
        direction: up, down, up_down, down_up, random. octaves: range 1-9.
        time_ms: Milliseconds per step (60000 / tempo / 4 = one sixteenth). gate: 1-200 %.
    """
    p = _project()
    t = p.find_track(track)
    values = p.set_arpeggiator(t, enabled=enabled, chord=chord, direction=direction, octaves=octaves,
                               time_ms=time_ms, gate=gate)
    return {"track": t.get("name"), "enabled": enabled,
            "chord": catalog.CHORD_TABLE[int(float(values.get("arp", 0)))],
            "time_ms": values.get("arptime")}


@tool(mutates=True, title="Chord stacking")
def set_chord_stacking(track: int | str, enabled: bool = True, chord: str | None = None,
                       octaves: int | None = None) -> dict:
    """Play a whole chord for every single note (LMMS "stacking"). Useful for power chords
    ("5"), octave doubling ("octave") or one-finger pads.

    Args:
        track: Track name or index. enabled: on/off.
        chord: "octave", "5", "major", "minor", "m7", ... octaves: range 1-9.
    """
    p = _project()
    t = p.find_track(track)
    values = p.set_chord_stacking(t, enabled=enabled, chord=chord, octaves=octaves)
    return {"track": t.get("name"), "enabled": enabled,
            "chord": catalog.CHORD_TABLE[int(float(values.get("chord", 0)))]}


# ---------------------------------------------------------------------------
# notes
# ---------------------------------------------------------------------------

@tool(mutates=True, title="Add notes")
def add_notes(track: int | str, notes: list | None = None, sequence: str | None = None,
              bar: float = 1, velocity: int = 100, gate: float = 1.0, replace: bool = False,
              pattern: int | str | None = None, clip_name: str | None = None) -> dict:
    """Write notes to an instrument track, starting at `bar`. Use `sequence` for melodies and
    basslines, `notes` for anything with overlapping or freely placed notes.

    The notes go into the clip at that bar (created or extended automatically), so call this
    once per section, e.g. bar=1 for the intro riff and bar=9 for the verse.

    Args:
        track: Track name or index.
        sequence: Notes played one after another, space separated: "C4:1 E4:0.5 G4:0.5 r:1
            [C4 E4 G4]:2". Format pitch:beats; r = rest; [..] = chord; a note without duration
            reuses the previous one. Durations may be numbers (1, 0.5, 1/3) or w h q e s
            (whole..sixteenth, "q." dotted). "@110" sets one note's velocity; "|" is ignored.
        notes: List of {"pitch": "C4", "start": 0, "length": 1, "velocity": 100, "pan": 0};
            "pitches": ["C4","E4"] makes a chord. start/length in beats, start counted from
            `bar`. Short form: ["C4", 0, 1] or ["C4", 0, 1, 90].
        bar: Song position (1-based) where beat 0 of these notes lies.
        velocity: Default velocity 0-200 (100 = normal).
        gate: Fraction of each duration that sounds (0.5 = staccato, 1 = legato).
        replace: Remove existing notes in the bars being written first.
        pattern: Write into a beat-editor pattern instead (name or index); `track` is then a
            beat instrument and `bar` is ignored. Use for basslines that loop with the drums.
        clip_name: Optional label shown on the clip, e.g. "Verse".
    """
    p = _project()
    if not notes and not sequence:
        raise ProjectError("Give notes or a sequence")
    if not 0 < gate <= 1:
        raise ProjectError("gate must be in (0, 1]")
    events = _events(notes, sequence, velocity, gate)
    if pattern is not None:
        t = _beat_track(p, track)
        result = p.add_beat_notes(p.find_pattern(pattern), t, events, replace)
    else:
        t = p.find_track(track)
        result = p.add_notes(t, events, bar, replace, clip_name)
    end = max(e.start + e.length for e in events)
    low, high = min(e.pitch for e in events), max(e.pitch for e in events)
    return {"track": t.get("name"), **result, "ends_at_beat": round(end, 3),
            "range": f"{theory.pitch_name(low)}-{theory.pitch_name(high)}"}


@tool(mutates=True, title="Add chord progression")
def add_chords(track: int | str, chords: str | list, bar: float = 1, beats_per_chord: float = 4,
               octave: int = 3, style: str = "block", rate: float = 0.5,
               rhythm: str | None = None, voicing: str = "smooth", velocity: int = 90,
               gate: float = 0.95, replace: bool = False) -> dict:
    """Write a chord progression to an instrument track from chord symbols.

    Args:
        track: Track name or index.
        chords: Chord symbols separated by spaces, e.g. "Am F C G" or "Dm7 G7 Cmaj7 Cmaj7".
            Qualities: m, 7, maj7, m7, dim, aug, sus2, sus4, 6, 9, add9, 5, m7b5, slash bass
            (C/E). "N.C." is a silent slot. A list entry may carry its own length:
            ["Am", ["F", 2], ["G", 2]].
        bar: Song position (1-based) of the first chord.
        beats_per_chord: Length of each chord in beats (4 = one bar in 4/4).
        octave: Octave of the chord roots (3 = around C3; use 2 for bass-heavy pads, 4 for bright).
        style: "block" (held chords), "arp_up", "arp_down", "arp_updown" (broken chords),
            "strum" (block with a slight roll), "root" (root notes only - a simple bassline).
        rate: Beats per arpeggio note (0.5 = eighths, 0.25 = sixteenths).
        rhythm: For block style: step pattern per bar on a sixteenth grid, e.g. "x..x..x.x..x..x."
            re-triggers the chord on every x. Without it the chord is held.
        voicing: "smooth" picks inversions that move as little as possible, "close" keeps root position.
        velocity: 0-200. gate: sounding fraction of each note. replace: clear the bars first.
    """
    p = _project()
    t = p.find_track(track)
    items = chords.replace("|", " ").split() if isinstance(chords, str) else list(chords)
    if not items:
        raise ProjectError("No chords given")
    styles = ("block", "arp_up", "arp_down", "arp_updown", "strum", "root")
    if style not in styles:
        raise ProjectError(f"style must be one of {styles}")
    if rate <= 0 or beats_per_chord <= 0:
        raise ProjectError("rate and beats_per_chord must be positive")
    hits = theory.parse_steps(rhythm) if rhythm else None
    events: list[NoteEvent] = []
    described = []
    position, previous = 0.0, None
    for item in items:
        symbol, length = (item[0], float(item[1])) if isinstance(item, (list, tuple)) else (item, beats_per_chord)
        tones = theory.parse_chord(str(symbol), octave)
        if tones and voicing == "smooth" and "/" not in str(symbol):
            tones = theory.smooth_voicing(tones, previous)
        if tones:
            previous = tones
        if style == "root" and tones:
            root = theory.parse_chord(str(symbol).split("/")[-1] if "/" in str(symbol) else str(symbol), octave)[0]
            tones = [root]
        if not tones:
            pass
        elif style in ("block", "strum", "root"):
            starts = [(0.0, length)]
            if hits:
                on = [i * 0.25 for i in range(int(length * 4)) if hits[i % len(hits)]]
                starts = [(s, (on[k + 1] if k + 1 < len(on) else length) - s) for k, s in enumerate(on)]
            for start, dur in starts:
                for n, tone in enumerate(tones):
                    delay = 0.03 * n if style == "strum" else 0.0
                    events.append(NoteEvent(tone, position + start + delay,
                                            max(dur * gate - delay, 0.05), velocity))
        else:
            order = {"arp_up": tones, "arp_down": tones[::-1],
                     "arp_updown": tones + tones[-2:0:-1]}[style]
            for step in range(int(math.ceil(length / rate - 1e-9))):
                events.append(NoteEvent(order[step % len(order)], position + step * rate,
                                        min(rate, length - step * rate) * gate, velocity))
        described.append({"chord": str(symbol), "beat": position,
                          "notes": [theory.pitch_name(n) for n in tones]})
        position += length
    result = p.add_notes(t, events, bar, replace, length_bars=position / p.beats_per_bar)
    return {"track": t.get("name"), **result, "total_beats": position, "chords": described}


@tool(title="Read notes")
def get_notes(track: int | str, from_bar: float | None = None, length_bars: float | None = None,
              pattern: int | str | None = None) -> dict:
    """Read back the notes of a track, clip by clip (start/length in beats from the clip start).

    Args:
        track: Track name or index.
        from_bar, length_bars: Restrict to a bar range of the song.
        pattern: Read a beat-editor pattern instead; `track` is then a beat instrument.
    """
    p = _project()
    if pattern is not None:
        t = _beat_track(p, track)
        clips = p.get_notes(t, scope="beat", pattern=p.find_pattern(pattern))
    else:
        t = p.find_track(track)
        clips = p.get_notes(t, from_bar, length_bars)
    return {"track": t.get("name"), "clips": clips}


@tool(mutates=True, title="Remove notes")
def remove_notes(track: int | str, from_bar: float | None = None,
                 length_bars: float | None = None) -> dict:
    """Delete notes of an instrument track, in a bar range or (without range) all of them.

    Args:
        track: Track name or index. from_bar, length_bars: Range to clear.
    """
    p = _project()
    t = p.find_track(track)
    return {"track": t.get("name"), "notes_removed": p.remove_notes(t, from_bar, length_bars)}


@tool(mutates=True, title="Transpose notes")
def transpose_notes(track: int | str, semitones: int, from_bar: float | None = None,
                    length_bars: float | None = None) -> dict:
    """Shift notes of a track up or down (12 = one octave), optionally only in a bar range.

    Args:
        track: Track name or index. semitones: Positive = up.
        from_bar, length_bars: Range to transpose (default: whole track).
    """
    p = _project()
    t = p.find_track(track)
    return {"track": t.get("name"), "notes_transposed": p.transpose(t, semitones, from_bar, length_bars)}


# ---------------------------------------------------------------------------
# drums / beat editor
# ---------------------------------------------------------------------------

@tool(mutates=True, title="Add drum kit")
def add_drum_kit(kit: str = "standard", pieces: list[str] | None = None,
                 new_mixer_channel: bool = True) -> dict:
    """Add a set of drum instruments to the beat editor, ready for set_beat.

    Args:
        kit: "standard", "acoustic", "hiphop", "electro" or "hard" (LMMS factory samples).
        pieces: Which drums to add, e.g. ["kick", "snare", "hihat"]. Default: kick, snare,
            hihat, openhat, clap (where the kit has them). describe_plugin("drumkits") lists
            all pieces per kit.
        new_mixer_channel: Route all drums to one new "Drums" mixer channel (good for
            compressing / levelling the kit as a whole).
    """
    p = _project()
    key = kit.strip().lower()
    if key not in catalog.DRUM_KITS:
        raise ProjectError(f"Unknown kit '{kit}'. Available: {', '.join(catalog.DRUM_KITS)}")
    samples = catalog.DRUM_KITS[key]
    wanted = pieces or [n for n in ("kick", "snare", "hihat", "openhat", "clap") if n in samples]
    unknown = [n for n in wanted if n not in samples]
    if unknown:
        raise ProjectError(f"Kit '{key}' has no {unknown}. It offers: {', '.join(samples)}")
    channel = p.add_channel("Drums") if new_mixer_channel else 0
    added, warnings = [], []
    for piece in wanted:
        track = p.add_instrument_track(piece.replace("_", " ").title(), mixer_channel=channel, scope="beat")
        info = _configure_instrument(p, track, sample=samples[piece])
        warnings += info.get("warnings", [])
        added.append(track.get("name"))
    result = {"beat_instruments": added, "mixer_channel": channel,
              "patterns": [t.get("name") for t in p._pattern_tracks()],
              "next": "set_beat(pattern, steps={...}) then arrange_pattern(pattern, bar, length_bars)"}
    if warnings:
        result["warnings"] = sorted(set(warnings))
    return result


@tool(mutates=True, title="Add beat instrument")
def add_beat_instrument(name: str, instrument: str | None = None, preset: str | None = None,
                        sample: str | None = None, params: dict | None = None,
                        volume: float = 100, pan: float = 0,
                        mixer_channel: int | None = None) -> dict:
    """Add one instrument row to the beat editor: an extra drum sample, a synthesized kick
    (instrument="kicker") or a bass synth for looping basslines. It is shared by all patterns.

    Args:
        name: Row name, e.g. "Cowbell", "808", "Bass".
        instrument / preset / sample / params: Sound, as in add_instrument_track.
        volume: 0-200. pan: -100..100. mixer_channel: Existing channel to route to.
    """
    p = _project()
    track = p.add_instrument_track(name, volume, pan, scope="beat")
    info = _configure_instrument(p, track, instrument=instrument, preset=preset, sample=sample, params=params)
    _route(p, track, mixer_channel, False)
    return {"beat_instrument": track.get("name"), **info,
            "patterns": [t.get("name") for t in p._pattern_tracks()]}


@tool(mutates=True, title="Write beat pattern")
def set_beat(pattern: int | str, steps: dict[str, str], pitches: dict[str, str] | None = None) -> dict:
    """Write a drum pattern with step-sequencer strings. Creates the pattern if the name is new.

    Each string is one row: one character per sixteenth note, 16 characters = one bar in 4/4
    (32 = two bars...). x = hit, X = accent, o = soft ghost note, . = rest. Spaces and | are
    ignored, so "x...|x...|x...|x..." is fine. A given row replaces that instrument's steps
    in this pattern; rows you do not mention stay untouched.

    Example (house): {"Kick": "x...x...x...x...", "Clap": "....x.......x...",
                      "Hihat": "..x...x...x...x.", "Openhat": "..............x."}

    Args:
        pattern: Pattern name (e.g. "Verse beat") or index. New names create a new pattern.
        steps: {beat instrument name: step string}.
        pitches: Optional {instrument: pitch} to play a row at another pitch than the sample's
            own (A4), e.g. {"Tom": "E4"} for a lower tom or a note for a synth row.
    """
    p = _project()
    if not steps:
        raise ProjectError("steps is empty")
    rows = [(_beat_track(p, name), theory.parse_steps(text)) for name, text in steps.items()] \
        if p.beat_tracks() else None
    if rows is None:
        raise ProjectError("The beat editor has no instruments yet. Call add_drum_kit or "
                           "add_beat_instrument first.")
    pitch_map = {_beat_track(p, k).get("name"): theory.parse_pitch(v) for k, v in (pitches or {}).items()}
    try:
        index = p.find_pattern(pattern)
    except ProjectError:
        if isinstance(pattern, int) or str(pattern).strip().isdigit():
            raise
        existing = p._pattern_tracks()
        if len(existing) == 1 and existing[0].get("name") == "Beat 1" and not p._clips(existing[0]) \
                and not any(c.findall("note") for t in p.beat_tracks() for c in p._clips(t)):
            existing[0].set("name", str(pattern))  # reuse the untouched default pattern
            index = 0
        else:
            index = p.add_pattern(str(pattern))
    for track, velocities in rows:
        p.set_steps(index, track, velocities, pitch_map.get(track.get("name")))
    name = p._pattern_tracks()[index].get("name")
    return {"pattern": name, "index": index, "length_bars": p.pattern_length_bars(index),
            "rows_written": [t.get("name") for t, _v in rows],
            "placed_in_song": bool(p._clips(p._pattern_tracks()[index]))}


@tool(mutates=True, title="Place pattern in song")
def arrange_pattern(pattern: int | str, bar: float, length_bars: float | None = None) -> dict:
    """Put a beat pattern on the song timeline. The pattern loops for the whole length, so one
    call covers a full section (e.g. bar=5, length_bars=16).

    Args:
        pattern: Pattern name or index. bar: Start bar (1-based).
        length_bars: How long it plays (default: one pass of the pattern).
    """
    p = _project()
    index = p.find_pattern(pattern)
    length = length_bars if length_bars is not None else p.pattern_length_bars(index)
    p.place_pattern(index, bar, length)
    track = p._pattern_tracks()[index]
    return {"pattern": track.get("name"), "bar": bar, "length_bars": length,
            "placements": [{"bar": p.tick_to_bar(int(c.get("pos"))),
                            "length_bars": int(c.get("len")) / p.tpb} for c in p._clips(track)]}


@tool(mutates=True, title="Duplicate pattern")
def duplicate_pattern(pattern: int | str, new_name: str) -> dict:
    """Copy a beat pattern under a new name - the usual way to make a variation or fill
    (duplicate, then set_beat only the rows that differ).

    Args:
        pattern: Source pattern name or index. new_name: Name of the copy.
    """
    p = _project()
    index = p.duplicate_pattern(p.find_pattern(pattern), new_name)
    return {"pattern": new_name, "index": index}


# ---------------------------------------------------------------------------
# arrangement
# ---------------------------------------------------------------------------

@tool(mutates=True, title="Copy section")
def copy_section(from_bar: float, length_bars: float, to_bar: float, times: int = 1,
                 tracks: list | None = None) -> dict:
    """Repeat part of the arrangement: copies every clip that starts inside the source section
    (notes, pattern placements, samples, automation) to `to_bar`, `times` times in a row.

    Example: an 8-bar verse at bar 5 repeated after the chorus: from_bar=5, length_bars=8, to_bar=21.

    Args:
        from_bar: First bar of the source section. length_bars: Its length.
        to_bar: Where the first copy starts. times: Number of back-to-back copies.
        tracks: Limit to these tracks (names or indexes). Default: all tracks.
    """
    p = _project()
    copied = p.copy_section(from_bar, length_bars, to_bar, times, _tracks(p, tracks))
    result = {"clips_copied": copied, "song_length_bars": p.summary()["length_bars"]}
    if copied == 0:
        result["warning"] = "No clip starts inside the source section - nothing was copied"
    return result


@tool(mutates=True, title="Clear section")
def clear_section(from_bar: float, length_bars: float | None = None,
                  tracks: list | None = None) -> dict:
    """Remove all clips that start inside a bar range (the range to the song end if no length).

    Args:
        from_bar: First bar. length_bars: Range length.
        tracks: Limit to these tracks (names or indexes). Default: all tracks.
    """
    p = _project()
    removed = sum(p.remove_clips(t, from_bar, length_bars) for t in (_tracks(p, tracks) or p.tracks()))
    return {"clips_removed": removed, "song_length_bars": p.summary()["length_bars"]}


# ---------------------------------------------------------------------------
# audio clips
# ---------------------------------------------------------------------------

@tool(mutates=True, title="Add sample track")
def add_sample_track(name: str, volume: float = 100, pan: float = 0,
                     mixer_channel: int | None = None) -> dict:
    """Add an audio track for placing audio files (loops, vocals, FX) directly on the timeline.
    For playing a sample as notes use add_instrument_track(sample=...) instead.

    Args:
        name: Track name. volume: 0-200. pan: -100..100. mixer_channel: Channel number.
    """
    p = _project()
    track = p.add_sample_track(name, volume, pan, mixer_channel or 0)
    return {"track": track.get("name"), "index": p.track_index(track)}


@tool(mutates=True, title="Place audio clip")
def add_sample_clip(track: int | str, file: str, bar: float, length_beats: float | None = None) -> dict:
    """Place an audio file on a sample track.

    Args:
        track: Sample track name or index.
        file: Factory sample ("beats/break01.ogg" - see list_samples) or absolute path.
        bar: Start bar (1-based).
        length_beats: Clip length in beats. Default: the file's real duration at the current
            tempo (wav/ogg/flac; other formats need this value).
    """
    p = _project()
    t = p.find_track(track)
    stored, absolute, warning = lmms_env.resolve_sample(file)
    if length_beats is None:
        seconds = lmms_env.audio_duration(absolute) if absolute else None
        if seconds is None:
            raise ProjectError("Cannot determine the length of that file - pass length_beats")
        length_beats = seconds * p.tempo / 60
    p.add_sample_clip(t, stored, bar, int(round(length_beats * TICKS_PER_BEAT)))
    result = {"track": t.get("name"), "file": stored, "bar": bar, "length_beats": round(length_beats, 3)}
    if warning:
        result["warning"] = warning
    return result


# ---------------------------------------------------------------------------
# mixer and effects
# ---------------------------------------------------------------------------

@tool(mutates=True, title="Add mixer channel")
def add_mixer_channel(name: str, volume: float = 100, tracks: list | None = None) -> dict:
    """Create a mixer channel (routed to master) and optionally route tracks to it.

    Args:
        name: Channel name, e.g. "Drums", "Reverb bus".
        volume: 0-200 (100 = 0 dB).
        tracks: Tracks (names or indexes) to route to the new channel.
    """
    p = _project()
    channel = p.add_channel(name, volume)
    routed = []
    for t in _tracks(p, tracks) or []:
        p.update_track(t, mixer_channel=channel)
        routed.append(t.get("name"))
    return {"mixer_channel": channel, "name": name, "routed_tracks": routed}


@tool(mutates=True, title="Update mixer channel")
def update_mixer_channel(mixer_channel: int, name: str | None = None, volume: float | None = None,
                         muted: bool | None = None, send_to: int | None = None,
                         send_amount: float | None = None, remove: bool = False) -> dict:
    """Change, re-route or remove a mixer channel.

    Args:
        mixer_channel: Channel number (0 = master).
        name: New name. volume: 0-200 (100 = 0 dB). muted: boolean.
        send_to + send_amount: Send this channel's signal to another channel at 0-100 %
            (0 removes the send). E.g. send a lead to a "Reverb bus" channel at 30.
        remove: Delete the channel; its tracks fall back to master and higher channels shift down.
    """
    p = _project()
    if remove:
        p.remove_channel(mixer_channel)
        return {"removed": mixer_channel, "mixer": p.summary()["mixer"]}
    p.update_channel(mixer_channel, name=name, volume=volume, muted=muted)
    if send_to is not None:
        p.set_send(mixer_channel, send_to, 100 if send_amount is None else send_amount)
    return next(c for c in p.summary()["mixer"] if c["channel"] == mixer_channel)


def _chain(p: Project, track, mixer_channel):
    if track is not None and mixer_channel is not None:
        raise ProjectError("Give either track or mixer_channel, not both")
    if track is not None:
        return p.chain(track=p.find_track(track))
    return p.chain(channel=0 if mixer_channel is None else mixer_channel)


@tool(mutates=True, title="Add effect")
def add_effect(effect: str, track: int | str | None = None, mixer_channel: int | None = None,
               params: dict | None = None, wet: float = 1.0, position: int | None = None) -> dict:
    """Insert an effect on a track or a mixer channel (neither given = master channel).

    Common choices: reverbsc (wet 0.2-0.4), delay, eq, bassbooster, stereoenhancer, flanger,
    bitcrush, dualfilter, amplifier, waveshaper; compressor (LMMS 1.3 only).

    Args:
        effect: Effect name (list_effects).
        track: Track name/index to put the effect on, OR
        mixer_channel: Mixer channel number (0 = master).
        params: Effect parameters, e.g. {"size": 0.9, "color": 8000} (describe_plugin shows them).
        wet: Dry/wet mix 0-1. position: Slot in the chain (default: end).
    """
    p = _project()
    chain = _chain(p, track, mixer_channel)
    index, warnings = p.add_effect(chain, effect, params, wet, position)
    result = {"effect": effect.lower(), "index": index, "chain": p.describe_chain(chain)}
    if warnings:
        result["warnings"] = warnings
    return result


@tool(mutates=True, title="Update or remove effect")
def update_effect(effect_index: int, track: int | str | None = None,
                  mixer_channel: int | None = None, params: dict | None = None,
                  wet: float | None = None, enabled: bool | None = None, remove: bool = False) -> dict:
    """Change the parameters / mix of an existing effect, bypass it, or remove it.

    Args:
        effect_index: Position in the chain (see get_track / get_project).
        track or mixer_channel: Where the chain lives (neither = master).
        params: Parameters to change. wet: 0-1. enabled: False bypasses. remove: delete it.
    """
    p = _project()
    chain = _chain(p, track, mixer_channel)
    warnings = []
    if remove:
        p.remove_effect(chain, effect_index)
    else:
        warnings = p.update_effect(chain, effect_index, params=params, wet=wet, enabled=enabled)
    result = {"chain": p.describe_chain(chain)}
    if warnings:
        result["warnings"] = warnings
    return result


# ---------------------------------------------------------------------------
# automation
# ---------------------------------------------------------------------------

def _parameter(p: Project, parameter, track, mixer_channel, effect_index, beat_instrument=False):
    if track is not None and mixer_channel is not None:
        raise ProjectError("Give either track or mixer_channel, not both")
    t = p.find_track(track, scope="beat" if beat_instrument else "song") if track is not None else None
    return p.resolve_parameter(parameter, track=t, channel=mixer_channel, effect_index=effect_index)


@tool(mutates=True, title="Automate parameter")
def add_automation(parameter: str, points: list, track: int | str | None = None,
                   mixer_channel: int | None = None, effect_index: int | None = None,
                   progression: str = "linear") -> dict:
    """Make a value change over time (fades, filter sweeps, tempo changes, build-ups).

    What `parameter` means depends on the other arguments:
      - nothing else:            "tempo", "master_volume", "master_pitch"
      - track:                   "volume" (0-200), "pan", "pitch" (cents), "cutoff" (Hz),
                                 "resonance", or an instrument parameter (e.g. "vcf_cut" for lb302)
      - mixer_channel:           "volume" (0-200)
      - effect_index (+ track or mixer_channel): an effect parameter or "wet"

    Args:
        parameter: See above.
        points: [{"bar": 1, "value": 0}, {"bar": 5, "value": 100}] or [[1, 0], [5, 100]].
            bar is the 1-based song position (fractions allowed), value is in the parameter's
            own unit. Before the first point the normal value applies; after the last point
            the last value is held.
        track / mixer_channel / effect_index: Select the target (see above).
        progression: "linear" (ramps), "discrete" (steps) or "smooth" (curves).
    """
    p = _project()
    parsed = []
    for point in points:
        try:
            if isinstance(point, dict):
                parsed.append((float(point["bar"]), float(point["value"])))
            else:
                parsed.append((float(point[0]), float(point[1])))
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProjectError(f"Invalid point {point!r}: use {{'bar': 1, 'value': 100}}") from None
    if any(bar < 1 for bar, _v in parsed):
        raise ProjectError("Automation bars are numbered from 1")
    element, attr, label, scale = _parameter(p, parameter, track, mixer_channel, effect_index)
    return p.add_automation(element, attr, label, parsed, progression, scale)


@tool(mutates=True, title="Add LFO to parameter")
def add_lfo(parameter: str, track: int | str | None = None, mixer_channel: int | None = None,
            effect_index: int | None = None, period_seconds: float = 2.0, amount: float = 0.5,
            base: float = 0.5, wave: str = "sine", controller: int | None = None) -> dict:
    """Modulate a parameter continuously with an LFO controller (wobble bass, auto-pan,
    slow filter movement). The LFO sweeps the parameter's full range: 0 = minimum, 1 = maximum.

    Args:
        parameter, track, mixer_channel, effect_index: Target, as in add_automation.
        period_seconds: Length of one LFO cycle, 0.01-20 (one beat = 60/tempo seconds).
        amount: Depth -1..1 as a fraction of the parameter range.
        base: Centre of the modulation, 0-1 of the parameter range.
        wave: sine, triangle, saw, square, moog_saw, exponential, noise.
        controller: Reuse an existing controller index instead of creating a new LFO.
    """
    p = _project()
    element, attr, label, _scale = _parameter(p, parameter, track, mixer_channel, effect_index)
    if controller is None:
        controller = p.add_lfo_controller(f"LFO {label}", speed=period_seconds, amount=amount,
                                          base=base, wave=wave)
    p.connect_controller(element, attr, controller)
    return {"controller": controller, "target": label}


# ---------------------------------------------------------------------------
# library
# ---------------------------------------------------------------------------

@tool(title="List instruments")
def list_instruments() -> dict:
    """All instrument plugins with a short description and typical uses."""
    return {"instruments": {
        name: {"description": spec["description"], "good_for": spec["use"],
               **({"has_presets": True} if spec.get("preset_dir") else {}),
               **({"needs_preset": True} if spec.get("needs_preset") else {})}
        for name, spec in catalog.INSTRUMENTS.items()}}


@tool(title="List effects")
def list_effects() -> dict:
    """All built-in effects with a short description."""
    return {"effects": {name: spec["description"] for name, spec in catalog.EFFECTS.items()}}


@tool(title="Describe plugin")
def describe_plugin(name: str) -> dict:
    """Parameters of an instrument or effect with defaults and ranges. Also accepts the topics
    "filters", "chords" (arpeggiator/stacking chord names), "drumkits" and "scales".

    Args:
        name: Plugin name, e.g. "tripleoscillator", "reverbsc", or a topic.
    """
    key = name.strip().lower()
    key = _INSTRUMENT_ALIASES.get(key, key)
    if key in catalog.INSTRUMENTS:
        spec = catalog.INSTRUMENTS[key]
        return {"instrument": key, "description": spec["description"],
                "parameters": catalog.describe(spec["params"]),
                "note": "Set with add_instrument_track(params=...) or set_instrument(params=...). "
                        "Per-note volume envelope and filter: set_envelope / set_filter."}
    if key in catalog.EFFECTS:
        spec = catalog.EFFECTS[key]
        return {"effect": key, "description": spec["description"],
                "parameters": catalog.describe(spec["params"]),
                **({"requires": "LMMS 1.3"} if spec.get("min_version") else {})}
    if key in ("filters", "filter"):
        return {"filter_types": catalog.FILTER_TYPES}
    if key in ("chords", "chord", "arpeggiator"):
        return {"chords": catalog.CHORD_TABLE[:67], "scales_usable_as_arp": catalog.CHORD_TABLE[67:],
                "arp_directions": catalog.ARP_DIRECTIONS}
    if key in ("drumkits", "drumkit", "kits"):
        return {"drum_kits": catalog.DRUM_KITS}
    if key == "scales":
        return {"scales": sorted(theory.SCALES)}
    raise ProjectError(f"Unknown plugin '{name}'. Instruments: {', '.join(catalog.INSTRUMENTS)}. "
                       f"Effects: {', '.join(catalog.EFFECTS)}.")


@tool(title="List presets")
def list_presets(instrument: str | None = None, search: str | None = None, limit: int = 80) -> dict:
    """Browse instrument presets of the installed LMMS (ready-made sounds).

    Without arguments returns the number of presets per instrument. ZynAddSubFX has the
    largest library, organised in folders like Bass, Brass, Pads, Strings, Organ, Guitar, Rhodes.

    Args:
        instrument: e.g. "tripleoscillator", "zynaddsubfx", "lb302", "kicker".
        search: Words that must appear in the preset path, e.g. "piano", "pad", "bass".
        limit: Maximum number of names returned.
    """
    key = _INSTRUMENT_ALIASES.get((instrument or "").lower(), (instrument or "").lower()) or None
    data = lmms_env.list_presets(key, search)
    if not data["presets_per_instrument"]:
        return {"presets": [], "note": "No presets found: the LMMS data folder is unknown. Set the "
                                       "LMMS_DATA_DIR environment variable (see get_environment)."}
    if not instrument and not search:
        return {"presets_per_instrument": data["presets_per_instrument"],
                "hint": "Call again with instrument and/or search to see names."}
    names = data["presets"]
    return {"count": len(names), "presets": names[:max(1, limit)],
            **({"truncated": True} if len(names) > limit else {})}


@tool(title="List samples")
def list_samples(category: str | None = None, search: str | None = None, limit: int = 120) -> dict:
    """Browse the factory samples of the installed LMMS (drums, basses, instruments, loops...).

    Without arguments returns the categories. Sample paths can be used wherever a `sample` or
    `file` is expected.

    Args:
        category: e.g. "drums", "basses", "instruments", "beats", "effects", "stringsnpads".
        search: Words that must appear in the path, e.g. "snare", "kick hard".
        limit: Maximum number of paths returned.
    """
    data = lmms_env.list_samples(category, search)
    if not data["categories"]:
        return {"samples": [], "note": "No samples found: the LMMS data folder is unknown. Set the "
                                       "LMMS_DATA_DIR environment variable (see get_environment). "
                                       "The drum kits of add_drum_kit still work in LMMS."}
    if not category and not search:
        return {"categories": data["categories"], "hint": "Call again with a category or search."}
    names = data["samples"]
    return {"count": len(names), "samples": names[:max(1, limit)],
            **({"truncated": True} if len(names) > limit else {})}


@tool(title="Scale / chord lookup")
def music_theory(scale: str | None = None, chord: str | None = None, octave: int = 4) -> dict:
    """Look up the notes of a scale or chord.

    Args:
        scale: Root and scale name, e.g. "A minor", "F# dorian", "C major_pentatonic".
        chord: Chord symbol, e.g. "Am7", "F#maj7", "G7/B".
        octave: Octave of the root (4 = middle).
    """
    result: dict = {}
    if scale:
        root, _, kind = scale.strip().partition(" ")
        notes = theory.scale_notes(root, kind or "major", octave)
        result["scale"] = {"notes": [theory.pitch_name(n) for n in notes], "midi": notes}
        intervals = theory.scale_intervals(kind or "major")
        if len(intervals) == 7:
            pcs = [(theory.pitch_class(root) + i) % 12 for i in intervals]
            triads = []
            for degree in range(7):
                third = (pcs[(degree + 2) % 7] - pcs[degree]) % 12
                fifth = (pcs[(degree + 4) % 7] - pcs[degree]) % 12
                quality = {(4, 7): "", (3, 7): "m", (3, 6): "dim", (4, 8): "aug"}.get((third, fifth), "?")
                triads.append(theory.NOTE_NAMES[pcs[degree]] + quality)
            result["scale"]["diatonic_chords"] = triads
    if chord:
        notes = theory.parse_chord(chord, octave)
        result["chord"] = {"notes": [theory.pitch_name(n) for n in notes], "midi": notes}
    if not result:
        result = {"scales": sorted(theory.SCALES),
                  "chord_qualities": [q or "(major)" for q in sorted(theory.CHORDS)]}
    return result


# ---------------------------------------------------------------------------
# files, LMMS application
# ---------------------------------------------------------------------------

@tool(title="Environment")
def get_environment() -> dict:
    """Where things are on this machine: LMMS executable and version, factory data folder,
    projects folder, and the project files found there."""
    folder = lmms_env.projects_dir()
    files = []
    if folder.is_dir():
        found = [f for f in folder.rglob("*") if f.suffix.lower() in (".mmpz", ".mmp") and f.is_file()]
        found.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        files = [f.relative_to(folder).as_posix() for f in found[:60]]
    exe, data = lmms_env.find_executable(), lmms_env.data_dir()
    info = {
        "server_version": __version__,
        "lmms_executable": str(exe) if exe else None,
        "lmms_version": lmms_env.version(),
        "new_project_format": lmms_env.default_target(),
        "lmms_data_dir": str(data) if data else None,
        "projects_dir": str(folder),
        "project_files": files,
    }
    hints = []
    if exe is None:
        hints.append("LMMS not found: set LMMS_EXECUTABLE to enable render_project / open_in_lmms.")
    if data is None:
        hints.append("Factory data not found: set LMMS_DATA_DIR (folder containing 'samples' and "
                     "'presets') to enable list_presets / list_samples.")
    if hints:
        info["hints"] = hints
    return info


def _saved_path(p: Project) -> Path:
    if p.path is None or p.modified or not p.path.is_file():
        raise ProjectError("The project has unsaved changes - call save_project first")
    return p.path


@tool(title="Render audio")
def render_project(output: str | None = None, format: str = "wav") -> dict:
    """Export the saved project to an audio file using the installed LMMS (may take a while).

    Args:
        output: Target file. Default: next to the project file with the same name.
        format: "wav", "flac", "ogg" or "mp3".
    """
    p = _project()
    source = _saved_path(p)
    fmt = format.strip().lower()
    if fmt not in ("wav", "flac", "ogg", "mp3"):
        raise ProjectError("format must be wav, flac, ogg or mp3")
    target = Path(output).expanduser() if output else source.with_suffix("." + fmt)
    if not target.is_absolute():
        target = source.parent / target
    if target.suffix.lower() != "." + fmt:
        target = target.with_suffix("." + fmt)
    try:
        return lmms_env.render(source, target, fmt)
    except RuntimeError as exc:
        raise ProjectError(str(exc)) from None


@tool(title="Open in LMMS")
def open_in_lmms() -> dict:
    """Launch LMMS with the saved project so the user can listen to and edit it."""
    source = _saved_path(_project())
    try:
        lmms_env.open_in_lmms(source)
    except (RuntimeError, OSError) as exc:
        raise ProjectError(str(exc)) from None
    return {"opened": str(source)}


@tool(mutates=True, title="Import MIDI file")
def import_midi(path: str, bar: float = 1, use_tempo: bool = True) -> dict:
    """Import the notes of a standard MIDI file (.mid) as new instrument tracks.

    Melodic channels become tripleoscillator tracks (change sounds afterwards with
    set_instrument); the drum channel is split into sample tracks per drum.

    Args:
        path: Absolute path of the .mid file.
        bar: Song position where the MIDI file starts.
        use_tempo: Take over the tempo and time signature of the file.
    """
    p = _project()
    source = Path(path.strip().strip('"')).expanduser()
    if not source.is_file():
        raise ProjectError(f"File not found: {source}")
    try:
        song = midi_import.read_midi(source)
    except (ValueError, IndexError) as exc:
        raise ProjectError(f"Cannot read MIDI file: {exc}") from None
    if use_tempo:
        p.set_tempo(max(10, min(999, song.tempo)))
        if song.denominator in (1, 2, 4, 8, 16, 32):
            p.set_time_signature(song.numerator, song.denominator)
    created = []
    kit = catalog.DRUM_KITS["standard"]
    for midi_track in song.tracks:
        if midi_track.is_drums:
            groups: dict[str, list[NoteEvent]] = {}
            for start, _length, pitch, velocity in midi_track.notes:
                piece = _GM_DRUMS.get(pitch)
                if piece:
                    groups.setdefault(piece, []).append(NoteEvent(69, start, 0.25, round(velocity / 127 * 160)))
            for piece, events in groups.items():
                track = p.add_instrument_track(f"Drums {piece}")
                _configure_instrument(p, track, sample=kit[piece])
                p.add_notes(track, events, bar)
                created.append({"track": track.get("name"), "notes": len(events)})
            continue
        track = p.add_instrument_track(midi_track.name or "MIDI")
        p.set_instrument(track, "tripleoscillator")
        events = [NoteEvent(pitch, start, length, round(velocity / 127 * 160))
                  for start, length, pitch, velocity in midi_track.notes]
        p.add_notes(track, events, bar)
        created.append({"track": track.get("name"), "notes": len(events),
                        "midi_program": midi_track.program})
    return {"tempo": p.tempo, "tracks_created": created}


# ---------------------------------------------------------------------------
# resources and prompts
# ---------------------------------------------------------------------------

GUIDE = INSTRUCTIONS + """
SONG BUILDING RECIPE
1. new_project(name, tempo, time_signature)
2. Drums: add_drum_kit() -> set_beat("Main", {...}) -> duplicate_pattern("Main", "Fill") ->
   arrange_pattern("Main", bar=1, length_bars=8)
3. Bass: add_instrument_track("Bass", instrument="lb302") or tripleoscillator with
   set_envelope + set_filter -> add_notes(track="Bass", sequence="A1:0.5 A1 r A2 ...", bar=1)
4. Harmony: add_instrument_track("Pad", preset="...") -> add_chords("Pad", "Am F C G", bar=1)
5. Melody: add_notes("Lead", sequence=...)
6. Structure: write each section once (intro, verse, chorus...), then copy_section.
7. Mix: update_track volumes (keep bass/kick around 80-100, pads 50-70), add_effect
   (reverbsc on pads/leads with wet 0.2-0.3, eq), add_automation for fades and sweeps.
8. validate_project -> save_project -> open_in_lmms or render_project.

SOUND DESIGN WITH TRIPLEOSCILLATOR
- Pluck: wavetype 2 (saw), set_envelope(volume, attack=0, hold=0, decay=0.25, sustain=0),
  set_filter(lowpass, cutoff=3000).
- Pad: detune oscillators (finel0=-8, finer0=8), set_envelope(attack=0.6, sustain=1, release=1.2).
- Sub bass: wavetype0=0 (sine), vol1=0, vol2=0.
- Supersaw lead: all wavetype 2, coarse0/1/2 = 0, finel/finer spread +-10..20.
"""


@mcp.resource("lmms://guide", name="LMMS MCP guide", mime_type="text/plain")
def guide_resource() -> str:
    """How to build a song with this server: conventions, workflow and sound design tips."""
    return GUIDE


@mcp.resource("lmms://project", name="Current project", mime_type="application/json")
def project_resource() -> str:
    """Overview of the currently open project."""
    if _State.project is None:
        return _json({"open": False})
    return _json(_State.project.summary(detail=True))


@mcp.prompt(name="compose_song")
def compose_song(style: str = "house", tempo: str = "", key: str = "A minor", bars: str = "32") -> str:
    """Compose and save a complete song in a given style."""
    tempo_text = f"at {tempo} BPM" if tempo else "at a tempo typical for the style"
    return (
        f"Compose a complete {style} song in {key} {tempo_text}, about {bars} bars long, using the "
        f"LMMS tools.\n\n"
        "1. Call get_environment, then new_project with a fitting name.\n"
        "2. Plan the structure (e.g. intro, verse, chorus, break, outro) with bar numbers.\n"
        "3. Add a drum kit and write at least two beat patterns (main + variation/fill) with set_beat; "
        "place them with arrange_pattern.\n"
        "4. Add bass, chords/pad and a lead. Prefer presets if list_presets finds some, otherwise "
        "shape tripleoscillator/lb302 with set_envelope and set_filter. Use music_theory to stay in key.\n"
        "5. Write each section once and build the arrangement with copy_section. Vary sections "
        "(drop instruments in the intro/break, add layers in the chorus).\n"
        "6. Balance volumes, add reverb/delay where it helps, add an automation (e.g. filter sweep or fade).\n"
        "7. Run validate_project, fix issues, save_project and report the file path and structure."
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
