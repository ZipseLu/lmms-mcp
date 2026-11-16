"""Reading and writing LMMS project files (.mmp, .mmpz, .mpt, .xpf)."""

from __future__ import annotations

import os
import re
import struct
import tempfile
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET

_HEADER = '<?xml version="1.0"?>\n<!DOCTYPE lmms-project>\n'


def parse_xml_bytes(raw: bytes) -> ET.Element:
    """Parse plain or qCompress()-ed LMMS XML."""
    candidates = [raw]
    if len(raw) > 4:
        for payload in (raw[4:], raw):  # Qt's qCompress prepends a 4 byte length
            try:
                candidates.append(zlib.decompress(payload))
            except zlib.error:
                pass
    last_error: Exception | None = None
    for data in candidates:
        try:
            text = data.decode("utf-8", errors="replace")
            text = re.sub(r"<!DOCTYPE[^>\[]*(\[[^\]]*\])?>", "", text, count=1)
            return ET.fromstring(text.lstrip("﻿ \t\r\n"))
        except ET.ParseError as exc:
            last_error = exc
    raise ValueError(f"Not a valid LMMS file: {last_error}")


def read_file(path: str | Path) -> ET.Element:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    root = parse_xml_bytes(path.read_bytes())
    if root.tag not in ("lmms-project", "multimediaproject", "multimedia-project"):
        raise ValueError(f"{path} is not an LMMS file (root element is <{root.tag}>)")
    return root


def serialize(root: ET.Element) -> str:
    ET.indent(root, space="  ")
    return _HEADER + ET.tostring(root, encoding="unicode") + "\n"


def write_file(path: str | Path, root: ET.Element) -> Path:
    """Write the project. ``.mmp``/``.mpt`` are plain XML, everything else is compressed."""
    path = Path(path)
    data = serialize(root).encode("utf-8")
    if path.suffix.lower() not in (".mmp", ".mpt", ".xml", ".xpf"):
        data = struct.pack(">I", len(data)) + zlib.compress(data, 9)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path
