"""Opt-in real MCP STDIO/persistence/render test. Caller provides isolation/LMMS."""

import array
import json
import math
import os
import sys
import time
import wave
from importlib.metadata import version
from pathlib import Path
from xml.etree import ElementTree as ET

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import PaginatedRequestParams


class ToolClient:
    def __init__(self, session, transcript):
        self.session, self.transcript = session, transcript

    async def call(self, name, arguments=None, *, expect_error=False):
        started = time.monotonic()
        result = await self.session.call_tool(name, arguments or {}, read_timeout_seconds=45)
        response = result.model_dump(mode="json", by_alias=True)
        self.transcript.append({"name": name, "arguments": arguments or {},
                                "elapsed_seconds": round(time.monotonic() - started, 6),
                                "response": response})
        if expect_error:
            # This test targets the rewritten server, whose ToolError wrapper
            # signals application failures through the MCP error flag.
            assert response.get("isError") is True, "Expected MCP isError flag"
        text = "\n".join(block.text for block in result.content if block.type == "text")
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = text
        error = response.get("isError", False) or isinstance(value, dict) and "error" in value
        assert bool(error) is expect_error, response
        if not expect_error:
            assert isinstance(value, dict), value
        return value


async def catalog_tools(session):
    names, cursors, cursor = set(), set(), None
    for _ in range(10):
        listing = (await session.list_tools() if cursor is None else
                   await session.list_tools(params=PaginatedRequestParams(cursor=cursor)))
        names.update(tool.name for tool in listing.tools)
        cursor = listing.next_cursor
        if cursor is None:
            return names
        assert cursor not in cursors, "Repeated tool catalog cursor"
        cursors.add(cursor)
    raise AssertionError("Tool catalog exceeded fixture pagination limit")


def fixture_tracks():
    """Input score: MIDI pitches and beats, independent of LMMS' stored keys."""
    return [
        {"name": "STDIO lead", "volume": 18, "pan": 10, "notes": [
            {"pitch": pitch, "start": i, "length": 0.75, "velocity": 70, "pan": (-12 if i % 2 else 15)}
            for i, pitch in enumerate((60, 64, 67, 64, 69, 67, 64, 60))]},
        {"name": "STDIO bass", "volume": 14, "pan": -10, "notes": [
            {"pitch": pitch, "start": i * 2, "length": 2, "velocity": 65, "pan": -5}
            for i, pitch in enumerate((36, 43, 33, 40))]},
    ]


def pitch_name(midi):
    names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
    return f"{names[midi % 12]}{midi // 12 - 1}"


def assert_restored_project(info, api_tracks, disk, expected):
    """API checks its exposed fields; independent legacy XML checks every note field."""
    assert info["tempo"] == 108 and info["master_volume"] == 50
    assert len(info["tracks"]) == len(expected)
    assert disk.get("version") == "1.0", "This real-LMMS fixture explicitly selects format 1.2"
    assert float(disk.find("head").get("bpm")) == 108
    assert float(disk.find("head").get("mastervol")) == 50
    xml_tracks = disk.findall("./song/trackcontainer/track")
    assert len(xml_tracks) == len(expected)
    for track, xml_track, target in zip(info["tracks"], xml_tracks, expected):
        assert track["name"] == xml_track.get("name") == target["name"]
        instrument = xml_track.find("instrumenttrack")
        assert track["instrument"] == instrument.find("instrument").get("name") == "tripleoscillator"
        assert track["volume"] == float(instrument.get("vol")) == target["volume"]
        assert track["pan"] == float(instrument.get("pan")) == target["pan"]
        assert len(track["clips"]) == 1 and track["clips"][0]["bar"] == 1
        assert track["clips"][0]["notes"] == len(target["notes"])
        clips = api_tracks[target["name"]]["clips"]
        assert len(clips) == 1 and clips[0]["bar"] == 1
        wanted = sorted((pitch_name(n["pitch"]), n["start"], n["length"], n["velocity"])
                        for n in target["notes"])
        observed = sorted((n["pitch"], n["start"], n["length"], n["velocity"])
                          for n in clips[0]["notes"])
        assert observed == wanted, "Reloaded API notes differ from the input score"
        # get_notes currently omits per-note pan. Do not pretend this API checks it.
        patterns = xml_track.findall("pattern")
        assert len(patterns) == 1 and int(patterns[0].get("pos")) == 0
        xml_notes = sorted(tuple(float(n.get(field)) for field in ("key", "pos", "len", "vol", "pan"))
                           for n in patterns[0].findall("note"))
        # LMMS 1.2 tripleoscillator uses MIDI - 12 and 48 ticks per beat.
        xml_wanted = sorted((n["pitch"] - 12, n["start"] * 48, n["length"] * 48,
                             n["velocity"], n["pan"]) for n in target["notes"])
        assert xml_notes == xml_wanted, "Saved XML notes differ from the input score"


async def compose_and_reload(client, output):
    project = output / "stdio-score.mmp"
    expected = fixture_tracks()
    await client.call("new_project", {"name": "stdio-score", "tempo": 108, "lmms_version": "1.2"})
    await client.call("set_song", {"master_volume": 50})
    for target in expected:
        await client.call("add_instrument_track", {
            "name": target["name"], "instrument": "tripleoscillator",
            "volume": target["volume"], "pan": target["pan"],
        })
        result = await client.call("add_notes", {"track": target["name"], "notes": target["notes"]})
        assert result["notes_added"] == len(target["notes"])
    await client.call("save_project", {"path": str(project)})
    # Replace state so that a no-op open_project cannot pass the readback checks.
    await client.call("new_project", {"name": "unrelated", "tempo": 77, "lmms_version": "1.2"})
    assert (await client.call("get_project"))["tracks"] == []
    await client.call("open_project", {"path": str(project)})
    info = await client.call("get_project")
    api_tracks = {target["name"]: await client.call("get_notes", {"track": target["name"]})
                  for target in expected}
    assert_restored_project(info, api_tracks, ET.parse(project).getroot(), expected)
    # get_notes does not expose per-note pan. Saving the reloaded state again
    # makes loss of that field during open_project observable through the API.
    restored = output / "stdio-reloaded.mmp"
    await client.call("save_project", {"path": str(restored)})
    assert_restored_project(info, api_tracks, ET.parse(restored).getroot(), expected)
    return restored


async def exercise(parameters, output, report, transcript):
    with anyio.fail_after(120), (output / "server-stderr.log").open("w", encoding="utf-8") as errors:
        async with stdio_client(parameters, errlog=errors) as streams:
            async with ClientSession(*streams, read_timeout_seconds=20) as session:
                initialized = await session.initialize()
                report["protocol_version"] = initialized.protocol_version
                report["server_info"] = initialized.server_info.model_dump(mode="json")
                names = await catalog_tools(session)
                required = {"new_project", "set_song", "add_instrument_track", "add_notes",
                            "save_project", "open_project", "get_project", "get_notes",
                            "get_environment", "render_project"}
                assert required <= names
                report["tool_names"] = sorted(names)
                client = ToolClient(session, transcript)
                installation = await client.call("get_environment")
                assert installation["lmms_executable"] and installation["lmms_version"]
                expected_version = os.environ.get("LMMS_TEST_EXPECTED_VERSION")
                if expected_version:
                    assert installation["lmms_version"] == expected_version
                assert "tripleoscillator" in installation["instruments_verified_available"]
                report["lmms_version"] = installation["lmms_version"]
                await client.call("new_project", {"lmms_version": "1.2"})
                await client.call("set_song", {"tempo": "not-a-number"}, expect_error=True)
                assert transcript[-1]["response"]["isError"] is True
                await client.call("add_instrument_track", {
                    "name": "invalid control", "instrument": "not_a_real_instrument_742",
                }, expect_error=True)
                report["application_error_is_error"] = transcript[-1]["response"].get("isError", False)
                assert (await client.call("get_project"))["tracks"] == []
                project = await compose_and_reload(client, output)
                rendered = await client.call("render_project", {
                    "output": str(output / "stdio-score.wav"), "format": "wav",
                })
                assert project.is_file() and (output / "stdio-score.wav").is_file()
                report["render"] = rendered
    report["session_closed"] = True


def inspect_audio(path):
    with wave.open(str(path), "rb") as wav:
        assert wav.getsampwidth() == 2
        channels, rate, frames = wav.getnchannels(), wav.getframerate(), wav.getnframes()
        pcm = array.array("h", wav.readframes(frames))
    if sys.byteorder != "little":
        pcm.byteswap()
    assert channels == 2 and rate == 44100 and frames > 0
    peak = max(abs(value) for value in pcm)
    rms = math.sqrt(sum(value * value for value in pcm) / len(pcm))
    clipping = sum(abs(value) >= 32767 for value in pcm)
    assert 4 <= frames / rate <= 9 and rms > 10 and clipping == 0
    return {"channels": channels, "sample_rate_hz": rate, "duration_seconds": round(frames / rate, 4),
            "peak_dbfs": round(20 * math.log10(peak / 32768), 3),
            "rms_dbfs": round(20 * math.log10(rms / 32768), 3), "full_scale_samples": clipping}


def configured_paths(tmp_path):
    configured = os.environ.get("LMMS_TEST_EXECUTABLE")
    assert configured, "LMMS_TEST_EXECUTABLE must select the prepared application"
    # Keep AppRun symlinks intact: resolving the executable can bypass its launcher.
    executable = Path(configured).expanduser().absolute()
    assert executable.is_file() and os.access(executable, os.X_OK)
    output = Path(os.environ.get("LMMS_TEST_ARTIFACT_DIR", tmp_path / "stdio-render"))
    return executable, output.expanduser().resolve()


@pytest.mark.skipif(os.environ.get("LMMS_TEST_STDIO_RENDER") != "1",
                    reason="Set LMMS_TEST_STDIO_RENDER=1 in a prepared isolated environment")
@pytest.mark.skipif(version("mcp").split(".", 1)[0] != "2",
                    reason="Real STDIO fixture requires MCP SDK 2.x; MCP 1.x is not exercised here")
def test_stdio_roundtrip_and_real_render(tmp_path):
    executable, output = configured_paths(tmp_path)
    output.mkdir(parents=True, exist_ok=False)
    transcript = []
    report = {"status": "FAIL", "transport": "stdio", "sdk_version": version("mcp"),
              "lmms_format": "1.2", "listening_review": "NOT_PERFORMED"}
    env = {"PYTHONPATH": os.pathsep.join(filter(None, (
        str(Path(__file__).resolve().parents[1] / "src"), os.environ.get("PYTHONPATH")))),
        "PYTHONDONTWRITEBYTECODE": "1", "LMMS_EXECUTABLE": str(executable),
        "LMMS_PROJECTS_DIR": str(output), "QT_QPA_PLATFORM": "offscreen", "OMP_NUM_THREADS": "1"}
    for key in ("QT_QPA_PLATFORM_PLUGIN_PATH", "LMMS_PLUGIN_DIR"):
        if key in os.environ:
            value = os.environ[key]
            if key == "QT_QPA_PLATFORM_PLUGIN_PATH":
                env[key] = os.pathsep.join(str(Path(part).expanduser().absolute())
                                          for part in value.split(os.pathsep) if part)
            else:
                env[key] = str(Path(value).expanduser().absolute()) if value else value
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "lmms_mcp"],
                                      env=env, cwd=str(output))
    try:
        anyio.run(exercise, parameters, output, report, transcript)
        report["audio"] = inspect_audio(output / "stdio-score.wav")
        report["tool_calls"] = len(transcript)
        report["persistence_coverage"] = {
            "api_note_fields": ["pitch", "start", "length", "velocity"],
            "independent_xml_note_fields": ["key", "pos", "len", "vol", "pan"],
            "saved_xml_stages": ["before_reload", "after_reload_and_resave"],
            "limitation": "get_notes omits per-note pan; it is verified through both XML saves",
        }
        report["controls"] = {"invalid_argument_is_error": True,
                              "invalid_instrument_did_not_add_track": True,
                              "reload_replaced_different_in_memory_project": True}
        report["status"] = "PASS"
    finally:
        (output / "transcript.json").write_text(json.dumps(transcript, indent=2), encoding="utf-8")
        (output / "smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
