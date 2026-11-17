"""Plugin discovery fixtures; no native libraries or executables are loaded."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from lmms_mcp import lmms_env


@pytest.fixture
def selected_executable(tmp_path, monkeypatch):
    executable = tmp_path / "usr" / "bin" / "lmms"
    executable.parent.mkdir(parents=True)
    executable.write_text("discovery fixture, never executed\n")
    monkeypatch.delenv("LMMS_PLUGIN_DIR", raising=False)
    monkeypatch.setattr(lmms_env, "find_executable", lambda: executable)
    monkeypatch.setattr(lmms_env.sys, "platform", "linux")
    return executable


@pytest.mark.parametrize("abi", ["x86_64-linux-gnu", "aarch64-linux-gnu"])
def test_multiarch_plugins_under_selected_prefix(selected_executable, monkeypatch, abi):
    prefix = selected_executable.parent.parent
    plugins = prefix / "lib" / abi / "lmms"
    plugins.mkdir(parents=True)
    (plugins / "libtripleoscillator.so").write_bytes(b"")
    with patch("sysconfig.get_config_var", return_value=abi):
        candidates = lmms_env.plugin_dirs()
    assert plugins in candidates
    # Keep this inventory test independent of any system installation.
    monkeypatch.setattr(lmms_env, "plugin_dirs", lambda: tuple(
        path for path in candidates if path.is_relative_to(prefix)))
    assert lmms_env.get_installed_plugins() == {"tripleoscillator"}


def test_other_abi_and_installation_are_not_added(selected_executable):
    prefix = selected_executable.parent.parent
    host = prefix / "lib" / "x86_64-linux-gnu" / "lmms"
    other_abi = prefix / "lib" / "aarch64-linux-gnu" / "lmms"
    other_install = prefix.parent / "another" / "lib" / "x86_64-linux-gnu" / "lmms"
    for path in (host, other_abi, other_install):
        path.mkdir(parents=True)
    with patch("sysconfig.get_config_var", return_value="x86_64-linux-gnu"):
        candidates = lmms_env.plugin_dirs()
    assert host in candidates
    assert other_abi not in candidates
    assert other_install not in candidates


@pytest.mark.parametrize("abi", [None, ""])
def test_missing_abi_keeps_existing_discovery(selected_executable, abi):
    prefix = selected_executable.parent.parent
    with patch("sysconfig.get_config_var", return_value=abi):
        candidates = lmms_env.plugin_dirs()
    assert prefix / "lib" / "lmms" in candidates
    assert prefix / "lib64" / "lmms" in candidates
    assert Path("/usr/lib/lmms") in candidates
    assert not any("None" in path.parts for path in candidates)
    assert len(candidates) == len(set(candidates))


def test_existing_location_order_is_preserved(selected_executable):
    prefix = selected_executable.parent.parent
    with patch("sysconfig.get_config_var", return_value="x86_64-linux-gnu"):
        candidates = lmms_env.plugin_dirs()
    generic = prefix / "lib" / "lmms"
    lib64 = prefix / "lib64" / "lmms"
    multiarch = prefix / "lib" / "x86_64-linux-gnu" / "lmms"
    assert candidates.index(generic) < candidates.index(lib64) < candidates.index(multiarch)
    assert candidates.index(multiarch) < candidates.index(Path("/usr/lib/lmms"))


def test_explicit_plugin_override_remains_exclusive(tmp_path, monkeypatch):
    configured = tmp_path / "chosen-plugins"
    monkeypatch.setenv("LMMS_PLUGIN_DIR", str(configured))
    with patch.object(lmms_env, "find_executable") as find, patch("sysconfig.get_config_var") as abi:
        assert lmms_env.plugin_dirs() == (configured,)
    find.assert_not_called()
    abi.assert_not_called()


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_other_platforms_keep_their_existing_paths(selected_executable, platform):
    with patch.object(lmms_env.sys, "platform", platform), patch("sysconfig.get_config_var") as abi:
        candidates = lmms_env.plugin_dirs()
    abi.assert_not_called()
    assert selected_executable.parent / "plugins" in candidates


@pytest.mark.skipif(os.name == "nt", reason="POSIX AppDir symlink fixture")
def test_apprun_uses_resolved_executable_prefix(selected_executable, monkeypatch):
    launcher = selected_executable.parent.parent.parent / "AppRun"
    launcher.symlink_to(selected_executable)
    monkeypatch.setattr(lmms_env, "find_executable", lambda: launcher)
    with patch("sysconfig.get_config_var", return_value="x86_64-linux-gnu"):
        assert (selected_executable.parent.parent / "lib" / "x86_64-linux-gnu" / "lmms"
                in lmms_env.plugin_dirs())
