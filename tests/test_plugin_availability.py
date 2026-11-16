"""Regression tests for plugin availability, independent of an LMMS install."""

import json
from unittest.mock import patch

import pytest

from lmms_mcp import lmms_env, server as s


def call(fn, **kwargs):
    return json.loads(fn(**kwargs))


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("LMMS_PROJECTS_DIR", str(tmp_path))
    s._State.project = None
    s._State.undo.clear()
    yield


def isolated_environment():
    """Keep environment checks away from the workstation's real LMMS."""
    return (
        patch.object(lmms_env, "find_executable", return_value=None),
        patch.object(lmms_env, "data_dir", return_value=None),
    )


def test_plugin_files_are_read_from_the_inventory_directory(tmp_path, monkeypatch):
    (tmp_path / "libtripleoscillator.so").write_bytes(b"")
    (tmp_path / "delay.dll").write_bytes(b"")
    (tmp_path / "notes.txt").write_text("not a plugin")
    monkeypatch.setenv("LMMS_PLUGIN_DIR", str(tmp_path))
    assert lmms_env.get_installed_plugins() == {"tripleoscillator", "delay"}


def test_empty_inventory_is_unknown():
    status, reason = lmms_env.get_plugin_availability("slicert", installed_plugins=set())
    assert status is None
    assert "cannot verify" in reason


def test_missing_from_a_nonempty_inventory_is_false():
    status, reason = lmms_env.get_plugin_availability("slicert", installed_plugins={"delay"})
    assert status is False
    assert "not included" in reason


def test_detected_plugin_and_alias_are_true():
    assert lmms_env.get_plugin_availability("delay", installed_plugins={"delay"})[0] is True
    assert lmms_env.get_plugin_availability("nes", installed_plugins={"papu"})[0] is True


def test_static_plugin_is_only_verified_when_its_file_exists():
    unknown, reason = lmms_env.get_plugin_availability("freeboy", installed_plugins={"delay"})
    detected, _ = lmms_env.get_plugin_availability("freeboy", installed_plugins={"freeboy"})
    assert unknown is None
    assert "built into" in reason
    assert detected is True


def test_unreadable_inventory_is_unknown():
    with patch.object(lmms_env, "get_installed_plugins", side_effect=PermissionError):
        status, reason = lmms_env.get_plugin_availability("tripleoscillator")
    assert status is None
    assert "could not be read" in reason


def test_legacy_boolean_helper_remains_permissive():
    with patch.object(lmms_env, "get_installed_plugins", return_value=set()):
        allowed, reason = lmms_env.check_plugin_available("tripleoscillator")
    assert allowed is True
    assert "cannot verify" in reason


def test_environment_reports_unknown_inventory():
    find_exe, data_dir = isolated_environment()
    with find_exe, data_dir, patch.object(lmms_env, "get_installed_plugins", return_value=set()):
        info = call(s.get_environment)
    assert info["plugin_inventory_status"] == "unknown"
    assert info["instruments_verified_available"] == []
    assert info["effects_verified_available"] == []
    assert "tripleoscillator" in info["instruments_unverified"]
    assert info["plugin_inventory_warning"]
    assert any("LMMS_PLUGIN_DIR" in hint for hint in info["hints"])


def test_environment_uses_one_inventory_snapshot():
    find_exe, data_dir = isolated_environment()
    with find_exe, data_dir, patch.object(
        lmms_env, "get_installed_plugins", return_value={"delay"}
    ) as query:
        info = call(s.get_environment)
    query.assert_called_once_with()
    assert info["plugin_inventory_status"] == "available"
    assert "delay" in info["effects_verified_available"]
    assert "tripleoscillator" not in info["instruments_verified_available"]
    assert "freeboy" in info["instruments_unverified"]


def test_offline_instrument_is_allowed_with_a_warning():
    with patch.object(lmms_env, "get_installed_plugins", return_value=set()):
        call(s.new_project, name="offline")
        result = call(s.add_instrument_track, name="Lead")
    assert result["instrument"] == "tripleoscillator"
    assert any("cannot verify" in warning for warning in result["warnings"])


def test_detected_instrument_does_not_warn():
    with patch.object(lmms_env, "get_installed_plugins", return_value={"tripleoscillator"}):
        call(s.new_project, name="online")
        result = call(s.add_instrument_track, name="Lead")
    assert "warnings" not in result


def test_offline_effect_is_allowed_with_a_warning():
    with patch.object(lmms_env, "get_installed_plugins", return_value=set()):
        call(s.new_project, name="offline")
        call(s.add_instrument_track, name="Lead", instrument="tripleoscillator")
        result = call(s.add_effect, effect="delay", track="Lead")
    assert any("cannot verify" in warning for warning in result["warnings"])


def test_missing_effect_reports_the_missing_file():
    with patch.object(lmms_env, "get_installed_plugins", return_value={"tripleoscillator", "delay"}):
        call(s.new_project, name="partial")
        call(s.add_instrument_track, name="Lead", instrument="tripleoscillator")
        result = call(s.add_effect, effect="reverbsc", track="Lead")
    assert any("not included" in warning for warning in result["warnings"])
