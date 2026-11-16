"""Minimal Standard MIDI File reader (format 0 and 1) for importing notes."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

GM_FAMILIES = ["Piano", "Chromatic Percussion", "Organ", "Guitar", "Bass", "Strings", "Ensemble",
               "Brass", "Reed", "Pipe", "Synth Lead", "Synth Pad", "Synth Effects", "Ethnic",
               "Percussive", "Sound Effects"]


@dataclass
class MidiTrack:
    name: str = ""
    channel: int = 0
    program: int = 0
    notes: list[tuple[float, float, int, int]] = field(default_factory=list)  # start, length (beats), pitch, velocity

    @property
    def is_drums(self) -> bool:
        return self.channel == 9


@dataclass
class MidiSong:
    tempo: float = 120.0
    numerator: int = 4
    denominator: int = 4
    tracks: list[MidiTrack] = field(default_factory=list)


def _varlen(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    while True:
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos


def read_midi(path: str | Path) -> MidiSong:
    data = Path(path).read_bytes()
    if data[:4] != b"MThd":
        raise ValueError("Not a standard MIDI file")
    header_len, fmt, _ntracks, division = struct.unpack(">IHHH", data[4:14])
    if division & 0x8000:
        raise ValueError("SMPTE-timed MIDI files are not supported")
    pos = 8 + header_len
    song = MidiSong()
    tempo_set = False
    while pos + 8 <= len(data):
        chunk, length = data[pos:pos + 4], struct.unpack(">I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + length]
        pos += 8 + length
        if chunk != b"MTrk":
            continue
        per_channel: dict[int, MidiTrack] = {}
        open_notes: dict[tuple[int, int], tuple[int, int]] = {}
        name = ""
        tick, i, status = 0, 0, 0
        while i < len(body):
            delta, i = _varlen(body, i)
            tick += delta
            if body[i] & 0x80:
                status = body[i]
                i += 1
            if status == 0xFF:
                meta = body[i]
                size, i = _varlen(body, i + 1)
                payload = body[i:i + size]
                i += size
                if meta == 0x03 and not name:
                    name = payload.decode("latin-1", errors="replace").strip()
                elif meta == 0x51 and size == 3 and not tempo_set:
                    song.tempo = round(60_000_000 / int.from_bytes(payload, "big"), 2)
                    tempo_set = True
                elif meta == 0x58 and size >= 2 and tick == 0:
                    song.numerator, song.denominator = payload[0], 2 ** payload[1]
                elif meta == 0x2F:
                    break
                continue
            if status in (0xF0, 0xF7):
                size, i = _varlen(body, i)
                i += size
                continue
            kind, channel = status & 0xF0, status & 0x0F
            if kind in (0xC0, 0xD0):
                value = body[i]
                i += 1
                if kind == 0xC0:
                    per_channel.setdefault(channel, MidiTrack(channel=channel)).program = value
                continue
            a, b = body[i], body[i + 1]
            i += 2
            if kind == 0x90 and b > 0:
                open_notes.setdefault((channel, a), (tick, b))
            elif kind == 0x80 or (kind == 0x90 and b == 0):
                started = open_notes.pop((channel, a), None)
                if started:
                    track = per_channel.setdefault(channel, MidiTrack(channel=channel))
                    track.notes.append((started[0] / division, max(tick - started[0], 1) / division,
                                        a, started[1]))
        for channel in sorted(per_channel):
            track = per_channel[channel]
            if not track.notes:
                continue
            if track.is_drums:
                track.name = name or "Drums"
            else:
                track.name = name or GM_FAMILIES[track.program // 8]
            if len(per_channel) > 1 and name:
                track.name = f"{name} ch{channel + 1}"
            track.notes.sort()
            song.tracks.append(track)
    if not song.tracks:
        raise ValueError("The MIDI file contains no notes")
    return song
