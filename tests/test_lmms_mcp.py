"""Tests for the LMMS MCP server.

The tests that need LMMS' factory data (presets, samples, demo songs) run only
when LMMS_DATA_DIR points to it, e.g. the ``data`` folder of an LMMS checkout.
"""

import json
import os
import struct
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from lmms_mcp import dialect, fileio, lmms_env, server as s, theory
from lmms_mcp.project import Project

DATA = lmms_env.data_dir()
needs_data = pytest.mark.skipif(DATA is None, reason="LMMS_DATA_DIR not set")


def call(fn, **kwargs):
    return json.loads(fn(**kwargs))


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("LMMS_PROJECTS_DIR", str(tmp_path))
    s._State.project = None
    s._State.undo.clear()
    yield


def xml():
    return ET.fromstring(s._project().to_xml().split("<!DOCTYPE lmms-project>")[1])


# --- theory -----------------------------------------------------------------

def test_pitches():
    assert theory.parse_pitch("C4") == 60
    assert theory.parse_pitch("A4") == 69
    assert theory.parse_pitch("Bb2") == 46
    assert theory.parse_pitch("f#3") == 54
    assert theory.parse_pitch(72) == 72
    assert theory.pitch_name(61) == "C#4"
    with pytest.raises(ValueError):
        theory.parse_pitch("H2")


def test_chords_and_scales():
    assert theory.parse_chord("Am", 3) == [57, 60, 64]
    assert theory.parse_chord("Cmaj7", 4) == [60, 64, 67, 71]
    assert theory.parse_chord("G7/B", 3)[0] % 12 == 11
    assert theory.parse_chord("N.C.") == []
    assert theory.scale_notes("A", "minor", 3) == [57, 59, 60, 62, 64, 65, 67, 69]
    with pytest.raises(ValueError):
        theory.parse_chord("Cfoo")


def test_sequence_notation():
    events, end = theory.parse_sequence("C4:q E4 [C4 E4 G4]:h. r:e D4@120 | F4:1/2")
    assert end == pytest.approx(6.5)
    assert [e.pitch for e in events] == [60, 64, 60, 64, 67, 62, 65]
    assert events[5].velocity == 120 and events[5].start == pytest.approx(5.5)
    assert theory.parse_steps("x.Xo|....") == [100, 0, 130, 55, 0, 0, 0, 0]


# --- file format --------------------------------------------------------------

@pytest.mark.parametrize("version", ["1.2", "1.3"])
def test_roundtrip_and_layout(version, tmp_path):
    call(s.new_project, name="song", tempo=128, lmms_version=version)
    call(s.add_instrument_track, name="Bass", instrument="lb302")
    call(s.add_notes, track="Bass", sequence="A1:1 C2:1", bar=2)
    saved = call(s.save_project)["saved"]
    raw = Path(saved).read_bytes()
    size = struct.unpack(">I", raw[:4])[0]
    assert len(zlib.decompress(raw[4:])) == size  # Qt qCompress framing
    root = fileio.read_file(saved)
    modern = version == "1.3"
    assert root.get("version") == ("31" if modern else "1.0")
    assert (root.find("song/mixer") is not None) == modern
    assert (root.find("song/fxmixer") is not None) == (not modern)
    clip = root.find(".//track/" + ("midiclip" if modern else "pattern"))
    assert clip.get("pos") == "192" and clip.get("len") == "192"
    keys = [n.get("key") for n in clip.findall("note")]
    assert keys == (["33", "36"] if modern else ["21", "24"])  # 1.2 counts A4 as 57
    it = root.find(".//instrumenttrack")
    assert it.get("basenote") == ("69" if modern else "57")
    assert ("mixch" in it.attrib) == modern
    summary = call(s.open_project, path="song")
    assert summary["tempo"] == 128 and summary["tracks"][0]["clips"][0]["notes"] == 2
    assert call(s.get_notes, track="Bass")["clips"][0]["notes"][0]["pitch"] == "A1"


def test_dialect_detection():
    assert dialect.detect(ET.Element("lmms-project", version="1.0", creatorversion="1.2.2")).file_version == 16
    assert dialect.detect(ET.Element("lmms-project", version="1.0", creatorversion="1.0.3")).file_version == 12
    d22 = dialect.detect(ET.Element("lmms-project", version="22", creatorversion="1.3.0-alpha"))
    assert d22.extended_keys and d22.tag("mixer") == "mixer" and d22.tag("midi_clip") == "pattern"
    assert dialect.LEGACY.key_offset("zynaddsubfx") == 0 and dialect.LEGACY.key_offset("lb302") == -12


def test_opens_mixed_legacy_file(tmp_path):
    """Files of the previous lmms-mcp generation: 1.2 header with 1.3 mixer names."""
    path = tmp_path / "old.mmp"
    path.write_text(
        '<?xml version="1.0"?><!DOCTYPE lmms-project>'
        '<lmms-project version="1.0" creator="x" creatorversion="1.2.0" type="song">'
        '<head bpm="100" timesig_numerator="4" timesig_denominator="4" mastervol="100" masterpitch="0"/>'
        '<song><trackcontainer type="song"><track type="0" name="T" muted="0" solo="0">'
        '<instrumenttrack vol="100" pan="0" mixch="0" basenote="57"><instrument name="kicker"><kicker/></instrument>'
        '</instrumenttrack><pattern pos="0" len="192" type="1"><note key="45" pos="0" len="48" vol="100" pan="0"/></pattern>'
        '</track></trackcontainer><mixer><mixerchannel num="0" name="Master" volume="1"/></mixer></song></lmms-project>')
    summary = call(s.open_project, path=str(path))
    assert summary["lmms_format"] == "1.2"
    root = xml()
    assert root.find("song/fxmixer/fxchannel") is not None and root.find("song/mixer") is None
    assert root.find(".//instrumenttrack").get("fxch") == "0"
    assert call(s.get_notes, track="T")["clips"][0]["notes"][0]["pitch"] == "A3"


# --- editing ------------------------------------------------------------------

def build_song(version="1.3"):
    call(s.new_project, name="demo", tempo=120, lmms_version=version)
    call(s.add_drum_kit)
    call(s.set_beat, pattern="Main", steps={"Kick": "x...x...x...x...", "Hihat": "..x...x...x...X."})
    call(s.arrange_pattern, pattern="Main", bar=1, length_bars=4)
    call(s.add_instrument_track, name="Pad")
    call(s.add_chords, track="Pad", chords="Am F C G", bar=1)


@pytest.mark.parametrize("version", ["1.2", "1.3"])
def test_beat_editor(version):
    build_song(version)
    root = xml()
    modern = version == "1.3"
    holder = "patterntrack" if modern else "bbtrack"
    stores = root.findall(f".//{holder}/trackcontainer")
    assert len(stores) == 1 and stores[0].get("type") == ("patternstore" if modern else "bbtrackcontainer")
    kick = stores[0].find("track")
    notes = kick.find("midiclip" if modern else "pattern").findall("note")
    assert [n.get("pos") for n in notes] == ["0", "48", "96", "144"]
    assert notes[0].get("len") == ("12" if modern else "-192")
    assert notes[0].get("key") == ("69" if modern else "57")
    assert (notes[0].get("type") == "1") == modern
    # a second pattern gets a clip in every row, one bar apart
    call(s.duplicate_pattern, pattern="Main", new_name="Fill")
    call(s.set_beat, pattern="Fill", steps={"Snare": "............xxxx"})
    root = xml()
    for row in root.findall(f".//{holder}/trackcontainer/track"):
        clips = row.findall("midiclip" if modern else "pattern")
        assert [c.get("pos") for c in clips] == ["0", "192"]
    overview = call(s.get_project)
    assert overview["beat_instruments"][0]["patterns"] == {"Main": "x...x...x...x...", "Fill": "x...x...x...x..."}
    # removing the first pattern keeps the instruments (the store moves to the next pattern track)
    call(s.remove_track, track="Main")
    root = xml()
    assert len(root.findall(f".//{holder}/trackcontainer/track")) == 5
    assert all(len(r.findall("midiclip" if modern else "pattern")) == 1
               for r in root.findall(f".//{holder}/trackcontainer/track"))


def test_default_pattern_is_reused():
    call(s.new_project)
    call(s.add_drum_kit, pieces=["kick"], new_mixer_channel=False)
    call(s.set_beat, pattern="Groove", steps={"kick": "x..."})
    assert [t["name"] for t in call(s.get_project)["tracks"]] == ["Groove"]


def test_notes_extend_and_replace_clip():
    call(s.new_project, lmms_version="1.3")
    call(s.add_instrument_track, name="Lead")
    r = call(s.add_notes, track="Lead", sequence="C4:1 D4 E4 F4", bar=3)
    assert r["created_clip"] and r["clip_length_bars"] == 1
    r = call(s.add_notes, track="Lead", notes=[{"pitches": ["C4", "E4"], "start": 4, "length": 2}], bar=3)
    assert not r["created_clip"] and r["clip_length_bars"] == 2 and r["notes_in_clip"] == 6
    r = call(s.add_notes, track="Lead", sequence="G4:4", bar=3, replace=True)
    assert r["notes_in_clip"] == 3
    with pytest.raises(s.ToolError):
        s.add_notes(track="Lead", sequence="C4:1", bar=0)
    with pytest.raises(s.ToolError):
        s.add_notes(track="Nope", sequence="C4:1")


def test_failed_tool_leaves_project_untouched():
    call(s.new_project)
    call(s.add_instrument_track, name="A")
    before = s._project().to_xml()
    with pytest.raises(s.ToolError):
        s.add_notes(track="A", notes=[{"pitch": "C4", "start": 0}, {"pitch": "XX", "start": 1}])
    assert s._project().to_xml() == before


def test_copy_section_transpose_undo():
    build_song()
    r = call(s.copy_section, from_bar=1, length_bars=4, to_bar=5, times=2)
    assert r["clips_copied"] == 4 and r["song_length_bars"] == 12
    assert call(s.transpose_notes, track="Pad", semitones=12, from_bar=5, length_bars=4)["notes_transposed"] == 12
    clips = call(s.get_notes, track="Pad")["clips"]
    assert clips[0]["notes"][0]["pitch"] == "A3" and clips[1]["notes"][0]["pitch"] == "A4"
    call(s.undo)
    assert call(s.get_notes, track="Pad")["clips"][1]["notes"][0]["pitch"] == "A3"
    assert call(s.clear_section, from_bar=5)["clips_removed"] == 4


def test_mixer_effects_automation():
    build_song()
    ch = call(s.add_mixer_channel, name="Bus", tracks=["Pad"])["mixer_channel"]
    assert ch == 2
    call(s.add_effect, effect="reverbsc", track="Pad", params={"size": 0.95}, wet=0.3)
    call(s.add_effect, effect="eq", mixer_channel=ch, params={"HPactive": 1})
    with pytest.raises(s.ToolError):
        s.add_effect(effect="reverbsc", track="Pad", params={"size": 5})
    call(s.add_automation, parameter="cutoff", track="Pad", points=[[1, 500], [5, 9000]])
    call(s.add_automation, parameter="tempo", points=[{"bar": 3, "value": 120}, {"bar": 5, "value": 90}])
    call(s.add_automation, parameter="volume", mixer_channel=ch, points=[[1, 0], [2, 100]])
    call(s.add_lfo, parameter="pan", track="Pad", period_seconds=1.5)
    root = xml()
    chain = root.find(".//track[@name='Pad']/instrumenttrack/fxchain")
    assert chain.get("enabled") == "1" and chain.get("numofeffects") == "1"
    assert chain.find("effect/ReverbSCControls").get("size") == "0.95"
    fcut = root.find(".//track[@name='Pad']/instrumenttrack/eldata/fcut")
    clip = root.find(".//track[@name='Pad > cutoff']/automationclip")
    assert clip.find("object").get("id") == fcut.get("id")
    assert [t.get("value") for t in clip.findall("time")] == ["500", "9000"]
    assert clip.find("time").get("outValue") == "500"
    bpm = root.find("head/bpm")
    assert bpm.get("value") == "120" and root.find("head").get("bpm") is None
    assert s._project().tempo == 120
    volume = root.find("song/mixer/mixerchannel[@num='2']/volume")
    vol_clip = root.find(".//track[@name='Mixer Bus > volume']/automationclip")
    assert vol_clip.findall("time")[1].get("value") == "1" and volume is not None
    assert root.find(".//track[@name='Pad']/instrumenttrack/connection/pan").get("id") == "0"
    assert root.find("song/controllers/lfocontroller").get("speed") == "1.5"
    # removing channel 1 (Drums) renumbers channel 2 and re-routes tracks
    call(s.update_mixer_channel, mixer_channel=1, remove=True)
    overview = call(s.get_project)
    assert [c["name"] for c in overview["mixer"]] == ["Master", "Bus"]
    assert next(t for t in overview["tracks"] if t["name"] == "Pad")["mixer_channel"] == 1
    assert all(t["mixer_channel"] == 0 for t in overview["beat_instruments"])


def test_envelope_filter_arp():
    call(s.new_project)
    call(s.add_instrument_track, name="Pluck", params={"wavetype0": 2})
    r = call(s.set_envelope, track="Pluck", attack=0, hold=0, decay=1.25, sustain=0)
    assert r["stored"]["dec"] == "0.5" and r["stored"]["amt"] == "1"  # 5 s * 0.5^2
    r = call(s.set_filter, track="Pluck", filter_type="moog", cutoff=1200)
    assert r["enabled"] and r["type"] == "moog"
    r = call(s.set_arpeggiator, track="Pluck", chord="minor", direction="up_down", time_ms=125)
    assert r["chord"] == "minor"
    assert call(s.set_chord_stacking, track="Pluck", chord="5")["chord"] == "5"
    assert xml().find(".//tripleoscillator").get("wavetype0") == "2"


def test_time_signature_and_validation():
    call(s.new_project, time_signature="3/4")
    assert s._project().tpb == 144
    call(s.add_instrument_track, name="Waltz")
    r = call(s.add_chords, track="Waltz", chords="C G", beats_per_chord=3, bar=2)
    assert r["clip_length_bars"] == 2
    call(s.add_instrument_track, name="Empty", instrument="audiofileprocessor")
    issues = call(s.validate_project)["issues"]
    assert any("no sample" in i for i in issues) and any("no notes" in i for i in issues)
    with pytest.raises(s.ToolError):
        s.add_instrument_track(name="X", instrument="watsyn")  # silent without preset


def test_sample_clip_and_midi_import(tmp_path):
    import wave
    wav = tmp_path / "tone.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(b"\0\0" * 16000)  # 2 seconds
    call(s.new_project, tempo=120, lmms_version="1.3")
    call(s.add_sample_track, name="Audio")
    r = call(s.add_sample_clip, track="Audio", file=str(wav), bar=2)
    assert r["length_beats"] == 4
    clip = xml().find(".//sampleclip")
    assert clip.get("pos") == "192" and clip.get("len") == "192"

    def vlq(n):
        return bytes([n]) if n < 128 else bytes([0x80 | (n >> 7), n & 0x7F])
    events = b"\x00\xff\x51\x03\x07\xa1\x20"  # 120 bpm
    events += b"\x00\x90\x3c\x64" + vlq(480) + b"\x80\x3c\x00" + b"\x00\x99\x24\x7f" + vlq(240) + b"\x89\x24\x00"
    events += b"\x00\xff\x2f\x00"
    mid = tmp_path / "t.mid"
    mid.write_bytes(b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480) + b"MTrk" + struct.pack(">I", len(events)) + events)
    r = call(s.import_midi, path=str(mid))
    assert {t["track"] for t in r["tracks_created"]} == {"Piano", "Drums kick"}
    assert call(s.get_notes, track="Piano")["clips"][0]["notes"] == [
        {"pitch": "C4", "start": 0.0, "length": 1.0, "velocity": 126}]


def test_tools_are_registered():
    import asyncio
    tools = asyncio.run(s.mcp.list_tools())
    names = {t.name for t in tools}
    assert {"new_project", "add_notes", "set_beat", "add_effect", "save_project"} <= names
    assert all(t.description for t in tools)


# --- against real LMMS data -----------------------------------------------------

@needs_data
def test_factory_samples_and_presets():
    assert lmms_env.resolve_sample("kick01")[0] == "drums/kick01.ogg"
    assert lmms_env.resolve_sample("beats/break01.ogg")[1] is not None  # renamed in LMMS 1.3
    for kit in s.catalog.DRUM_KITS.values():
        for sample in kit.values():
            assert lmms_env.resolve_sample(sample)[1] is not None, sample
    assert lmms_env.audio_duration(DATA / "samples" / "drums" / "kick01.ogg") > 0.05
    presets = lmms_env.list_presets()
    assert presets["presets_per_instrument"].get("tripleoscillator", 0) > 20


@needs_data
@pytest.mark.parametrize("version", ["1.2", "1.3"])
def test_every_preset_loads(version):
    call(s.new_project, lmms_version=version)
    failures = []
    for instrument, name, path in lmms_env._presets():
        try:
            r = call(s.add_instrument_track, name="P", preset=path)
            assert r["instrument"], name
            call(s.remove_track, track="P")
        except Exception as exc:  # noqa: BLE001
            if "too old preset format" not in str(exc):
                failures.append(f"{name}: {exc}")
    assert not failures, failures[:5]


@needs_data
def test_zyn_preset_is_wrapped_as_master():
    call(s.new_project, lmms_version="1.2")
    call(s.add_instrument_track, name="Keys", instrument="zynaddsubfx", preset="Rhodes/0001-DX Rhodes 1")
    call(s.add_notes, track="Keys", sequence="A4:1")
    root = xml()
    data = root.find(".//zynaddsubfx/ZynAddSubFX-data")
    assert data.find("MASTER/PART[@id='0']/INSTRUMENT") is not None
    assert root.find(".//track[@name='Keys']/pattern/note").get("key") == "69"  # MIDI based: no shift


@needs_data
def test_demo_projects_survive_roundtrip(tmp_path):
    demos = sorted((DATA / "projects").rglob("*.mmp*"))
    assert demos
    for demo in demos:
        project = Project.open(demo)
        before = project.summary()
        notes_before = sum(1 for _ in project.root.iter("note"))
        out = tmp_path / (demo.stem + ".mmpz")
        project.save(out)
        again = Project.open(out)
        assert again.summary()["tracks"] == before["tracks"], demo.name
        assert sum(1 for _ in again.root.iter("note")) == notes_before
        assert again.root.get("version") == project.root.get("version")
