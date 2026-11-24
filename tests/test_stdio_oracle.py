"""Corrupt values without changing note counts to test the persistence oracle."""

from copy import deepcopy
import os
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import anyio
import pytest

from test_stdio_render import ToolClient, assert_restored_project, catalog_tools, configured_paths


def oracle_fixture():
    expected = [{"name": "Lead", "volume": 18, "pan": 10, "notes": [
        {"pitch": 60, "start": 0, "length": 0.75, "velocity": 70, "pan": -12}]}]
    info = {"tempo": 108, "master_volume": 50, "tracks": [{
        "name": "Lead", "instrument": "tripleoscillator", "volume": 18, "pan": 10,
        "clips": [{"bar": 1, "notes": 1}]}]}
    api = {"Lead": {"clips": [{"bar": 1, "notes": [
        {"pitch": "C4", "start": 0, "length": 0.75, "velocity": 70}]}]}}
    disk = ET.fromstring('''<lmms-project version="1.0"><head bpm="108" mastervol="50"/>
      <song><trackcontainer><track name="Lead"><instrumenttrack vol="18" pan="10">
      <instrument name="tripleoscillator"/></instrumenttrack><pattern pos="0">
      <note key="48" pos="0" len="36" vol="70" pan="-12"/>
      </pattern></track></trackcontainer></song></lmms-project>''')
    return info, api, disk, expected


def test_unchanged_score_is_accepted():
    assert_restored_project(*oracle_fixture())


@pytest.mark.parametrize("field,value", [("pitch", "C5"), ("start", 1),
                                        ("length", 1), ("velocity", 71)])
def test_api_note_corruption_is_rejected(field, value):
    info, api, disk, expected = oracle_fixture()
    api["Lead"]["clips"][0]["notes"][0][field] = value
    with pytest.raises(AssertionError, match="API notes"):
        assert_restored_project(info, api, disk, expected)


@pytest.mark.parametrize("field", ["key", "pos", "len", "vol", "pan"])
def test_xml_note_corruption_is_rejected(field):
    info, api, disk, expected = oracle_fixture()
    note = disk.find(".//note")
    note.set(field, str(int(note.get(field)) + 1))
    with pytest.raises(AssertionError, match="XML notes"):
        assert_restored_project(info, api, disk, expected)


def test_unchanged_counts_do_not_hide_replaced_notes():
    info, api, disk, expected = oracle_fixture()
    original = deepcopy(expected)
    expected[0]["notes"][0]["pitch"] = 72
    assert len(expected[0]["notes"]) == len(original[0]["notes"])
    with pytest.raises(AssertionError):
        assert_restored_project(info, api, disk, expected)


@pytest.mark.parametrize("field,value", [("volume", 19), ("pan", 11)])
def test_track_setting_corruption_is_rejected(field, value):
    info, api, disk, expected = oracle_fixture()
    info["tracks"][0][field] = value
    with pytest.raises(AssertionError):
        assert_restored_project(info, api, disk, expected)


def test_tool_catalog_uses_sdk_pagination_params():
    class Session:
        async def list_tools(self, *, params=None):
            if params is None:
                return SimpleNamespace(tools=[SimpleNamespace(name="first")], next_cursor="next")
            assert params.cursor == "next"
            return SimpleNamespace(tools=[SimpleNamespace(name="second")], next_cursor=None)
    assert anyio.run(catalog_tools, Session()) == {"first", "second"}


def test_tool_catalog_rejects_repeated_cursor():
    class Session:
        async def list_tools(self, *, params=None):
            return SimpleNamespace(tools=[], next_cursor="repeat")
    with pytest.raises(AssertionError, match="Repeated"):
        anyio.run(catalog_tools, Session())


def test_relative_paths_become_absolute_before_server_chdir(tmp_path, monkeypatch):
    executable = tmp_path / "lmms"
    executable.write_text("fixture, never executed\n")
    executable.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LMMS_TEST_EXECUTABLE", "lmms")
    monkeypatch.setenv("LMMS_TEST_ARTIFACT_DIR", "artifacts")
    app, output = configured_paths(tmp_path)
    assert app == executable
    assert output == tmp_path / "artifacts"


@pytest.mark.skipif(os.name == "nt", reason="POSIX AppRun symlink fixture")
def test_executable_launcher_symlink_is_preserved(tmp_path, monkeypatch):
    binary = tmp_path / "lmms"
    binary.write_text("fixture, never executed\n")
    binary.chmod(0o755)
    launcher = tmp_path / "AppRun"
    launcher.symlink_to(binary)
    monkeypatch.setenv("LMMS_TEST_EXECUTABLE", str(launcher))
    app, _ = configured_paths(tmp_path)
    assert app == launcher and app != binary


@pytest.mark.parametrize("flag", [False, True])
def test_application_error_requires_current_server_mcp_flag(flag):
    class Session:
        async def call_tool(self, *args, **kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text='{"error":"invalid instrument"}')],
                model_dump=lambda **kwargs: {"isError": flag},
            )
    async def request():
        return await ToolClient(Session(), []).call("add_instrument_track", expect_error=True)
    if flag:
        assert anyio.run(request) == {"error": "invalid instrument"}
    else:
        with pytest.raises(AssertionError, match="isError"):
            anyio.run(request)
