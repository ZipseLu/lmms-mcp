"""The LMMS installation on this machine: executable, factory data, rendering."""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import sys
import wave
from functools import lru_cache
from pathlib import Path

AUDIO_EXTENSIONS = {".wav", ".ogg", ".flac", ".mp3", ".aiff", ".aif", ".ds", ".spx", ".voc", ".au", ".raw"}

PRESET_DIRS = {  # folder below data/presets -> instrument plugin
    "AudioFileProcessor": "audiofileprocessor", "BitInvader": "bitinvader", "Kicker": "kicker",
    "LB302": "lb302", "Monstro": "monstro", "Nescaline": "nes", "OpulenZ": "opulenz",
    "Organic": "organic", "SID": "sid", "TripleOscillator": "tripleoscillator",
    "Vibed": "vibedstrings", "Watsyn": "watsyn", "Xpressive": "xpressive",
    "ZynAddSubFX": "zynaddsubfx", "Sfxr": "sfxr", "FreeBoy": "freeboy",
}


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip().strip('"')
    return Path(value).expanduser() if value else None


def projects_dir() -> Path:
    """Folder for relative project paths (``LMMS_PROJECTS_DIR``)."""
    configured = _env_path("LMMS_PROJECTS_DIR")
    if configured:
        return configured
    for candidate in (Path.home() / "Documents" / "lmms" / "projects", Path.home() / "lmms" / "projects"):
        if candidate.is_dir():
            return candidate
    return Path.home() / "Documents" / "lmms" / "projects"


@lru_cache(maxsize=1)
def find_executable() -> Path | None:
    configured = _env_path("LMMS_EXECUTABLE")
    if configured and configured.is_file():
        return configured
    found = shutil.which("lmms")
    if found:
        return Path(found)
    candidates = []
    if sys.platform.startswith("win"):
        for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                candidates += [Path(base) / "LMMS" / "lmms.exe", Path(base) / "Programs" / "LMMS" / "lmms.exe"]
    elif sys.platform == "darwin":
        candidates += [Path("/Applications/LMMS.app/Contents/MacOS/lmms"),
                       Path.home() / "Applications/LMMS.app/Contents/MacOS/lmms"]
    else:
        candidates += [Path("/usr/bin/lmms"), Path("/usr/local/bin/lmms"),
                       Path("/var/lib/flatpak/exports/bin/io.lmms.LMMS")]
    return next((c for c in candidates if c.is_file()), None)


@lru_cache(maxsize=1)
def version() -> str | None:
    """Version of the installed LMMS, e.g. '1.2.2' (None if unknown)."""
    configured = os.environ.get("LMMS_VERSION", "").strip()
    if configured:
        return configured
    exe = find_executable()
    if exe is None:
        return None
    try:
        out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=15,
                             stdin=subprocess.DEVNULL)
        match = re.search(r"(\d+\.\d+(?:\.\d+)?(?:-[\w.]+)?)", out.stdout + out.stderr)
        if match:
            return match.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    # No usable output (common for the Windows GUI build): plugins that only exist in 1.3 tell.
    base = exe.resolve().parent
    for folder in (base / "plugins", base.parent / "lib" / "lmms", base.parent / "lib64" / "lmms"):
        if folder.is_dir():
            names = {f.stem.lower().removeprefix("lib") for f in folder.iterdir()}
            return "1.3" if names & {"slicert", "lomm", "compressor", "dispersion"} else "1.2"
    return None


def default_target() -> str:
    """File format for new projects: 'LMMS_TARGET_VERSION', else the installed LMMS, else 1.2."""
    configured = os.environ.get("LMMS_TARGET_VERSION", "").strip()
    if configured:
        return configured
    ver = version()
    if ver:
        nums = [int(n) for n in re.findall(r"\d+", ver)[:2]]
        if len(nums) == 2 and tuple(nums) >= (1, 3):
            return "1.3"
    return "1.2"  # the 1.2 layout is understood by every LMMS version


@lru_cache(maxsize=1)
def data_dir() -> Path | None:
    """LMMS' factory data folder (contains samples/, presets/, projects/)."""
    candidates = []
    configured = _env_path("LMMS_DATA_DIR")
    if configured:
        candidates.append(configured)
    exe = find_executable()
    if exe:
        base = exe.resolve().parent
        candidates += [base / "data", base.parent / "share" / "lmms", base.parent / "data",
                       base.parent / "Resources" / "share" / "lmms"]
    candidates += [Path("/usr/share/lmms"), Path("/usr/local/share/lmms"),
                   Path("/Applications/LMMS.app/Contents/share/lmms")]
    for candidate in candidates:
        if (candidate / "samples").is_dir() or (candidate / "presets").is_dir():
            return candidate
    return None


def user_dir() -> Path | None:
    configured = _env_path("LMMS_USER_DIR")
    for candidate in (configured, Path.home() / "Documents" / "lmms", Path.home() / "lmms"):
        if candidate and candidate.is_dir():
            return candidate
    return None


# ---------------------------------------------------------------------------
# samples
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _factory_samples() -> tuple[str, ...]:
    base = data_dir()
    if base is None or not (base / "samples").is_dir():
        return ()
    root = base / "samples"
    return tuple(sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS))


def list_samples(category: str | None = None, search: str | None = None) -> dict:
    samples = _factory_samples()
    categories: dict[str, int] = {}
    for s in samples:
        cat = s.split("/")[0] if "/" in s else "(root)"
        categories[cat] = categories.get(cat, 0) + 1
    selected = list(samples)
    if category:
        selected = [s for s in selected if s.lower().startswith(category.lower().rstrip("/") + "/")]
    if search:
        words = search.lower().split()
        selected = [s for s in selected if all(w in s.lower() for w in words)]
    return {"categories": categories, "samples": selected}


def resolve_sample(reference: str) -> tuple[str, Path | None, str | None]:
    """Turn a user supplied sample reference into (stored path, absolute path, warning)."""
    ref = reference.strip().strip('"')
    if not ref:
        raise ValueError("Empty sample path")
    for prefix in ("factorysample:", "usersample:"):
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
    path = Path(ref).expanduser()
    if path.is_absolute():
        if path.is_file():
            return path.as_posix(), path, None
        return path.as_posix(), None, f"File not found: {path}"
    normalized = ref.replace("\\", "/")
    samples = _factory_samples()
    base = data_dir()
    if normalized in samples:
        return normalized, base / "samples" / normalized, None
    user = user_dir()
    if user and (user / "samples" / normalized).is_file():
        return normalized, user / "samples" / normalized, None
    # forgiving lookup: "kick01", "drums/kick01", "kick_hard"
    stem = normalized.lower().rsplit(".", 1)[0] if "." in normalized.split("/")[-1] else normalized.lower()
    matches = [s for s in samples if s.lower().rsplit(".", 1)[0] == stem]
    matches = matches or [s for s in samples if s.lower().rsplit(".", 1)[0].split("/")[-1] == stem.split("/")[-1]]
    # LMMS 1.3 renamed its loops to "<name> - <tempo> BPM.ogg"
    matches = matches or [s for s in samples if s.lower().startswith(stem + " - ")]
    if len(matches) == 1:
        return matches[0], base / "samples" / matches[0], None
    if len(matches) > 1:
        raise ValueError(f"Sample '{reference}' is ambiguous: {', '.join(matches[:8])}")
    if samples:
        close = [s for s in samples if stem.split("/")[-1][:4] in s.lower()][:8]
        hint = f" Similar: {', '.join(close)}." if close else ""
        raise ValueError(f"Sample '{reference}' not found in the LMMS factory samples.{hint} "
                         f"Use list_samples or an absolute file path.")
    return normalized, None, ("Could not verify the sample because the LMMS data folder was not "
                              "found (set LMMS_DATA_DIR).")


def sample_exists(stored: str) -> bool:
    """Whether a stored sample path can be found (True when it cannot be checked)."""
    try:
        _stored, absolute, warning = resolve_sample(stored)
    except ValueError:
        return False
    if absolute is None and warning and "verify" in warning:
        return True
    return absolute is not None


def audio_duration(path: Path) -> float | None:
    """Length of an audio file in seconds (wav, ogg vorbis, flac); None if unknown."""
    try:
        suffix = path.suffix.lower()
        if suffix == ".wav":
            with wave.open(str(path), "rb") as w:
                return w.getnframes() / float(w.getframerate())
        data = path.read_bytes()
        if suffix == ".flac" and data[:4] == b"fLaC":
            info = data[8:8 + 34]
            rate = (info[10] << 12) | (info[11] << 4) | (info[12] >> 4)
            total = ((info[13] & 0x0F) << 32) | struct.unpack(">I", info[14:18])[0]
            return total / rate if rate else None
        if data[:4] == b"OggS":
            marker = data.find(b"\x01vorbis")
            if marker < 0:
                return None
            rate = struct.unpack("<I", data[marker + 12:marker + 16])[0]
            last = data.rfind(b"OggS")
            granule = struct.unpack("<q", data[last + 6:last + 14])[0]
            return granule / rate if rate and granule > 0 else None
    except (OSError, wave.Error, struct.error, IndexError, EOFError):
        return None
    return None


# ---------------------------------------------------------------------------
# presets
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _presets() -> tuple[tuple[str, str, str], ...]:
    """(instrument, 'Folder/Sub/Name', absolute path) for all factory + user presets."""
    found = []
    roots = [d / "presets" for d in (data_dir(), user_dir()) if d]
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix.lower() not in (".xpf", ".xiz") or not path.is_file():
                continue
            rel = path.relative_to(root)
            instrument = PRESET_DIRS.get(rel.parts[0], rel.parts[0].lower()) if len(rel.parts) > 1 else ""
            found.append((instrument, rel.with_suffix("").as_posix(), str(path)))
    return tuple(found)


def list_presets(instrument: str | None = None, search: str | None = None) -> dict:
    presets = _presets()
    counts: dict[str, int] = {}
    for inst, _name, _path in presets:
        counts[inst] = counts.get(inst, 0) + 1
    selected = [(i, n) for i, n, _p in presets]
    if instrument:
        selected = [(i, n) for i, n in selected if i == instrument.lower()]
    if search:
        words = search.lower().split()
        selected = [(i, n) for i, n in selected if all(w in n.lower() for w in words)]
    return {"presets_per_instrument": counts, "presets": [n for _i, n in selected]}


def _plain(name: str) -> str:
    """Comparable preset name: no folder, no numeric prefix, no punctuation."""
    name = name.replace("\\", "/").split("/")[-1].lower()
    name = re.sub(r"\.(xpf|xiz)$", "", name)
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"^\d+[-_ ]+", "", name))


def find_preset(reference: str, instrument: str | None = None) -> tuple[str, Path]:
    """Locate a preset by path, 'Folder/Name' or (fuzzy) name. Returns (instrument, path)."""
    ref = reference.strip().strip('"')
    direct = Path(ref).expanduser()
    if direct.is_absolute():
        if direct.is_file():
            return ("zynaddsubfx" if direct.suffix.lower() == ".xiz" else ""), direct
        raise ValueError(f"Preset file not found: {direct}")
    presets = _presets()
    if not presets:
        raise ValueError("No presets available: the LMMS data folder was not found. Set the "
                         "LMMS_DATA_DIR environment variable to LMMS' data folder.")
    pool = [p for p in presets if not instrument or p[0] == instrument.lower()] or list(presets)
    wanted = re.sub(r"\.(xpf|xiz)$", "", ref.replace("\\", "/"), flags=re.I).lower()
    exact = [p for p in pool if p[1].lower() == wanted]
    if not exact:
        exact = [p for p in pool if p[1].lower().endswith("/" + wanted)]
    if not exact:
        exact = [p for p in pool if _plain(p[1]) == _plain(wanted)]
    if not exact:
        partial = [p for p in pool if _plain(wanted) and _plain(wanted) in _plain(p[1])]
        if len(partial) == 1:
            exact = partial
        elif partial:
            names = ", ".join(p[1] for p in partial[:10])
            raise ValueError(f"Preset '{reference}' is ambiguous. Candidates: {names}")
    if not exact:
        raise ValueError(f"Preset '{reference}' not found. Use list_presets to browse.")
    return exact[0][0], Path(exact[0][2])


# ---------------------------------------------------------------------------
# running LMMS
# ---------------------------------------------------------------------------

def render(project: Path, output: Path, fmt: str = "wav", loop: bool = False,
           timeout: int = 900) -> dict:
    exe = find_executable()
    if exe is None:
        raise RuntimeError("LMMS was not found. Install LMMS or set LMMS_EXECUTABLE to lmms(.exe).")
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [str(exe), "--render", str(project), "--output", str(output), "--format", fmt]
    if loop:
        command.append("--loop")
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Rendering did not finish within {timeout} seconds") from None
    log = (proc.stdout + "\n" + proc.stderr).strip()
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError(f"LMMS did not produce {output.name} (exit code {proc.returncode}). "
                           f"Output: {log[-800:] or '(none)'}")
    return {"output": str(output), "size_kb": round(output.stat().st_size / 1024),
            "log_tail": log[-400:]}


def open_in_lmms(project: Path) -> None:
    exe = find_executable()
    if exe is None:
        raise RuntimeError("LMMS was not found. Install LMMS or set LMMS_EXECUTABLE to lmms(.exe).")
    kwargs = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # detached, new process group
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([str(exe), str(project)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, **kwargs)
