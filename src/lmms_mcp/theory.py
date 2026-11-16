"""Music theory helpers: pitches, scales, chords and the compact note notation.

All times in this module are in *beats* (quarter notes). Pitches are MIDI note
numbers (C4 = 60, A4 = 69), which is also what LMMS displays in its piano roll.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_LETTER = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_PITCH_RE = re.compile(r"^([A-Ga-g])([#b♯♭]*)(-?\d+)$")
_ROOT_RE = re.compile(r"^([A-G])([#b♯♭]*)")

SCALES: dict[str, list[int]] = {
    "major": [0, 2, 4, 5, 7, 9, 11],
    "minor": [0, 2, 3, 5, 7, 8, 10],
    "harmonic_minor": [0, 2, 3, 5, 7, 8, 11],
    "melodic_minor": [0, 2, 3, 5, 7, 9, 11],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "phrygian": [0, 1, 3, 5, 7, 8, 10],
    "lydian": [0, 2, 4, 6, 7, 9, 11],
    "mixolydian": [0, 2, 4, 5, 7, 9, 10],
    "locrian": [0, 1, 3, 5, 6, 8, 10],
    "major_pentatonic": [0, 2, 4, 7, 9],
    "minor_pentatonic": [0, 3, 5, 7, 10],
    "blues": [0, 3, 5, 6, 7, 10],
    "whole_tone": [0, 2, 4, 6, 8, 10],
    "chromatic": list(range(12)),
    "phrygian_dominant": [0, 1, 4, 5, 7, 8, 10],
    "hungarian_minor": [0, 2, 3, 6, 7, 8, 11],
    "japanese": [0, 1, 5, 7, 8],
}
_SCALE_ALIASES = {
    "ionian": "major", "aeolian": "minor", "natural_minor": "minor",
    "pentatonic": "major_pentatonic", "min": "minor", "maj": "major",
    "m": "minor",
}

# Chord quality -> semitone offsets from the root.
CHORDS: dict[str, list[int]] = {
    "": [0, 4, 7], "maj": [0, 4, 7], "M": [0, 4, 7],
    "m": [0, 3, 7], "min": [0, 3, 7], "-": [0, 3, 7],
    "dim": [0, 3, 6], "aug": [0, 4, 8], "+": [0, 4, 8],
    "5": [0, 7], "sus2": [0, 2, 7], "sus4": [0, 5, 7], "sus": [0, 5, 7],
    "6": [0, 4, 7, 9], "m6": [0, 3, 7, 9],
    "7": [0, 4, 7, 10], "maj7": [0, 4, 7, 11], "M7": [0, 4, 7, 11],
    "m7": [0, 3, 7, 10], "min7": [0, 3, 7, 10], "mmaj7": [0, 3, 7, 11],
    "dim7": [0, 3, 6, 9], "m7b5": [0, 3, 6, 10], "7sus4": [0, 5, 7, 10],
    "9": [0, 4, 7, 10, 14], "maj9": [0, 4, 7, 11, 14], "m9": [0, 3, 7, 10, 14],
    "add9": [0, 4, 7, 14], "madd9": [0, 3, 7, 14],
    "11": [0, 4, 7, 10, 14, 17], "m11": [0, 3, 7, 10, 14, 17],
    "13": [0, 4, 7, 10, 14, 21], "7b9": [0, 4, 7, 10, 13], "7#9": [0, 4, 7, 10, 15],
}

_DURATION_LETTERS = {"w": 4.0, "h": 2.0, "q": 1.0, "e": 0.5, "s": 0.25, "t": 0.125}


def _accidental(acc: str) -> int:
    return sum(1 if c in "#♯" else -1 for c in acc)


def pitch_class(name: str) -> int:
    """Pitch class (0-11) of a note name without octave, e.g. 'F#' -> 6."""
    m = _ROOT_RE.match(name.strip()[:1].upper() + name.strip()[1:])
    if not m or m.end() != len(name.strip()):
        raise ValueError(f"Invalid note name '{name}' (expected e.g. C, F#, Bb)")
    return (_LETTER[m.group(1)] + _accidental(m.group(2))) % 12


def parse_pitch(value) -> int:
    """Convert 'C4', 'f#3', 'Bb2' or a MIDI number to a MIDI note number."""
    if isinstance(value, bool):
        raise ValueError(f"Invalid pitch: {value!r}")
    if isinstance(value, (int, float)):
        midi = int(round(value))
    else:
        text = str(value).strip()
        if re.fullmatch(r"-?\d+", text):
            midi = int(text)
        else:
            m = _PITCH_RE.match(text)
            if not m:
                raise ValueError(
                    f"Invalid pitch '{value}'. Use a note name with octave such as "
                    f"C4, F#3, Bb2 (C4 = MIDI 60) or a MIDI number 0-127."
                )
            midi = (int(m.group(3)) + 1) * 12 + _LETTER[m.group(1).upper()] + _accidental(m.group(2))
    if not 0 <= midi <= 127:
        raise ValueError(f"Pitch {value!r} is outside the MIDI range 0-127")
    return midi


def pitch_name(midi: int) -> str:
    """MIDI note number -> name, e.g. 60 -> 'C4'."""
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def scale_intervals(scale: str) -> list[int]:
    key = scale.strip().lower().replace(" ", "_").replace("-", "_")
    key = _SCALE_ALIASES.get(key, key)
    if key not in SCALES:
        raise ValueError(f"Unknown scale '{scale}'. Available: {', '.join(sorted(SCALES))}")
    return SCALES[key]


def scale_notes(root: str, scale: str = "major", octave: int = 4, octaves: int = 1) -> list[int]:
    """MIDI notes of a scale starting at ``root`` in ``octave`` (root included at the top)."""
    base = (octave + 1) * 12 + pitch_class(root)
    intervals = scale_intervals(scale)
    notes = [base + 12 * o + i for o in range(octaves) for i in intervals]
    notes.append(base + 12 * octaves)
    return [n for n in notes if 0 <= n <= 127]


def parse_chord(symbol: str, octave: int = 3) -> list[int]:
    """Chord symbol -> MIDI notes. Supports e.g. C, Am, F#m7, Bbmaj7, Dsus4, G7/B, N.C."""
    text = symbol.strip()
    if text.lower() in ("nc", "n.c.", "n.c", "r", "-", "rest"):
        return []
    main, _, bass = text.partition("/")
    m = _ROOT_RE.match(main)
    if not m:
        raise ValueError(f"Invalid chord symbol '{symbol}' (examples: C, Am, F#m7, Bbmaj7, G7/B)")
    root_pc = (_LETTER[m.group(1)] + _accidental(m.group(2))) % 12
    quality = main[m.end():]
    if quality not in CHORDS:
        alt = quality.lower()
        if alt in CHORDS and quality not in ("M", "M7"):
            quality = alt
        else:
            raise ValueError(
                f"Unknown chord quality '{quality}' in '{symbol}'. Supported: "
                + ", ".join(repr(q) if q else "(major)" for q in sorted(CHORDS))
            )
    root = (octave + 1) * 12 + root_pc
    notes = [root + i for i in CHORDS[quality]]
    if bass:
        bass_note = (octave + 1) * 12 + pitch_class(bass)
        while bass_note >= notes[0]:
            bass_note -= 12
        notes = [bass_note] + [n for n in notes if n % 12 != bass_note % 12 or n == notes[0]]
    return [n for n in notes if 0 <= n <= 127]


def smooth_voicing(notes: list[int], previous: list[int] | None) -> list[int]:
    """Pick the inversion of ``notes`` closest to the previous chord (voice leading)."""
    if not previous or not notes:
        return notes
    target = sum(previous) / len(previous)
    best, best_cost = notes, float("inf")
    for shift in (-12, 0, 12):
        base = [n + shift for n in notes]
        for inv in range(len(base)):
            cand = sorted(base[inv:] + [n + 12 for n in base[:inv]])
            for octave_fix in (0, -12):
                c = [n + octave_fix for n in cand]
                if min(c) < 0 or max(c) > 127:
                    continue
                cost = abs(sum(c) / len(c) - target)
                if cost < best_cost:
                    best, best_cost = c, cost
    return best


def parse_duration(text) -> float:
    """Duration in beats: number ('0.5', '1/3') or letter w h q e s t, optional dot."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        value = float(text)
    else:
        s = str(text).strip().lower()
        dots = len(s) - len(s.rstrip("."))
        core = s.rstrip(".") if s[:1] in _DURATION_LETTERS else s
        if core in _DURATION_LETTERS:
            value = _DURATION_LETTERS[core]
            add = value
            for _ in range(dots):
                add /= 2
                value += add
        elif "/" in s:
            num, _, den = s.partition("/")
            value = float(num) / float(den)
        else:
            try:
                value = float(s)
            except ValueError:
                raise ValueError(
                    f"Invalid duration '{text}'. Use beats (1, 0.5, 1/3) or w h q e s (dotted: q.)"
                ) from None
    if value <= 0:
        raise ValueError(f"Duration must be positive, got {text!r}")
    return value


@dataclass
class NoteEvent:
    """A note in beats relative to the start of the target clip."""

    pitch: int
    start: float
    length: float
    velocity: int = 100
    pan: int = 0


_TOKEN_RE = re.compile(r"\[[^\]]*\][^\s]*|\S+")


def parse_sequence(sequence: str, start: float = 0.0, velocity: int = 100,
                   gate: float = 1.0) -> tuple[list[NoteEvent], float]:
    """Parse the compact sequential notation.

    Tokens are separated by spaces and played one after another:
      ``C4:1``          note C4 lasting 1 beat
      ``[C4 E4 G4]:2``  chord lasting 2 beats
      ``r:0.5``         rest (also ``-`` or ``_``)
      ``E4:q.@120``     dotted quarter with velocity 120
    A token without duration reuses the previous duration. ``|`` is ignored.
    Returns (events, end position in beats).
    """
    events: list[NoteEvent] = []
    pos = float(start)
    duration = 1.0
    for token in _TOKEN_RE.findall(sequence.replace(",", " ")):
        if set(token) <= {"|"}:
            continue
        vel = velocity
        body = token
        if "@" in body:
            body, _, vel_text = body.rpartition("@")
            try:
                vel = int(float(vel_text))
            except ValueError:
                raise ValueError(f"Invalid velocity in token '{token}'") from None
        if body.startswith("["):
            close = body.index("]")
            pitch_part, rest = body[1:close], body[close + 1:]
            dur_text = rest[1:] if rest.startswith(":") else ""
            pitches = pitch_part.split()
        else:
            pitch_part, sep, dur_text = body.partition(":")
            pitches = [pitch_part]
        if dur_text:
            duration = parse_duration(dur_text)
        if not (len(pitches) == 1 and pitches[0].lower() in ("r", "-", "_", "rest")):
            for p in pitches:
                events.append(NoteEvent(parse_pitch(p), pos, max(duration * gate, 1 / 48), vel))
        pos += duration
    return events, pos


def parse_steps(pattern: str) -> list[int]:
    """Step-sequencer string -> list of velocities per 16th step (0 = rest).

    ``x`` hit, ``X`` accent, ``o`` ghost note, ``.``/``-``/``_`` rest, digits 1-9
    scale the velocity (9 = loudest). Spaces and ``|`` are ignored.
    """
    result = []
    for ch in pattern:
        if ch in " |\t\n":
            continue
        if ch == "x":
            result.append(100)
        elif ch == "X":
            result.append(130)
        elif ch == "o":
            result.append(55)
        elif ch in ".-_0":
            result.append(0)
        elif ch.isdigit():
            result.append(int(ch) * 14 + 4)
        else:
            raise ValueError(
                f"Invalid step character '{ch}' in '{pattern}'. Use x (hit), X (accent), "
                f"o (ghost), . (rest); one character per 16th note."
            )
    if not result:
        raise ValueError("Empty step pattern")
    return result
