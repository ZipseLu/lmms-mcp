"""In-memory LMMS project with high level editing operations.

The project is kept as the original XML tree, so everything LMMS stored in a
file survives a load/edit/save round trip, including settings this module
knows nothing about.

Units used by the public API:

* ``bar``    - 1-based bar number on the song timeline (bar 1 = song start).
               Fractions are allowed (2.5 = middle of bar 2 in 4/4).
* beats     - quarter notes, 0-based offsets (note ``start`` / ``length``).
* pitches   - MIDI note numbers (C4 = 60).
"""

from __future__ import annotations

import copy
import math
import random
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from . import catalog, dialect as dialects, fileio
from .dialect import Dialect
from .theory import NoteEvent, pitch_name

TICKS_PER_BEAT = 48
TICKS_PER_STEP = 12
TRACK_TYPES = {0: "instrument", 1: "pattern", 2: "sample", 5: "automation", 6: "automation"}

_ENV_DEFAULTS = {
    "pdel": 0, "att": 0, "hold": 0.5, "dec": 0.5, "sustain": 0.5, "rel": 0.1, "amt": 0,
    "lpdel": 0, "latt": 0, "lspd": 0.1, "lamt": 0, "lshp": 0, "x100": 0, "ctlenvamt": 0,
    "userwavefile": "", "lspd_numerator": 4, "lspd_denominator": 4, "lspd_syncmode": 0,
}
_ARP_DEFAULTS = {
    "arp-enabled": 0, "arp": 0, "arprange": 1, "arpdir": 0, "arpmode": 0, "arptime": 200,
    "arpgate": 100, "arpcycle": 0, "arpskip": 0, "arpmiss": 0,
    "arptime_numerator": 4, "arptime_denominator": 4, "arptime_syncmode": 0,
}
_MIDIPORT_DEFAULTS = {
    "inputcontroller": 0, "fixedoutputvelocity": -1, "inputchannel": 0, "outputcontroller": 0,
    "writable": 0, "outputchannel": 1, "fixedinputvelocity": -1, "fixedoutputnote": -1,
    "outputprogram": 1, "basevelocity": 63, "readable": 0,
}
_WINDOW = {"visible": "0", "maximized": "0", "minimized": "0"}


class ProjectError(ValueError):
    """An editing request that cannot be carried out (bad reference, bad value...)."""


# ---------------------------------------------------------------------------
# small XML helpers
# ---------------------------------------------------------------------------

def fmt(value) -> str:
    """Format a value the way LMMS/Qt writes attributes."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProjectError("Numeric values must be finite")
        if value == int(value) and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.6g}"
    return str(value)


def num(text, default: float = 0.0) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _set_all(el: ET.Element, values: dict) -> None:
    for key, value in values.items():
        el.set(key, fmt(value))


def _model_node(el: ET.Element, name: str) -> ET.Element | None:
    """Child element holding an automated / log-scaled model value, if any."""
    for child in el:
        if child.tag == name and "value" in child.attrib:
            return child
        if child.tag == "automatablemodel" and child.get("nodename") == name:
            return child
    return None


def model_get(el: ET.Element, name: str, default=None):
    node = _model_node(el, name)
    if node is not None:
        return node.get("value", default)
    return el.get(name, default)


def model_set(el: ET.Element, name: str, value) -> None:
    node = _model_node(el, name)
    if node is not None:
        node.set("value", fmt(value))
    else:
        el.set(name, fmt(value))


def _sub(parent: ET.Element, tag: str, values: dict | None = None) -> ET.Element:
    el = ET.SubElement(parent, tag)
    if values:
        _set_all(el, values)
    return el


class Project:
    """An LMMS song being edited."""

    def __init__(self, root: ET.Element, dialect: Dialect, path: Path | None = None):
        self.root = root
        self.d = dialect
        self.path = path
        self.modified = False

    # ------------------------------------------------------------------
    # creation / persistence
    # ------------------------------------------------------------------
    @classmethod
    def new(cls, dialect: Dialect, tempo: float = 120, numerator: int = 4, denominator: int = 4,
            master_volume: int = 100) -> "Project":
        root = ET.Element("lmms-project", dialect.root_attributes())
        _sub(root, "head", {"bpm": 140, "timesig_numerator": 4, "timesig_denominator": 4,
                            "mastervol": 100, "masterpitch": 0})
        song = ET.SubElement(root, "song")
        ET.SubElement(song, "trackcontainer", {
            "type": "song", "visible": "1", "width": "900", "height": "420", "x": "5", "y": "5",
            "maximized": "0", "minimized": "0"})
        mixer = ET.SubElement(song, dialect.tag("mixer"), {
            "visible": "1", "width": "600", "height": "335", "x": "5", "y": "430",
            "maximized": "0", "minimized": "0"})
        master = _sub(mixer, dialect.tag("mixer_channel"),
                      {"num": 0, "name": "Master", "volume": 1, "muted": 0, "soloed": 0})
        _sub(master, "fxchain", {"numofeffects": 0, "enabled": 0})
        ET.SubElement(song, "ControllerRackView", {**_WINDOW, "width": "350", "height": "200", "x": "680", "y": "310"})
        ET.SubElement(song, "pianoroll", {**_WINDOW, "width": "640", "height": "480", "x": "5", "y": "5"})
        ET.SubElement(song, "automationeditor", {**_WINDOW, "width": "640", "height": "400", "x": "5", "y": "5"})
        ET.SubElement(song, "projectnotes", {**_WINDOW, "width": "640", "height": "400", "x": "700", "y": "10"})
        _sub(song, "timeline", {"lp0pos": 0, "lp1pos": 192, "lpstate": 0})
        ET.SubElement(song, "controllers")
        project = cls(root, dialect)
        project.set_time_signature(numerator, denominator)
        project.set_tempo(tempo)
        project.set_master_volume(master_volume)
        project.modified = True
        return project

    @classmethod
    def open(cls, path: str | Path) -> "Project":
        path = Path(path)
        root = fileio.read_file(path)
        if root.get("type") not in ("song", "songtemplate") or root.find("song") is None:
            raise ProjectError(f"{path.name} is not an LMMS song project (type={root.get('type')!r})")
        dialect = dialects.detect(root)
        dialects.normalize(root, dialect)
        project = cls(root, dialect, None if path.suffix.lower() == ".mpt" else path)
        if project.container is None:
            raise ProjectError(f"{path.name} has no song track container")
        project.mixer  # make sure a mixer with a master channel exists
        return project

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path else self.path
        if target is None:
            raise ProjectError("The project has no file name yet - pass a path")
        self.root.set("type", "song")
        self._finalize()
        fileio.write_file(target, self.root)
        self.path = target
        self.modified = False
        return target

    def to_xml(self) -> str:
        self._finalize()
        return fileio.serialize(self.root)

    def _finalize(self) -> None:
        """Keep derived attributes consistent before writing."""
        for chain in self.root.iter("fxchain"):
            chain.set("numofeffects", str(len(chain.findall("effect"))))
        for clips in self._all_midi_clip_lists():
            for clip in clips:
                clip[:] = sorted(
                    clip, key=lambda n: (n.tag != "note", int(num(n.get("pos"))), int(num(n.get("key")))))

    # ------------------------------------------------------------------
    # structure accessors
    # ------------------------------------------------------------------
    @property
    def head(self) -> ET.Element:
        head = self.root.find("head")
        if head is None:
            head = ET.Element("head")
            self.root.insert(0, head)
        return head

    @property
    def song(self) -> ET.Element:
        return self.root.find("song")

    @property
    def container(self) -> ET.Element | None:
        for tc in self.song.findall("trackcontainer"):
            if tc.get("type", "song") == "song":
                return tc
        return None

    @property
    def mixer(self) -> ET.Element:
        mixer = self.song.find(self.d.tag("mixer"))
        if mixer is None:
            mixer = ET.SubElement(self.song, self.d.tag("mixer"), dict(_WINDOW))
        if not any(int(num(c.get("num"))) == 0 for c in mixer.findall(self.d.tag("mixer_channel"))):
            master = ET.Element(self.d.tag("mixer_channel"))
            _set_all(master, {"num": 0, "name": "Master", "volume": 1, "muted": 0, "soloed": 0})
            _sub(master, "fxchain", {"numofeffects": 0, "enabled": 0})
            mixer.insert(0, master)
        return mixer

    @property
    def numerator(self) -> int:
        return int(num(model_get(self.head, "timesig_numerator", 4), 4)) or 4

    @property
    def denominator(self) -> int:
        return int(num(model_get(self.head, "timesig_denominator", 4), 4)) or 4

    @property
    def tpb(self) -> int:
        """Ticks per bar."""
        return max(1, 192 * self.numerator // self.denominator)

    @property
    def beats_per_bar(self) -> float:
        return self.tpb / TICKS_PER_BEAT

    @property
    def tempo(self) -> float:
        return num(model_get(self.head, "bpm", 140), 140)

    def bar_to_tick(self, bar: float) -> int:
        if bar < 1:
            raise ProjectError(f"Bars are numbered from 1 (got {bar})")
        return int(round((bar - 1) * self.tpb))

    def tick_to_bar(self, tick: int) -> float:
        return round(tick / self.tpb + 1, 4)

    @staticmethod
    def beats_to_ticks(beats: float) -> int:
        return int(round(beats * TICKS_PER_BEAT))

    def _ceil_bar(self, ticks: int) -> int:
        return max(1, math.ceil(ticks / self.tpb)) * self.tpb

    # ------------------------------------------------------------------
    # song settings
    # ------------------------------------------------------------------
    def set_tempo(self, bpm: float) -> None:
        if not 10 <= bpm <= 999:
            raise ProjectError("Tempo must be between 10 and 999 BPM")
        model_set(self.head, "bpm", int(round(bpm)))
        self.modified = True

    def set_time_signature(self, numerator: int, denominator: int) -> None:
        if not 1 <= numerator <= 32 or denominator not in (1, 2, 4, 8, 16, 32):
            raise ProjectError("Time signature must be 1-32 over 1, 2, 4, 8, 16 or 32")
        old_tpb = self.tpb
        model_set(self.head, "timesig_numerator", numerator)
        model_set(self.head, "timesig_denominator", denominator)
        if self.tpb != old_tpb:
            # Patterns of the beat editor live at one bar per pattern.
            for track in self.beat_tracks():
                for index, clip in enumerate(self._clips(track)):
                    clip.set("pos", str(index * self.tpb))
        self.modified = True

    def set_master_volume(self, volume: float) -> None:
        if not 0 <= volume <= 200:
            raise ProjectError("Master volume must be 0-200")
        model_set(self.head, "mastervol", int(round(volume)))
        self.modified = True

    def set_master_pitch(self, semitones: int) -> None:
        if not -12 <= semitones <= 12:
            raise ProjectError("Master pitch must be -12..12 semitones")
        model_set(self.head, "masterpitch", int(semitones))
        self.modified = True

    def set_loop(self, start_bar: float | None, end_bar: float | None, enabled: bool | None) -> None:
        timeline = self.song.find("timeline")
        if timeline is None:
            timeline = _sub(self.song, "timeline", {"lp0pos": 0, "lp1pos": self.tpb, "lpstate": 0})
        if start_bar is not None:
            timeline.set("lp0pos", str(self.bar_to_tick(start_bar)))
        if end_bar is not None:
            timeline.set("lp1pos", str(self.bar_to_tick(end_bar)))
        if enabled is not None:
            timeline.set("lpstate", fmt(bool(enabled)))
        self.modified = True

    def set_notes_text(self, text: str) -> None:
        node = self.song.find("projectnotes")
        if node is None:
            node = ET.SubElement(self.song, "projectnotes", dict(_WINDOW))
        for child in list(node):
            node.remove(child)
        node.text = text
        self.modified = True

    # ------------------------------------------------------------------
    # tracks
    # ------------------------------------------------------------------
    def tracks(self) -> list[ET.Element]:
        return self.container.findall("track")

    @staticmethod
    def track_type(track: ET.Element) -> str:
        return TRACK_TYPES.get(int(num(track.get("type"))), "other")

    def _pattern_tracks(self) -> list[ET.Element]:
        return [t for t in self.tracks() if self.track_type(t) == "pattern"]

    def _store(self, create: bool = False) -> ET.Element | None:
        """The shared container holding the instruments of the beat/pattern editor."""
        tag = self.d.tag("pattern_track")
        patterns = self._pattern_tracks()
        for track in patterns:
            holder = track.find(tag)
            store = holder.find("trackcontainer") if holder is not None else None
            if store is not None:
                first = patterns[0].find(tag)
                if first is None:
                    first = ET.SubElement(patterns[0], tag)
                    patterns[0].remove(first)
                    patterns[0].insert(0, first)
                if holder is not first:  # LMMS only reads it from the first pattern track
                    holder.remove(store)
                    first.append(store)
                return store
        if not create or not patterns:
            return None
        first = patterns[0].find(tag)
        if first is None:
            first = ET.Element(tag)
            patterns[0].insert(0, first)
        return ET.SubElement(first, "trackcontainer", {
            "type": self.d.pattern_store_type, "visible": "0", "width": "700", "height": "400",
            "x": "610", "y": "5", "maximized": "0", "minimized": "0"})

    def beat_tracks(self) -> list[ET.Element]:
        store = self._store()
        return store.findall("track") if store is not None else []

    def find_track(self, ref, scope: str = "song") -> ET.Element:
        tracks = self.tracks() if scope == "song" else self.beat_tracks()
        what = "track" if scope == "song" else "beat instrument"
        if isinstance(ref, bool) or ref is None:
            raise ProjectError(f"Invalid {what} reference {ref!r}")
        if isinstance(ref, int) or (isinstance(ref, str) and re.fullmatch(r"\d+", ref.strip())):
            index = int(ref)
            if not 0 <= index < len(tracks):
                raise ProjectError(f"No {what} with index {index} (there are {len(tracks)}: "
                                   f"{self._names(tracks)})")
            return tracks[index]
        wanted = str(ref).strip().lower()
        exact = [t for t in tracks if t.get("name", "").lower() == wanted]
        partial = exact or [t for t in tracks if wanted in t.get("name", "").lower()]
        if len(partial) == 1 or (exact and len(exact) >= 1):
            return partial[0]
        if not partial:
            raise ProjectError(f"No {what} named '{ref}'. Available: {self._names(tracks)}")
        raise ProjectError(f"'{ref}' matches several {what}s: {self._names(partial)}. Use the index.")

    @staticmethod
    def _names(tracks: list[ET.Element]) -> str:
        return ", ".join(f"{i}:{t.get('name', '')}" for i, t in enumerate(tracks)) or "(none)"

    def track_index(self, track: ET.Element, scope: str = "song") -> int:
        tracks = self.tracks() if scope == "song" else self.beat_tracks()
        return tracks.index(track)

    def _unique_name(self, name: str, scope: str = "song") -> str:
        existing = {t.get("name", "").lower() for t in (self.tracks() if scope == "song" else self.beat_tracks())}
        if name.lower() not in existing:
            return name
        i = 2
        while f"{name} {i}".lower() in existing:
            i += 1
        return f"{name} {i}"

    @staticmethod
    def _settings(track: ET.Element) -> ET.Element | None:
        """The <instrumenttrack>/<sampletrack> element of a track."""
        for tag in ("instrumenttrack", "sampletrack"):
            el = track.find(tag)
            if el is not None:
                return el
        return None

    def instrument_name(self, track: ET.Element) -> str | None:
        it = track.find("instrumenttrack")
        inst = it.find("instrument") if it is not None else None
        if inst is None:
            return None
        return dialects.MODERN.instrument_name(inst.get("name", ""))

    def _plugin_node(self, track: ET.Element) -> ET.Element:
        it = track.find("instrumenttrack")
        inst = it.find("instrument") if it is not None else None
        if inst is None:
            raise ProjectError(f"Track '{track.get('name')}' is not an instrument track")
        node = next((c for c in inst if c.tag != "key"), None)
        if node is None:
            node = ET.SubElement(inst, inst.get("name", "instrument"))
        return node

    def _new_instrument_track(self, parent: ET.Element, name: str, volume: float, pan: float,
                              mixer_channel: int) -> ET.Element:
        track = _sub(parent, "track", {"type": 0, "name": name, "muted": 0, "solo": 0})
        it = _sub(track, "instrumenttrack", {
            "vol": volume, "pan": pan, "pitch": 0, "pitchrange": 1,
            self.d.mixer_attr: mixer_channel, "basenote": self.d.base_note, "usemasterpitch": 1})
        ET.SubElement(it, "instrument", {"name": "tripleoscillator"})
        eldata = _sub(it, "eldata", {"fcut": 14000, "fres": 0.5, "ftype": 0, "fwet": 0})
        for tag in ("elvol", "elcut", "elres"):
            _sub(eldata, tag, _ENV_DEFAULTS)
        _sub(it, "chordcreator", {"chord": 0, "chordrange": 1, "chord-enabled": 0})
        _sub(it, "arpeggiator", _ARP_DEFAULTS)
        _sub(it, "midiport", _MIDIPORT_DEFAULTS)
        _sub(it, "fxchain", {"numofeffects": 0, "enabled": 0})
        return track

    def add_instrument_track(self, name: str, volume: float = 100, pan: float = 0,
                             mixer_channel: int = 0, scope: str = "song") -> ET.Element:
        self._check_vol_pan(volume, pan)
        self._check_channel(mixer_channel)
        if scope == "song":
            parent = self.container
        else:
            if not self._pattern_tracks():
                self.add_pattern("Beat 1")
            parent = self._store(create=True)
        track = self._new_instrument_track(parent, self._unique_name(name, scope), volume, pan, mixer_channel)
        if scope != "song":
            for index in range(len(self._pattern_tracks())):
                self._new_beat_clip(track, index)
        self.modified = True
        return track

    def add_sample_track(self, name: str, volume: float = 100, pan: float = 0,
                         mixer_channel: int = 0) -> ET.Element:
        self._check_vol_pan(volume, pan)
        self._check_channel(mixer_channel)
        track = _sub(self.container, "track",
                     {"type": 2, "name": self._unique_name(name), "muted": 0, "solo": 0})
        st = _sub(track, "sampletrack", {"vol": volume, "pan": pan, self.d.mixer_attr: mixer_channel})
        _sub(st, "fxchain", {"numofeffects": 0, "enabled": 0})
        self.modified = True
        return track

    def _automation_track(self, name: str) -> ET.Element:
        for track in self.tracks():
            if self.track_type(track) == "automation" and track.get("name") == name:
                return track
        track = _sub(self.container, "track", {"type": 5, "name": name, "muted": 0, "solo": 0})
        ET.SubElement(track, "automationtrack")
        return track

    @staticmethod
    def _check_vol_pan(volume: float, pan: float) -> None:
        if not 0 <= volume <= 200:
            raise ProjectError("Volume must be 0-200 (100 = unchanged)")
        if not -100 <= pan <= 100:
            raise ProjectError("Pan must be -100 (left) .. 100 (right)")

    def update_track(self, track: ET.Element, *, name=None, volume=None, pan=None, muted=None,
                     solo=None, mixer_channel=None, pitch=None, pitch_range=None,
                     base_note=None, color=None, use_master_pitch=None) -> None:
        settings = self._settings(track)
        if name is not None:
            track.set("name", str(name))
        if muted is not None:
            model_set(track, "muted", bool(muted))
        if solo is not None:
            model_set(track, "solo", bool(solo))
        if color is not None:
            if color == "":
                track.attrib.pop("color", None)
            elif not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                raise ProjectError("Color must look like #rrggbb")
            elif self.d.modern:
                track.set("color", color.lower())
        needs_settings = [volume, pan, mixer_channel, pitch, pitch_range, base_note, use_master_pitch]
        if any(v is not None for v in needs_settings) and settings is None:
            raise ProjectError(f"Track '{track.get('name')}' ({self.track_type(track)}) has no "
                               f"volume/pan/mixer settings")
        if volume is not None or pan is not None:
            self._check_vol_pan(100 if volume is None else volume, 0 if pan is None else pan)
        if volume is not None:
            model_set(settings, "vol", volume)
        if pan is not None:
            model_set(settings, "pan", pan)
        if mixer_channel is not None:
            self._check_channel(mixer_channel)
            model_set(settings, self.d.mixer_attr, int(mixer_channel))
        instrument_only = [pitch, pitch_range, base_note, use_master_pitch]
        if any(v is not None for v in instrument_only) and settings.tag != "instrumenttrack":
            raise ProjectError("pitch / base_note settings only exist on instrument tracks")
        if pitch is not None:
            if not -6000 <= pitch <= 6000:
                raise ProjectError("pitch is in cents (-6000..6000; 100 = one semitone)")
            model_set(settings, "pitch", pitch)
            needed = math.ceil(abs(pitch) / 100)
            if needed > num(model_get(settings, "pitchrange", 1), 1):
                model_set(settings, "pitchrange", min(60, needed))
        if pitch_range is not None:
            model_set(settings, "pitchrange", int(pitch_range))
        if base_note is not None:
            # stored value: which key plays the instrument's "A4"
            model_set(settings, "basenote", int(base_note) + self.d.base_note - 69)
        if use_master_pitch is not None:
            model_set(settings, "usemasterpitch", bool(use_master_pitch))
        self.modified = True

    def remove_track(self, track: ET.Element, scope: str = "song") -> None:
        if scope != "song":
            self._store().remove(track)
        elif self.track_type(track) == "pattern":
            self._remove_pattern(track)
        else:
            self.container.remove(track)
        self.modified = True

    def move_track(self, track: ET.Element, new_index: int, scope: str = "song") -> None:
        if scope == "song" and self.track_type(track) == "pattern":
            raise ProjectError("Pattern tracks cannot be reordered (their order is the pattern number)")
        parent = self.container if scope == "song" else self._store()
        tracks = parent.findall("track")
        if not 0 <= new_index < len(tracks):
            raise ProjectError(f"new_index must be 0..{len(tracks) - 1}")
        others = [t for t in tracks if t is not track]
        if scope == "song":
            # keep pattern tracks in their relative order
            pass
        others.insert(new_index, track)
        non_tracks = [c for c in parent if c.tag != "track"]
        parent[:] = non_tracks + others
        self.modified = True

    def duplicate_track(self, track: ET.Element, name: str | None = None) -> ET.Element:
        if self.track_type(track) == "pattern":
            raise ProjectError("Use duplicate_pattern for beat patterns")
        clone = copy.deepcopy(track)
        clone.set("name", self._unique_name(name or f"{track.get('name', 'Track')} copy"))
        self._strip_ids(clone)
        parent = self.container
        parent.insert(list(parent).index(track) + 1, clone)
        self.modified = True
        return clone

    def _strip_ids(self, element: ET.Element) -> None:
        """Turn automated models of a copied subtree back into plain attributes."""
        for parent in element.iter():
            for child in list(parent):
                if "id" in child.attrib and "value" in child.attrib and len(child) == 0 \
                        and child.tag not in ("object",):
                    name = child.get("nodename", child.tag)
                    parent.remove(child)
                    parent.set(name, child.get("value"))

    # ------------------------------------------------------------------
    # instruments
    # ------------------------------------------------------------------
    def set_instrument(self, track: ET.Element, instrument: str) -> ET.Element:
        """Replace the instrument plugin of a track with a default instance."""
        it = track.find("instrumenttrack")
        if it is None:
            raise ProjectError(f"Track '{track.get('name')}' is not an instrument track")
        canonical = dialects.MODERN.instrument_name(instrument.strip())
        key = canonical.lower()
        spec = catalog.INSTRUMENTS.get(key)
        stored = self.d.instrument_name(key if spec else canonical)
        old_offset = self.d.key_offset(self.instrument_name(track))
        for old in it.findall("instrument"):
            it.remove(old)
        inst = ET.Element("instrument", {"name": stored})
        it.insert(0, inst)
        node = ET.SubElement(inst, stored)
        if spec:
            _set_all(node, spec["defaults"])
            if key == "bitinvader":
                node.set("sampleShape", catalog.wave_table("sine", 128))
        new_offset = self.d.key_offset(key)
        if new_offset != old_offset:  # notes are stored differently for MIDI based plugins
            for clip in self._clips(track):
                for note in clip.findall("note"):
                    note.set("key", str(int(num(note.get("key"))) - old_offset + new_offset))
        self.modified = True
        return node

    def set_instrument_params(self, track: ET.Element, params: dict) -> list[str]:
        node = self._plugin_node(track)
        name = self.instrument_name(track) or ""
        warnings = []
        for key, value in params.items():
            if name == "bitinvader" and key == "wave":
                length = int(num(node.get("sampleLength", 128), 128)) or 128
                node.set("sampleShape", catalog.wave_table(str(value), length))
                continue
            if isinstance(value, (dict, list)):
                raise ProjectError(f"Parameter '{key}' must be a number or string")
            if node.attrib and key not in node.attrib and _model_node(node, key) is None \
                    and catalog.INSTRUMENTS.get(name, {}).get("defaults"):
                warnings.append(f"'{key}' is not a known {name} parameter (written anyway)")
            model_set(node, key, value)
        self.modified = True
        return warnings

    def apply_preset(self, track: ET.Element, preset_path: Path) -> str:
        """Load an LMMS .xpf preset or a ZynAddSubFX .xiz/.xmz patch into a track."""
        it = track.find("instrumenttrack")
        if it is None:
            raise ProjectError(f"Track '{track.get('name')}' is not an instrument track")
        suffix = preset_path.suffix.lower()
        if suffix in (".xiz", ".xmz"):
            return self._apply_zyn_preset(track, preset_path)
        root = fileio.read_file(preset_path)
        source = root.find(".//instrumenttrack")
        if source is None:
            if root.find(".//channeltrack") is not None:
                raise ProjectError(f"{preset_path.name} is in a too old preset format (LMMS 0.x)")
            raise ProjectError(f"{preset_path.name} is not an instrument preset")
        preset_dialect = dialects.detect(root)
        source = copy.deepcopy(source)
        if source.find("instrument") is None:
            # very old presets store the plugin settings directly below <instrumenttrack>
            known = ("eldata", "chordcreator", "arpeggiator", "midiport", "fxchain", "connection",
                     "midicontrollers")
            plugin = next((c for c in source if c.tag not in known and "id" not in c.attrib), None)
            if plugin is None:
                raise ProjectError(f"{preset_path.name} is not an instrument preset")
            source.remove(plugin)
            ET.SubElement(source, "instrument", {"name": plugin.tag}).append(plugin)
        inst = source.find("instrument")
        canonical = dialects.MODERN.instrument_name(inst.get("name", ""))
        stored = self.d.instrument_name(canonical)
        for node in inst:
            if node.tag == inst.get("name"):
                node.tag = stored
        inst.set("name", stored)
        old_offset = self.d.key_offset(self.instrument_name(track))

        # base note: convert between the old (A4 = 57) and new (A4 = 69) numbering
        base = num(model_get(source, "basenote", preset_dialect.base_note), preset_dialect.base_note)
        relative = base - preset_dialect.base_note
        if preset_dialect.file_version <= 16 < self.d.file_version:
            # LADSPA plugins were renamed in 1.3; those effect slots would be dead.
            chain = source.find("fxchain")
            if chain is not None:
                for effect in chain.findall("effect"):
                    if effect.get("name") == "ladspaeffect":
                        chain.remove(effect)
                if not chain.findall("effect"):
                    chain.set("enabled", "0")
        for child in list(it):
            if child.tag in ("instrument", "eldata", "chordcreator", "arpeggiator", "fxchain"):
                it.remove(child)
        for child in source:
            if child.tag in ("instrument", "eldata", "chordcreator", "arpeggiator", "fxchain") \
                    and it.find(child.tag) is None:
                it.append(child)
        if it.find("fxchain") is None:
            _sub(it, "fxchain", {"numofeffects": 0, "enabled": 0})
        model_set(it, "basenote", fmt(self.d.base_note + relative))
        model_set(it, "pitchrange", model_get(source, "pitchrange", 1))
        new_offset = self.d.key_offset(canonical)
        if new_offset != old_offset:
            for clip in self._clips(track):
                for note in clip.findall("note"):
                    note.set("key", str(int(num(note.get("key"))) - old_offset + new_offset))
        self.modified = True
        return canonical

    def _apply_zyn_preset(self, track: ET.Element, path: Path) -> str:
        import gzip
        raw = path.read_bytes()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        text = re.sub(r"<!DOCTYPE[^>]*>", "", raw.decode("utf-8", errors="replace")).strip()
        text = re.sub(r"^<\?xml[^>]*\?>", "", text).strip()
        try:
            data = ET.fromstring(text)
        except ET.ParseError as exc:
            raise ProjectError(f"Cannot read ZynAddSubFX preset {path.name}: {exc}") from exc
        if data.tag != "ZynAddSubFX-data":
            raise ProjectError(f"{path.name} is not a ZynAddSubFX preset")
        if self.instrument_name(track) != "zynaddsubfx":
            self.set_instrument(track, "zynaddsubfx")
        node = self._plugin_node(track)
        for old in list(node):
            node.remove(old)
        if data.find("MASTER") is None:
            # An instrument patch (.xiz): LMMS restores a complete "master" state,
            # so wrap the instrument into part 0 of an otherwise default master.
            instrument = data.find("INSTRUMENT")
            if instrument is None:
                raise ProjectError(f"{path.name} contains no instrument data")
            wrapped = ET.Element("ZynAddSubFX-data", dict(data.attrib))
            base = data.find("BASE_PARAMETERS")
            if base is not None:
                wrapped.append(base)
            master = ET.SubElement(wrapped, "MASTER")
            part = ET.SubElement(master, "PART", {"id": "0"})
            ET.SubElement(part, "par_bool", {"name": "enabled", "value": "yes"})
            part.append(instrument)
            data = wrapped
        node.append(data)
        self.modified = True
        return "zynaddsubfx"

    # --- envelope, filter, note functions -----------------------------
    def _eldata(self, track: ET.Element) -> ET.Element:
        it = track.find("instrumenttrack")
        if it is None:
            raise ProjectError(f"Track '{track.get('name')}' is not an instrument track")
        eldata = it.find("eldata")
        if eldata is None:
            eldata = _sub(it, "eldata", {"fcut": 14000, "fres": 0.5, "ftype": 0, "fwet": 0})
        for tag in ("elvol", "elcut", "elres"):
            if eldata.find(tag) is None:
                _sub(eldata, tag, _ENV_DEFAULTS)
        return eldata

    @staticmethod
    def _env_time(seconds: float, limit: float = 2.0) -> float:
        """Seconds -> knob value (LMMS envelope segments last 5 s * value^2)."""
        if seconds < 0:
            raise ProjectError("Envelope times must not be negative")
        return min(limit, math.sqrt(seconds / 5.0))

    def set_envelope(self, track: ET.Element, target: str, *, attack=None, hold=None, decay=None,
                     sustain=None, release=None, predelay=None, amount=None,
                     lfo_amount=None, lfo_speed_hz=None, lfo_wave=None, lfo_attack=None,
                     lfo_predelay=None) -> dict:
        tag = {"volume": "elvol", "vol": "elvol", "cutoff": "elcut", "cut": "elcut",
               "filter": "elcut", "resonance": "elres", "res": "elres"}.get(target.lower())
        if tag is None:
            raise ProjectError("Envelope target must be 'volume', 'cutoff' or 'resonance'")
        env = self._eldata(track).find(tag)
        times = {"att": attack, "hold": hold, "dec": decay, "rel": release, "pdel": predelay}
        touched = any(v is not None for v in (*times.values(), sustain))
        for key, seconds in times.items():
            if seconds is not None:
                model_set(env, key, round(self._env_time(seconds), 4))
        if sustain is not None:
            if not 0 <= sustain <= 1:
                raise ProjectError("sustain is a level between 0 and 1")
            model_set(env, "sustain", sustain)
        if amount is not None:
            if not -1 <= amount <= 1:
                raise ProjectError("amount must be -1..1")
            model_set(env, "amt", amount)
        elif touched and num(model_get(env, "amt", 0)) == 0:
            model_set(env, "amt", 1)  # an envelope with amount 0 does nothing
        if lfo_amount is not None:
            if not -1 <= lfo_amount <= 1:
                raise ProjectError("lfo_amount must be -1..1")
            model_set(env, "lamt", lfo_amount)
        if lfo_speed_hz is not None:
            if lfo_speed_hz <= 0:
                raise ProjectError("lfo_speed_hz must be positive")
            # period = 20 s * value (or / 100 with the x100 switch)
            period = 1.0 / lfo_speed_hz
            fast = period < 0.02 * 20
            value = period / 20.0 * (100 if fast else 1)
            model_set(env, "x100", fast)
            model_set(env, "lspd", round(min(1.0, max(0.001, value)), 5))
        if lfo_wave is not None:
            waves = ["sine", "triangle", "saw", "square", "user", "random"]
            if str(lfo_wave).lower() not in waves:
                raise ProjectError(f"lfo_wave must be one of {waves}")
            model_set(env, "lshp", waves.index(str(lfo_wave).lower()))
        if lfo_attack is not None:
            model_set(env, "latt", round(self._env_time(lfo_attack, 1.0), 4))
        if lfo_predelay is not None:
            model_set(env, "lpdel", round(self._env_time(lfo_predelay, 1.0), 4))
        if tag != "elvol" and num(model_get(self._eldata(track), "fwet", 0)) == 0:
            model_set(self._eldata(track), "fwet", 1)  # filter envelopes need the filter on
        self.modified = True
        return dict(env.attrib)

    def set_filter(self, track: ET.Element, *, enabled=None, filter_type=None, cutoff=None,
                   resonance=None) -> dict:
        eldata = self._eldata(track)
        if filter_type is not None:
            if isinstance(filter_type, int) or str(filter_type).isdigit():
                index = int(filter_type)
            else:
                key = str(filter_type).lower().replace("-", "").replace(" ", "_")
                aliases = {"lp": "lowpass", "hp": "highpass", "bp": "bandpass_csg",
                           "bandpass": "bandpass_csg", "low_pass": "lowpass", "high_pass": "highpass"}
                key = aliases.get(key, key)
                if key not in catalog.FILTER_TYPES:
                    raise ProjectError(f"Unknown filter type '{filter_type}'. "
                                       f"Available: {', '.join(catalog.FILTER_TYPES)}")
                index = catalog.FILTER_TYPES.index(key)
            if not 0 <= index < len(catalog.FILTER_TYPES):
                raise ProjectError("Filter type index out of range")
            model_set(eldata, "ftype", index)
        if cutoff is not None:
            if not 1 <= cutoff <= 14000:
                raise ProjectError("cutoff must be 1-14000 Hz")
            model_set(eldata, "fcut", cutoff)
        if resonance is not None:
            if not 0.01 <= resonance <= 10:
                raise ProjectError("resonance must be 0.01-10 (0.5 = neutral)")
            model_set(eldata, "fres", resonance)
        if enabled is None and any(v is not None for v in (filter_type, cutoff, resonance)):
            enabled = True
        if enabled is not None:
            model_set(eldata, "fwet", bool(enabled))
        self.modified = True
        return {k: v for k, v in eldata.attrib.items()}

    @staticmethod
    def _chord_index(chord) -> int:
        if isinstance(chord, int) and not isinstance(chord, bool):
            index = chord
            if not 0 <= index < len(catalog.CHORD_TABLE):
                raise ProjectError("Chord index out of range")
            return index
        lowered = [c.lower() for c in catalog.CHORD_TABLE]
        key = str(chord).strip().lower()
        key = {"maj": "major", "min": "minor", "m": "minor", "power": "5"}.get(key, key)
        if key not in lowered:
            raise ProjectError(f"Unknown chord '{chord}'. Available: {', '.join(catalog.CHORD_TABLE[:67])}")
        return lowered.index(key)

    def set_arpeggiator(self, track: ET.Element, *, enabled=True, chord=None, direction=None,
                        octaves=None, time_ms=None, gate=None, mode=None) -> dict:
        it = track.find("instrumenttrack")
        if it is None:
            raise ProjectError(f"Track '{track.get('name')}' is not an instrument track")
        arp = it.find("arpeggiator")
        if arp is None:
            arp = _sub(it, "arpeggiator", _ARP_DEFAULTS)
        model_set(arp, "arp-enabled", bool(enabled))
        if chord is not None:
            model_set(arp, "arp", self._chord_index(chord))
        if direction is not None:
            key = str(direction).lower().replace(" ", "_").replace("and_", "")
            if key not in catalog.ARP_DIRECTIONS:
                raise ProjectError(f"direction must be one of {catalog.ARP_DIRECTIONS}")
            model_set(arp, "arpdir", catalog.ARP_DIRECTIONS.index(key))
        if octaves is not None:
            if not 1 <= octaves <= 9:
                raise ProjectError("octaves must be 1-9")
            model_set(arp, "arprange", int(octaves))
        if time_ms is not None:
            if not 25 <= time_ms <= 2000:
                raise ProjectError("time_ms must be 25-2000")
            model_set(arp, "arptime", time_ms)
        if gate is not None:
            if not 1 <= gate <= 200:
                raise ProjectError("gate must be 1-200 (%)")
            model_set(arp, "arpgate", gate)
        if mode is not None:
            if str(mode).lower() not in catalog.ARP_MODES:
                raise ProjectError(f"mode must be one of {catalog.ARP_MODES}")
            model_set(arp, "arpmode", catalog.ARP_MODES.index(str(mode).lower()))
        self.modified = True
        return dict(arp.attrib)

    def set_chord_stacking(self, track: ET.Element, *, enabled=True, chord=None, octaves=None) -> dict:
        it = track.find("instrumenttrack")
        if it is None:
            raise ProjectError(f"Track '{track.get('name')}' is not an instrument track")
        cc = it.find("chordcreator")
        if cc is None:
            cc = _sub(it, "chordcreator", {"chord": 0, "chordrange": 1, "chord-enabled": 0})
        model_set(cc, "chord-enabled", bool(enabled))
        if chord is not None:
            model_set(cc, "chord", self._chord_index(chord))
        if octaves is not None:
            if not 1 <= octaves <= 9:
                raise ProjectError("octaves must be 1-9")
            model_set(cc, "chordrange", int(octaves))
        self.modified = True
        return dict(cc.attrib)

    # ------------------------------------------------------------------
    # clips and notes
    # ------------------------------------------------------------------
    def _clip_tags(self) -> tuple[str, ...]:
        return (self.d.tag("midi_clip"), self.d.tag("sample_clip"), self.d.tag("pattern_clip"),
                self.d.tag("automation_clip"))

    def _clips(self, track: ET.Element) -> list[ET.Element]:
        tags = self._clip_tags()
        clips = [c for c in track if c.tag in tags]
        return sorted(clips, key=lambda c: int(num(c.get("pos"))))

    def _all_midi_clip_lists(self):
        tag = self.d.tag("midi_clip")
        for track in self.tracks() + self.beat_tracks():
            if track.find("instrumenttrack") is not None:
                yield track.findall(tag)

    def _clip_len(self, clip: ET.Element) -> int:
        length = int(num(clip.get("len")))
        if length > 0:
            return length
        end = 0
        for note in clip.findall("note"):
            note_len = int(num(note.get("len")))
            end = max(end, int(num(note.get("pos"))) + (note_len if note_len > 0 else TICKS_PER_STEP))
        for point in clip.findall("time"):
            end = max(end, int(num(point.get("pos"))))
        return self._ceil_bar(end)

    def _new_midi_clip(self, track: ET.Element, pos: int, length: int, name: str = "") -> ET.Element:
        clip = _sub(track, self.d.tag("midi_clip"),
                    {"type": 1, "name": name, "muted": 0, "pos": pos, "steps": 16, "len": length})
        if self.d.modern:
            _set_all(clip, {"off": 0, "autoresize": 1})
        return clip

    def _new_beat_clip(self, track: ET.Element, index: int) -> ET.Element:
        clip = self._new_midi_clip(track, index * self.tpb, self.tpb)
        clip.set("type", "0")
        clip.set("steps", str(self.tpb // TICKS_PER_STEP))
        return clip

    def _note_element(self, event: NoteEvent, offset_ticks: int, key_offset: int, step: bool = False) -> ET.Element:
        key = event.pitch + key_offset
        if not 0 <= key <= self.d.max_key():
            raise ProjectError(f"Pitch {pitch_name(event.pitch)} is outside the range this LMMS "
                               f"version can store for this instrument")
        if not 0 <= event.velocity <= 200:
            raise ProjectError("velocity must be 0-200 (100 = normal)")
        if not -100 <= event.pan <= 100:
            raise ProjectError("pan must be -100..100")
        pos = offset_ticks + self.beats_to_ticks(event.start)
        if pos < 0:
            raise ProjectError("Notes cannot start before the clip start")
        length = max(1, self.beats_to_ticks(event.length))
        note = ET.Element("note")
        if step:
            if self.d.typed_notes:
                _set_all(note, {"key": key, "vol": event.velocity, "pan": event.pan,
                                "len": TICKS_PER_STEP, "pos": pos, "type": 1})
            else:
                _set_all(note, {"key": key, "vol": event.velocity, "pan": event.pan, "len": -192, "pos": pos})
        else:
            _set_all(note, {"key": key, "vol": event.velocity, "pan": event.pan, "len": length, "pos": pos})
            if self.d.typed_notes:
                note.set("type", "0")
        return note

    def _find_clip_at(self, track: ET.Element, tick: int) -> ET.Element | None:
        tag = self.d.tag("midi_clip")
        clips = [c for c in self._clips(track) if c.tag == tag]
        for clip in clips:
            if int(num(clip.get("pos"))) == tick:
                return clip
        for clip in clips:
            pos = int(num(clip.get("pos")))
            if pos < tick < pos + self._clip_len(clip):
                return clip
        return None

    def add_notes(self, track: ET.Element, events: list[NoteEvent], bar: float = 1,
                  replace: bool = False, clip_name: str | None = None,
                  length_bars: float | None = None) -> dict:
        """Add notes to a song instrument track; ``event.start`` counts from ``bar``."""
        if track.find("instrumenttrack") is None:
            raise ProjectError(f"Track '{track.get('name')}' is not an instrument track")
        if not events and length_bars is None:
            raise ProjectError("No notes given")
        tick = self.bar_to_tick(bar)
        clip = self._find_clip_at(track, tick)
        created = clip is None
        if clip is None:
            clip = self._new_midi_clip(track, tick, self.tpb, clip_name or "")
        elif clip_name is not None:
            clip.set("name", clip_name)
        clip_pos = int(num(clip.get("pos")))
        offset = tick - clip_pos
        key_offset = self.d.key_offset(self.instrument_name(track))
        new_notes = [self._note_element(e, offset, key_offset) for e in events]
        end = max((int(n.get("pos")) + int(n.get("len")) for n in new_notes), default=offset)
        if length_bars is not None:
            end = max(end, offset + int(round(length_bars * self.tpb)))
        if replace:
            start = min((int(n.get("pos")) for n in new_notes), default=offset)
            lo = offset + ((start - offset) // self.tpb) * self.tpb
            hi = self._ceil_bar(end)
            for note in clip.findall("note"):
                if lo <= int(num(note.get("pos"))) < hi:
                    clip.remove(note)
        clip.extend(new_notes)
        clip.set("type", "1")
        new_len = max(0 if created else self._clip_len(clip), self._ceil_bar(end))
        clip.set("len", str(new_len))
        self.modified = True
        result = {"clip_bar": self.tick_to_bar(clip_pos), "clip_length_bars": new_len / self.tpb,
                  "notes_added": len(new_notes), "notes_in_clip": len(clip.findall("note")),
                  "created_clip": created}
        overlap = [c for c in self._clips(track)
                   if c is not clip and clip_pos < int(num(c.get("pos"))) < clip_pos + new_len]
        if overlap:
            result["warning"] = (f"The clip now overlaps the clip starting at bar "
                                 f"{self.tick_to_bar(int(num(overlap[0].get('pos'))))}")
        return result

    def get_notes(self, track: ET.Element, from_bar: float | None = None,
                  length_bars: float | None = None, scope: str = "song",
                  pattern: int | None = None) -> list[dict]:
        key_offset = self.d.key_offset(self.instrument_name(track))
        lo = self.bar_to_tick(from_bar) if from_bar is not None else 0
        hi = lo + int(round(length_bars * self.tpb)) if length_bars is not None else None
        tag = self.d.tag("midi_clip")
        clips = [c for c in self._clips(track) if c.tag == tag]
        if scope != "song":
            clips = [clips[pattern]] if pattern is not None and pattern < len(clips) else []
        result = []
        for clip in clips:
            pos = 0 if scope != "song" else int(num(clip.get("pos")))
            notes = []
            for note in clip.findall("note"):
                start = pos + int(num(note.get("pos")))
                if start < lo or (hi is not None and start >= hi):
                    continue
                length = int(num(note.get("len")))
                step = length < 0 or note.get("type") == "1"
                notes.append({
                    "pitch": pitch_name(max(0, min(127, int(num(note.get("key"))) - key_offset))),
                    "start": round(int(num(note.get("pos"))) / TICKS_PER_BEAT, 4),
                    "length": 0.25 if step else round(length / TICKS_PER_BEAT, 4),
                    "velocity": int(num(note.get("vol"), 100)),
                })
            if notes or from_bar is None:
                entry = {"length_bars": round(self._clip_len(clip) / self.tpb, 3), "notes": notes}
                if scope == "song":
                    entry = {"bar": self.tick_to_bar(pos), **entry}
                if clip.get("name"):
                    entry["name"] = clip.get("name")
                result.append(entry)
        return result

    def _range(self, from_bar: float | None, length_bars: float | None) -> tuple[int, float]:
        lo = self.bar_to_tick(from_bar) if from_bar is not None else 0
        hi = lo + int(round(length_bars * self.tpb)) if length_bars is not None else float("inf")
        return lo, hi

    def transpose(self, track: ET.Element, semitones: int, from_bar=None, length_bars=None) -> int:
        lo, hi = self._range(from_bar, length_bars)
        tag = self.d.tag("midi_clip")
        changed = []
        for clip in self._clips(track):
            if clip.tag != tag:
                continue
            pos = int(num(clip.get("pos")))
            for note in clip.findall("note"):
                if lo <= pos + int(num(note.get("pos"))) < hi:
                    key = int(num(note.get("key"))) + semitones
                    if not 0 <= key <= self.d.max_key():
                        raise ProjectError("Transposing would move notes outside the playable range")
                    changed.append((note, key))
        for note, key in changed:
            note.set("key", str(key))
        self.modified = self.modified or bool(changed)
        return len(changed)

    def remove_notes(self, track: ET.Element, from_bar=None, length_bars=None) -> int:
        """Delete notes in a bar range (whole clips are removed when they become empty)."""
        lo, hi = self._range(from_bar, length_bars)
        tag = self.d.tag("midi_clip")
        removed = 0
        for clip in self._clips(track):
            if clip.tag != tag:
                continue
            pos = int(num(clip.get("pos")))
            for note in clip.findall("note"):
                if lo <= pos + int(num(note.get("pos"))) < hi:
                    clip.remove(note)
                    removed += 1
            if not clip.findall("note") and lo <= pos < hi:
                track.remove(clip)
        self.modified = True
        return removed

    def remove_clips(self, track: ET.Element, from_bar=None, length_bars=None) -> int:
        lo, hi = self._range(from_bar, length_bars)
        removed = 0
        for clip in self._clips(track):
            if lo <= int(num(clip.get("pos"))) < hi:
                track.remove(clip)
                removed += 1
        self.modified = True
        return removed

    def copy_section(self, from_bar: float, length_bars: float, to_bar: float, times: int = 1,
                     tracks: list[ET.Element] | None = None) -> int:
        """Copy every clip that starts inside a section to another position."""
        if times < 1 or times > 200:
            raise ProjectError("times must be 1-200")
        if length_bars <= 0:
            raise ProjectError("length_bars must be positive")
        lo = self.bar_to_tick(from_bar)
        span = int(round(length_bars * self.tpb))
        dest = self.bar_to_tick(to_bar)
        copied = 0
        for track in tracks if tracks is not None else self.tracks():
            source = [c for c in self._clips(track) if lo <= int(num(c.get("pos"))) < lo + span]
            for repeat in range(times):
                shift = dest - lo + repeat * span
                if shift == 0:
                    continue
                for clip in source:
                    clone = copy.deepcopy(clip)
                    clone.set("pos", str(int(num(clip.get("pos"))) + shift))
                    track.append(clone)
                    copied += 1
        self.modified = True
        return copied

    def song_length_ticks(self) -> int:
        end = 0
        for track in self.tracks():
            for clip in self._clips(track):
                end = max(end, int(num(clip.get("pos"))) + self._clip_len(clip))
        return end

    # ------------------------------------------------------------------
    # beat / pattern editor
    # ------------------------------------------------------------------
    def find_pattern(self, ref) -> int:
        patterns = self._pattern_tracks()
        listing = ", ".join(f"{i}:{t.get('name', '')}" for i, t in enumerate(patterns)) or "(none)"
        if isinstance(ref, bool) or ref is None:
            raise ProjectError(f"Invalid pattern reference {ref!r}")
        if isinstance(ref, int) or re.fullmatch(r"\d+", str(ref).strip()):
            index = int(ref)
            if not 0 <= index < len(patterns):
                raise ProjectError(f"No pattern with index {index}. Patterns: {listing}")
            return index
        wanted = str(ref).strip().lower()
        for i, t in enumerate(patterns):
            if t.get("name", "").lower() == wanted:
                return i
        raise ProjectError(f"No pattern named '{ref}'. Patterns: {listing}")

    def add_pattern(self, name: str) -> int:
        """Add a pattern (a row of the beat editor + a pattern track in the song)."""
        names = {t.get("name", "").lower() for t in self._pattern_tracks()}
        if name.lower() in names:
            raise ProjectError(f"A pattern named '{name}' already exists")
        track = _sub(self.container, "track", {"type": 1, "name": name, "muted": 0, "solo": 0})
        ET.SubElement(track, self.d.tag("pattern_track"))
        index = len(self._pattern_tracks()) - 1
        for inner in self.beat_tracks():
            if len(self._clips(inner)) <= index:
                self._new_beat_clip(inner, index)
        self.modified = True
        return index

    def duplicate_pattern(self, index: int, name: str) -> int:
        new_index = self.add_pattern(name)
        for inner in self.beat_tracks():
            clips = self._clips(inner)
            source, target = clips[index], clips[new_index]
            for key in ("steps", "len", "type"):
                target.set(key, source.get(key, target.get(key, "")))
            target[:] = [copy.deepcopy(n) for n in source]
        return new_index

    def _remove_pattern(self, track: ET.Element) -> None:
        patterns = self._pattern_tracks()
        index = patterns.index(track)
        store = self._store()
        if store is not None and len(patterns) > 1:
            holder = track.find(self.d.tag("pattern_track"))
            if holder is not None and store in list(holder):
                holder.remove(store)
                other = patterns[1 if index == 0 else 0]
                target = other.find(self.d.tag("pattern_track"))
                if target is None:
                    target = ET.Element(self.d.tag("pattern_track"))
                    other.insert(0, target)
                target.append(store)
            for inner in store.findall("track"):
                clips = self._clips(inner)
                if index < len(clips):
                    inner.remove(clips[index])
                for new_index, clip in enumerate(self._clips(inner)):
                    clip.set("pos", str(new_index * self.tpb))
        self.container.remove(track)

    def _beat_clip(self, inner: ET.Element, pattern: int) -> ET.Element:
        clips = self._clips(inner)
        while len(clips) <= pattern:
            self._new_beat_clip(inner, len(clips))
            clips = self._clips(inner)
        return clips[pattern]

    def set_steps(self, pattern: int, inner: ET.Element, velocities: list[int],
                  pitch: int | None = None) -> None:
        """Replace the content of one instrument row of a pattern by step notes."""
        clip = self._beat_clip(inner, pattern)
        for note in clip.findall("note"):
            clip.remove(note)
        key_offset = self.d.key_offset(self.instrument_name(inner))
        midi = 69 if pitch is None else pitch
        for i, velocity in enumerate(velocities):
            if velocity > 0:
                event = NoteEvent(midi, i * 0.25, 0.25, velocity)
                clip.append(self._note_element(event, 0, key_offset, step=True))
        steps_per_bar = max(1, self.tpb // TICKS_PER_STEP)
        steps = math.ceil(len(velocities) / steps_per_bar) * steps_per_bar
        clip.set("steps", str(steps))
        clip.set("type", "0")
        clip.set("len", str(steps // steps_per_bar * self.tpb))
        self.modified = True

    def add_beat_notes(self, pattern: int, inner: ET.Element, events: list[NoteEvent],
                       replace: bool = False) -> dict:
        """Add melodic notes (e.g. a bassline) to an instrument row of a pattern."""
        clip = self._beat_clip(inner, pattern)
        if replace:
            for note in clip.findall("note"):
                clip.remove(note)
        key_offset = self.d.key_offset(self.instrument_name(inner))
        notes = [self._note_element(e, 0, key_offset) for e in events]
        clip.extend(notes)
        end = max((int(n.get("pos")) + max(int(n.get("len")), TICKS_PER_STEP)
                   for n in clip.findall("note")), default=self.tpb)
        length = self._ceil_bar(end)
        clip.set("len", str(length))
        clip.set("steps", str(length // TICKS_PER_STEP))
        clip.set("type", "1")
        self.modified = True
        return {"notes_added": len(notes), "pattern_length_bars": length / self.tpb}

    def pattern_length_bars(self, pattern: int) -> float:
        longest = self.tpb
        for inner in self.beat_tracks():
            clips = self._clips(inner)
            if pattern < len(clips) and clips[pattern].findall("note"):
                longest = max(longest, self._clip_len(clips[pattern]))
        return longest / self.tpb

    def place_pattern(self, pattern: int, bar: float, length_bars: float) -> None:
        if length_bars <= 0:
            raise ProjectError("length_bars must be positive")
        track = self._pattern_tracks()[pattern]
        pos = self.bar_to_tick(bar)
        clip = _sub(track, self.d.tag("pattern_clip"),
                    {"pos": pos, "len": int(round(length_bars * self.tpb)), "name": "", "muted": 0})
        if self.d.modern:
            _set_all(clip, {"off": 0, "autoresize": 0})
        self.modified = True

    # ------------------------------------------------------------------
    # sample clips
    # ------------------------------------------------------------------
    def add_sample_clip(self, track: ET.Element, src: str, bar: float, length_ticks: int) -> None:
        if track.find("sampletrack") is None:
            raise ProjectError(f"Track '{track.get('name')}' is not a sample track")
        clip = _sub(track, self.d.tag("sample_clip"),
                    {"pos": self.bar_to_tick(bar), "len": max(1, length_ticks), "muted": 0, "src": src})
        if self.d.modern:
            _set_all(clip, {"off": 0, "autoresize": 0})
        self.modified = True

    # ------------------------------------------------------------------
    # mixer
    # ------------------------------------------------------------------
    def channels(self) -> list[ET.Element]:
        chans = self.mixer.findall(self.d.tag("mixer_channel"))
        return sorted(chans, key=lambda c: int(num(c.get("num"))))

    def channel(self, number: int) -> ET.Element:
        for ch in self.channels():
            if int(num(ch.get("num"))) == int(number):
                return ch
        raise ProjectError(f"No mixer channel {number}. Channels: "
                           + ", ".join(f"{int(num(c.get('num')))}:{c.get('name', '')}" for c in self.channels()))

    def _check_channel(self, number) -> None:
        if isinstance(number, bool) or not isinstance(number, int):
            raise ProjectError("mixer_channel must be a channel number (0 = master)")
        self.channel(number)

    def add_channel(self, name: str, volume: float = 100) -> int:
        if not 0 <= volume <= 200:
            raise ProjectError("Mixer volume must be 0-200 (100 = 0 dB)")
        number = max(int(num(c.get("num"))) for c in self.channels()) + 1
        if number > 63 and not self.d.extended_keys:
            raise ProjectError("LMMS 1.2 supports at most 64 mixer channels")
        ch = _sub(self.mixer, self.d.tag("mixer_channel"),
                  {"num": number, "name": name, "volume": volume / 100, "muted": 0, "soloed": 0})
        _sub(ch, "fxchain", {"numofeffects": 0, "enabled": 0})
        _sub(ch, "send", {"channel": 0, "amount": 1})
        self.modified = True
        return number

    def update_channel(self, number: int, *, name=None, volume=None, muted=None, solo=None) -> None:
        ch = self.channel(number)
        if name is not None:
            ch.set("name", str(name))
        if volume is not None:
            if not 0 <= volume <= 200:
                raise ProjectError("Mixer volume must be 0-200 (100 = 0 dB)")
            model_set(ch, "volume", volume / 100)
        if muted is not None:
            model_set(ch, "muted", bool(muted))
        if solo is not None:
            model_set(ch, "soloed", bool(solo))
        self.modified = True

    def set_send(self, source: int, target: int, amount: float | None) -> None:
        """Create/update (amount in %) or remove (amount None/0) a send between channels."""
        if source == 0:
            raise ProjectError("The master channel cannot send anywhere")
        if source == target:
            raise ProjectError("A channel cannot send to itself")
        ch = self.channel(source)
        self.channel(target)
        if target > source and target != 0:
            # LMMS processes channels in dependency order but forbids loops.
            back = self.channel(target)
            if any(int(num(s.get("channel"))) == source for s in back.findall("send")):
                raise ProjectError("That send would create a feedback loop")
        existing = next((s for s in ch.findall("send") if int(num(s.get("channel"))) == target), None)
        if not amount:
            if existing is not None:
                ch.remove(existing)
        else:
            if not 0 < amount <= 100:
                raise ProjectError("Send amount must be 0-100 (%)")
            if existing is None:
                existing = _sub(ch, "send", {"channel": target})
            model_set(existing, "amount", amount / 100)
        self.modified = True

    def remove_channel(self, number: int) -> None:
        if number == 0:
            raise ProjectError("The master channel cannot be removed")
        self.mixer.remove(self.channel(number))
        for ch in self.channels():
            n = int(num(ch.get("num")))
            if n > number:
                ch.set("num", str(n - 1))
            for send in ch.findall("send"):
                target = int(num(send.get("channel")))
                if target == number:
                    ch.remove(send)
                elif target > number:
                    send.set("channel", str(target - 1))
            if int(num(ch.get("num"))) != 0 and not ch.findall("send"):
                _sub(ch, "send", {"channel": 0, "amount": 1})
        attr = self.d.mixer_attr
        for track in self.tracks() + self.beat_tracks():
            settings = self._settings(track)
            if settings is None:
                continue
            current = int(num(model_get(settings, attr, 0)))
            if current == number:
                model_set(settings, attr, 0)
            elif current > number:
                model_set(settings, attr, current - 1)
        self.modified = True

    # ------------------------------------------------------------------
    # effects
    # ------------------------------------------------------------------
    def chain(self, *, track: ET.Element | None = None, channel: int | None = None) -> ET.Element:
        if track is not None:
            owner = self._settings(track)
            if owner is None:
                raise ProjectError(f"Track '{track.get('name')}' cannot hold effects "
                                   f"(only instrument and sample tracks can)")
        else:
            owner = self.channel(0 if channel is None else channel)
        chain = owner.find("fxchain")
        if chain is None:
            chain = _sub(owner, "fxchain", {"numofeffects": 0, "enabled": 0})
        return chain

    def add_effect(self, chain: ET.Element, effect: str, params: dict | None = None,
                   wet: float = 1.0, position: int | None = None) -> tuple[int, list[str]]:
        name = effect.strip().lower()
        spec = catalog.EFFECTS.get(name)
        if spec is None:
            raise ProjectError(f"Unknown effect '{effect}'. Available: {', '.join(sorted(catalog.EFFECTS))}")
        if not -1 <= wet <= 1:
            raise ProjectError("wet must be between -1 and 1 (1 = fully wet)")
        warnings = []
        if spec.get("min_version") == "1.3" and not self.d.extended_keys:
            warnings.append(f"'{name}' only exists in LMMS 1.3+; LMMS 1.2 will show a missing-plugin dummy")
        el = ET.Element("effect")
        _set_all(el, {"name": name, "on": 1, "wet": wet, "autoquit": 1, "gate": 0})
        ET.SubElement(el, spec["node"])
        ET.SubElement(el, "key")
        effects = chain.findall("effect")
        index = len(effects) if position is None else max(0, min(position, len(effects)))
        if index == len(effects):
            chain.append(el)
        else:
            chain.insert(list(chain).index(effects[index]), el)
        model_set(chain, "enabled", True)
        chain.set("numofeffects", str(len(effects) + 1))
        if params:
            warnings += self.update_effect(chain, index, params=params)
        self.modified = True
        return index, warnings

    def _effect(self, chain: ET.Element, index: int) -> ET.Element:
        effects = chain.findall("effect")
        if not 0 <= index < len(effects):
            raise ProjectError(f"No effect at position {index} (the chain has {len(effects)} effects)")
        return effects[index]

    @staticmethod
    def _controls(effect: ET.Element) -> ET.Element:
        node = next((c for c in effect if c.tag not in ("key", "connection") and "value" not in c.attrib), None)
        if node is None:
            spec = catalog.EFFECTS.get(effect.get("name", ""))
            node = ET.Element(spec["node"] if spec else "controls")
            effect.insert(0, node)
        return node

    def update_effect(self, chain: ET.Element, index: int, *, params: dict | None = None,
                      wet: float | None = None, enabled: bool | None = None) -> list[str]:
        effect = self._effect(chain, index)
        warnings = []
        if wet is not None:
            if not -1 <= wet <= 1:
                raise ProjectError("wet must be between -1 and 1")
            model_set(effect, "wet", wet)
        if enabled is not None:
            model_set(effect, "on", bool(enabled))
        if params:
            controls = self._controls(effect)
            spec = catalog.EFFECTS.get(effect.get("name", ""), {})
            known = set()
            for key in spec.get("params", {}):
                for part in key.split(","):
                    part = part.strip()
                    m = re.fullmatch(r"(\w+?)(\d)\.\.(\d)", part)
                    known |= {f"{m.group(1)}{i}" for i in range(int(m.group(2)), int(m.group(3)) + 1)} if m else {part}
            for key, value in params.items():
                if isinstance(value, (dict, list)):
                    raise ProjectError(f"Effect parameter '{key}' must be a number")
                if known and key not in known and key not in controls.attrib:
                    warnings.append(f"'{key}' is not a documented {effect.get('name')} parameter (written anyway)")
                if key in spec.get("params", {}) and isinstance(value, (int, float)):
                    _d, lo, hi, _t = spec["params"][key]
                    if lo is not None and not lo <= value <= hi:
                        raise ProjectError(f"{effect.get('name')}.{key} must be {lo}..{hi}")
                model_set(controls, key, value)
        self.modified = True
        return warnings

    def remove_effect(self, chain: ET.Element, index: int) -> str:
        effect = self._effect(chain, index)
        chain.remove(effect)
        remaining = len(chain.findall("effect"))
        chain.set("numofeffects", str(remaining))
        if remaining == 0:
            model_set(chain, "enabled", False)
        self.modified = True
        return effect.get("name", "")

    def describe_chain(self, chain: ET.Element | None) -> list[dict]:
        if chain is None:
            return []
        result = []
        for index, effect in enumerate(chain.findall("effect")):
            entry = {"index": index, "effect": effect.get("name"),
                     "wet": num(model_get(effect, "wet", 1), 1),
                     "enabled": num(model_get(effect, "on", 1), 1) != 0}
            controls = self._controls(effect)
            if effect.get("name") != "ladspaeffect" and controls.attrib:
                entry["params"] = {k: v for k, v in controls.attrib.items() if len(v) < 40}
            result.append(entry)
        return result

    # ------------------------------------------------------------------
    # automation and controllers
    # ------------------------------------------------------------------
    def resolve_parameter(self, parameter: str, *, track: ET.Element | None = None,
                          channel: int | None = None, effect_index: int | None = None
                          ) -> tuple[ET.Element, str, str, float]:
        """Find the XML home of an automatable value.

        Returns (element, attribute name, human label, scale) where ``scale``
        converts the user-facing value to the stored one.
        """
        key = parameter.strip()
        low = key.lower()
        if effect_index is not None:
            chain = self.chain(track=track, channel=channel)
            effect = self._effect(chain, effect_index)
            owner = track.get("name") if track is not None else f"Mixer {0 if channel is None else channel}"
            if low in ("wet", "wet/dry"):
                return effect, "wet", f"{owner} > {effect.get('name')} > wet", 1.0
            return self._controls(effect), key, f"{owner} > {effect.get('name')} > {key}", 1.0
        if track is not None:
            settings = self._settings(track)
            if settings is None:
                raise ProjectError(f"Track '{track.get('name')}' has no automatable settings")
            name = track.get("name", "")
            simple = {"volume": "vol", "vol": "vol", "pan": "pan", "panning": "pan"}
            if low in simple:
                return settings, simple[low], f"{name} > {low}", 1.0
            if settings.tag != "instrumenttrack":
                raise ProjectError("Sample tracks only have 'volume' and 'pan'")
            if low == "pitch":
                return settings, "pitch", f"{name} > pitch", 1.0
            if low in ("cutoff", "filter_cutoff", "fcut"):
                return self._eldata(track), "fcut", f"{name} > cutoff", 1.0
            if low in ("resonance", "filter_resonance", "fres"):
                return self._eldata(track), "fres", f"{name} > resonance", 1.0
            node = self._plugin_node(track)
            if node.attrib and key not in node.attrib and _model_node(node, key) is None \
                    and self.instrument_name(track) in catalog.INSTRUMENTS \
                    and catalog.INSTRUMENTS[self.instrument_name(track)]["defaults"]:
                raise ProjectError(
                    f"'{key}' is not a parameter of track '{name}'. Use volume, pan, pitch, cutoff, "
                    f"resonance or an instrument parameter: {', '.join(sorted(node.attrib))}")
            return node, key, f"{name} > {key}", 1.0
        if channel is not None:
            if low not in ("volume", "vol"):
                raise ProjectError("Mixer channels only have 'volume' (use effect_index for effect parameters)")
            ch = self.channel(channel)
            return ch, "volume", f"Mixer {ch.get('name', channel)} > volume", 0.01
        song = {"tempo": ("bpm", "Tempo"), "bpm": ("bpm", "Tempo"),
                "master_volume": ("mastervol", "Master volume"),
                "master_pitch": ("masterpitch", "Master pitch")}
        if low not in song:
            raise ProjectError("Without track/mixer_channel the parameter must be tempo, "
                               "master_volume or master_pitch")
        return self.head, song[low][0], song[low][1], 1.0

    def _used_ids(self) -> set[int]:
        return {int(num(el.get("id"))) for el in self.root.iter() if "id" in el.attrib and el.tag != "PART"}

    def _model_id(self, element: ET.Element, name: str) -> int:
        """Make sure a value is stored as an identifiable model node and return its id."""
        if not re.fullmatch(r"[A-Za-z_][\w.\-]*", name) or name[0].isdigit():
            raise ProjectError(f"Parameter '{name}' cannot be automated")
        node = _model_node(element, name)
        if node is not None and int(num(node.get("id"))) > 0:
            return int(num(node.get("id")))
        used = self._used_ids()
        new_id = random.randint(1, 16_777_215)
        while new_id in used:
            new_id = random.randint(1, 16_777_215)
        if node is None:
            value = element.attrib.pop(name, None)
            if value is None:
                raise ProjectError(f"Parameter '{name}' has no value yet - set it first")
            node = ET.SubElement(element, name, {"value": value, "scale_type": "linear"})
        node.set("id", str(new_id))
        return new_id

    def add_automation(self, element: ET.Element, attr: str, label: str,
                       points: list[tuple[float, float]], progression: str = "linear",
                       scale: float = 1.0) -> dict:
        """Automate a value. ``points`` are (bar, value) pairs on the song timeline."""
        modes = {"discrete": 0, "step": 0, "linear": 1, "smooth": 2, "cubic": 2}
        if progression.lower() not in modes:
            raise ProjectError("progression must be 'discrete', 'linear' or 'smooth'")
        if not points:
            raise ProjectError("At least one automation point is required")
        points = sorted(points)
        if element.get(attr) is None and _model_node(element, attr) is None:
            element.set(attr, fmt(points[0][1] * scale))
        model_id = self._model_id(element, attr)
        first = int(math.floor(points[0][0] - 1 + 1e-9)) * self.tpb
        last_tick = self.bar_to_tick(points[-1][0])
        track = self._automation_track(label)
        clip = _sub(track, self.d.tag("automation_clip"), {
            "pos": first, "len": self._ceil_bar(max(last_tick - first, 1)), "name": label,
            "prog": modes[progression.lower()], "tens": 1, "mute": 0})
        if self.d.modern:
            _set_all(clip, {"off": 0, "autoresize": 0})
        for bar, value in points:
            point = _sub(clip, "time", {"pos": self.bar_to_tick(bar) - first, "value": value * scale})
            if self.d.automation_out_values:
                point.set("outValue", fmt(value * scale))
        _sub(clip, "object", {"id": model_id})
        self.modified = True
        return {"automation_track": label, "clip_bar": self.tick_to_bar(first),
                "length_bars": int(clip.get("len")) / self.tpb, "points": len(points)}

    def controllers(self) -> ET.Element:
        node = self.song.find("controllers")
        if node is None:
            node = ET.SubElement(self.song, "controllers")
        return node

    def add_lfo_controller(self, name: str, *, speed: float = 2.0, amount: float = 1.0,
                           base: float = 0.5, wave: str = "sine", phase: float = 0.0) -> int:
        waves = ["sine", "triangle", "saw", "square", "moog_saw", "exponential", "noise"]
        if wave.lower() not in waves:
            raise ProjectError(f"wave must be one of {waves}")
        if not 0.01 <= speed <= 20:
            raise ProjectError("speed is the LFO period in seconds and must be 0.01-20")
        if not -1 <= amount <= 1 or not 0 <= base <= 1:
            raise ProjectError("amount must be -1..1 and base 0..1")
        controllers = self.controllers()
        _sub(controllers, "lfocontroller", {
            "type": 1, "name": name, "base": base, "speed": speed, "amount": amount,
            "phase": phase, "wave": waves.index(wave.lower()), "multiplier": 0, "userwavefile": ""})
        self.modified = True
        return len(list(controllers)) - 1

    def connect_controller(self, element: ET.Element, attr: str, controller: int) -> None:
        count = len(list(self.controllers()))
        if not 0 <= controller < count:
            raise ProjectError(f"No controller {controller} (the project has {count})")
        if not re.fullmatch(r"[A-Za-z_]\w*", attr):
            raise ProjectError(f"Parameter '{attr}' cannot be connected to a controller")
        if element.get(attr) is None and _model_node(element, attr) is None:
            raise ProjectError(f"Parameter '{attr}' has no value yet - set it first")
        connection = element.find("connection")
        if connection is None:
            connection = ET.SubElement(element, "connection")
        for old in connection.findall(attr):
            connection.remove(old)
        _sub(connection, attr, {"id": controller})
        self.modified = True

    # ------------------------------------------------------------------
    # inspection
    # ------------------------------------------------------------------
    def describe_track(self, track: ET.Element, index: int, scope: str = "song", detail: bool = False) -> dict:
        kind = self.track_type(track)
        info: dict = {"index": index, "name": track.get("name", ""), "type": kind}
        if num(model_get(track, "muted", 0)):
            info["muted"] = True
        if num(model_get(track, "solo", 0)):
            info["solo"] = True
        settings = self._settings(track)
        if settings is not None:
            if kind == "instrument":
                info["instrument"] = self.instrument_name(track)
                node = self._plugin_node(track)
                if node.get("src"):
                    info["sample"] = node.get("src")
            info["volume"] = num(model_get(settings, "vol", 100), 100)
            info["pan"] = num(model_get(settings, "pan", 0))
            info["mixer_channel"] = int(num(model_get(settings, self.d.mixer_attr, 0)))
            effects = self.describe_chain(settings.find("fxchain"))
            if effects:
                info["effects"] = effects if detail else [e["effect"] for e in effects]
            if detail and kind == "instrument":
                node = self._plugin_node(track)
                info["instrument_params"] = {k: v for k, v in node.attrib.items() if len(v) < 40}
                eldata = settings.find("eldata")
                if eldata is not None and num(model_get(eldata, "fwet", 0)):
                    info["filter"] = {
                        "type": catalog.FILTER_TYPES[int(num(model_get(eldata, "ftype", 0))) % len(catalog.FILTER_TYPES)],
                        "cutoff": num(model_get(eldata, "fcut", 14000)),
                        "resonance": num(model_get(eldata, "fres", 0.5))}
        if scope != "song":
            return info
        clips = self._clips(track)
        if kind == "pattern":
            info["pattern_index"] = self._pattern_tracks().index(track)
            info["length_bars"] = self.pattern_length_bars(info["pattern_index"])
        entries = []
        for clip in clips:
            entry = {"bar": self.tick_to_bar(int(num(clip.get("pos")))),
                     "length_bars": round(self._clip_len(clip) / self.tpb, 3)}
            if clip.tag == self.d.tag("midi_clip"):
                entry["notes"] = len(clip.findall("note"))
            if clip.get("src"):
                entry["sample"] = clip.get("src")
            if clip.get("name"):
                entry["name"] = clip.get("name")
            if clip.tag == self.d.tag("automation_clip"):
                entry["points"] = len(clip.findall("time"))
            entries.append(entry)
        if entries and not detail and len(entries) > 24:
            info["clips"] = entries[:24]
            info["more_clips"] = len(entries) - 24
        elif entries:
            info["clips"] = entries
        return info

    def summary(self, detail: bool = False) -> dict:
        length = self.song_length_ticks()
        bars = math.ceil(length / self.tpb) if length else 0
        seconds = length / TICKS_PER_BEAT * 60 / self.tempo if self.tempo else 0
        info: dict = {
            "file": str(self.path) if self.path else None,
            "unsaved_changes": self.modified,
            "lmms_format": self.d.label,
            "tempo": self.tempo,
            "time_signature": f"{self.numerator}/{self.denominator}",
            "master_volume": num(model_get(self.head, "mastervol", 100), 100),
            "master_pitch": num(model_get(self.head, "masterpitch", 0)),
            "length_bars": bars,
            "length_seconds": round(seconds, 1),
            "tracks": [self.describe_track(t, i, detail=detail) for i, t in enumerate(self.tracks())],
        }
        beat = self.beat_tracks()
        if beat:
            patterns = self._pattern_tracks()
            rows = []
            for i, inner in enumerate(beat):
                row = self.describe_track(inner, i, scope="beat", detail=detail)
                steps = {}
                for p, clip in enumerate(self._clips(inner)[:len(patterns)]):
                    notes = clip.findall("note")
                    if not notes:
                        continue
                    count = max(int(num(clip.get("steps"), 16)), 1)
                    if all(int(num(n.get("len"))) < 0 or n.get("type") == "1" for n in notes):
                        grid = ["."] * count
                        for n in notes:
                            i_step = int(num(n.get("pos"))) // TICKS_PER_STEP
                            if i_step < count:
                                vol = num(n.get("vol"), 100)
                                grid[i_step] = "X" if vol > 115 else "o" if vol < 70 else "x"
                        steps[patterns[p].get("name", str(p))] = "".join(grid)
                    else:
                        steps[patterns[p].get("name", str(p))] = f"{len(notes)} notes"
                if steps:
                    row["patterns"] = steps
                rows.append(row)
            info["beat_instruments"] = rows
        chans = []
        for ch in self.channels():
            entry = {"channel": int(num(ch.get("num"))), "name": ch.get("name", ""),
                     "volume": round(num(model_get(ch, "volume", 1), 1) * 100, 1)}
            if num(model_get(ch, "muted", 0)):
                entry["muted"] = True
            sends = {int(num(s.get("channel"))): round(num(model_get(s, "amount", 1), 1) * 100, 1)
                     for s in ch.findall("send")}
            if sends and sends != {0: 100.0}:
                entry["sends"] = sends
            effects = self.describe_chain(ch.find("fxchain"))
            if effects:
                entry["effects"] = effects if detail else [e["effect"] for e in effects]
            chans.append(entry)
        info["mixer"] = chans
        controllers = [{"index": i, "type": c.tag, "name": c.get("name", "")}
                       for i, c in enumerate(self.controllers())]
        if controllers:
            info["controllers"] = controllers
        return info

    def validate(self, sample_exists=None) -> list[str]:
        """Return human readable problems that would make the song sound wrong in LMMS."""
        issues = []
        channel_numbers = {int(num(c.get("num"))) for c in self.channels()}
        tracks = [(t, "track") for t in self.tracks()] + [(t, "beat instrument") for t in self.beat_tracks()]
        if not self.tracks():
            issues.append("The project has no tracks.")
        has_sound = False
        for track, what in tracks:
            name = track.get("name", "")
            settings = self._settings(track)
            if settings is not None:
                channel = int(num(model_get(settings, self.d.mixer_attr, 0)))
                if channel not in channel_numbers:
                    issues.append(f"{what} '{name}' is routed to missing mixer channel {channel}.")
            if track.find("instrumenttrack") is not None:
                instrument = self.instrument_name(track)
                node = self._plugin_node(track)
                spec = catalog.INSTRUMENTS.get(instrument or "")
                if instrument in ("audiofileprocessor", "sf2player") and not node.get("src"):
                    issues.append(f"{what} '{name}' ({instrument}) has no sample/soundfont file - it will be silent.")
                elif node.get("src") and sample_exists and not sample_exists(node.get("src")):
                    issues.append(f"{what} '{name}': file '{node.get('src')}' was not found.")
                if spec and spec.get("needs_preset") and not node.attrib and not len(node):
                    issues.append(f"{what} '{name}' ({instrument}) has no preset loaded - it will be silent.")
                if spec and spec.get("min_version") == "1.3" and not self.d.extended_keys:
                    issues.append(f"{what} '{name}' uses {instrument}, which needs LMMS 1.3.")
                notes = sum(len(c.findall("note")) for c in self._clips(track))
                has_sound = has_sound or notes > 0
                if notes == 0 and what == "track":
                    issues.append(f"track '{name}' has no notes.")
                for clip in self._clips(track):
                    length = self._clip_len(clip)
                    for note in clip.findall("note"):
                        if int(num(note.get("pos"))) >= length:
                            issues.append(f"{what} '{name}': a note lies beyond the end of its clip "
                                          f"(bar {self.tick_to_bar(int(num(clip.get('pos'))))}).")
                            break
            if self.track_type(track) == "sample":
                clips = self._clips(track)
                has_sound = has_sound or bool(clips)
                for clip in clips:
                    if clip.get("src") and sample_exists and not sample_exists(clip.get("src")):
                        issues.append(f"sample track '{name}': file '{clip.get('src')}' was not found.")
        patterns = self._pattern_tracks()
        for index, track in enumerate(patterns):
            used = any(c.findall("note") for inner in self.beat_tracks()
                       for c in self._clips(inner)[index:index + 1])
            placed = bool(self._clips(track))
            if used and not placed:
                issues.append(f"pattern '{track.get('name')}' has notes but is not placed in the song "
                              f"(use arrange_pattern).")
            if placed and not used:
                issues.append(f"pattern '{track.get('name')}' is placed in the song but is empty.")
        if not has_sound and self.tracks():
            issues.append("Nothing would be audible: no notes or sample clips yet.")
        for chain in self.root.iter("fxchain"):
            if chain.findall("effect") and not num(model_get(chain, "enabled", 0)):
                issues.append("An effect chain contains effects but is disabled.")
        return issues
