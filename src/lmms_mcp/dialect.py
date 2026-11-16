"""LMMS project file format versions.

LMMS upgrades old files step by step when loading them (``DataFile::upgrade`` in
the LMMS sources). The ``version`` attribute of ``<lmms-project>`` says how many
of those upgrade steps a file already went through. A :class:`Dialect` wraps
that number and answers "how must this file be written so LMMS reads it
correctly?" - element names, key numbering, automation node layout, etc.

Two targets are used for new projects:

* ``LEGACY``  - LMMS 1.2.x layout. Loads natively in 1.2.x and is upgraded
  automatically (and losslessly) by LMMS 1.3+.
* ``MODERN``  - LMMS 1.3 layout.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

# Versions that have an upgrade routine, in order (DataFile::UPGRADE_VERSIONS).
_UPGRADE_VERSIONS = [
    "0.2.1-20070501", "0.2.1-20070508", "0.3.0-rc2", "0.3.0", "0.4.0-20080104",
    "0.4.0-20080118", "0.4.0-20080129", "0.4.0-20080409", "0.4.0-20080607",
    "0.4.0-20080622", "0.4.0-beta1", "0.4.0-rc2", "1.0.99-0", "1.1.0-0",
    "1.1.91-0", "1.2.0-rc3", "1.3.0",
]
# Index of the named upgrade steps in DataFile::UPGRADE_METHODS.
_STEP_1_3_0 = 16
_STEP_AUTOMATION_NODES = 18
_STEP_EXTENDED_NOTE_RANGE = 19
_STEP_MIXER_RENAME = 21
_STEP_CLIP_RENAME = 22
_STEP_NOTE_TYPES = 26
_STEP_NO_HIDDEN_AUTOMATION = 30
CURRENT_FILE_VERSION = 31

# Instruments whose notes LMMS did *not* shift when the key range was extended.
_MIDI_BASED = {"zynaddsubfx", "vestige", "lv2instrument", "carlapatchbay", "carlarack"}

_RENAMES = {  # kind: (legacy name, modern name, upgrade step that renames it)
    "mixer": ("fxmixer", "mixer", _STEP_MIXER_RENAME),
    "mixer_channel": ("fxchannel", "mixerchannel", _STEP_MIXER_RENAME),
    "midi_clip": ("pattern", "midiclip", _STEP_CLIP_RENAME),
    "pattern_clip": ("bbtco", "patternclip", _STEP_CLIP_RENAME),
    "sample_clip": ("sampletco", "sampleclip", _STEP_CLIP_RENAME),
    "automation_clip": ("automationpattern", "automationclip", _STEP_CLIP_RENAME),
    "pattern_track": ("bbtrack", "patterntrack", _STEP_CLIP_RENAME),
}
_INSTRUMENT_RENAMES = {"papu": "freeboy", "OPL2": "opulenz"}


def _version_key(version: str) -> tuple:
    """Sortable key approximating LMMS' ProjectVersion comparison."""
    version = version.replace("svn", "")
    main, _, label = version.partition("-")
    nums = [int(n) for n in re.findall(r"\d+", main)[:3]]
    nums += [0] * (3 - len(nums))
    # A release without pre-release label sorts after its pre-releases.
    return (*nums, 1 if not label else 0, label)


def _legacy_file_version(creator_version: str) -> int:
    key = _version_key(creator_version or "0.0.0")
    return sum(1 for v in _UPGRADE_VERSIONS if _version_key(v) <= key)


@dataclass(frozen=True)
class Dialect:
    file_version: int

    # --- capabilities -------------------------------------------------
    @property
    def extended_keys(self) -> bool:
        """True if note keys are plain MIDI numbers (A4 = 69)."""
        return self.file_version > _STEP_EXTENDED_NOTE_RANGE

    @property
    def automation_out_values(self) -> bool:
        return self.file_version > _STEP_AUTOMATION_NODES

    @property
    def typed_notes(self) -> bool:
        """True if step notes are marked with type="1" instead of negative length."""
        return self.file_version > _STEP_NOTE_TYPES

    @property
    def modern(self) -> bool:
        return self.file_version > _STEP_CLIP_RENAME

    @property
    def label(self) -> str:
        return "1.3" if self.extended_keys else "1.2"

    # --- names --------------------------------------------------------
    def tag(self, kind: str) -> str:
        legacy, modern, step = _RENAMES[kind]
        return modern if self.file_version > step else legacy

    @property
    def mixer_attr(self) -> str:
        return "mixch" if self.file_version > _STEP_MIXER_RENAME else "fxch"

    @property
    def pattern_store_type(self) -> str:
        return "patternstore" if self.file_version > _STEP_CLIP_RENAME else "bbtrackcontainer"

    def instrument_name(self, name: str) -> str:
        """Plugin name as stored in this file version (freeboy was 'papu' etc.)."""
        if self.file_version > _STEP_1_3_0:
            return _INSTRUMENT_RENAMES.get(name, name)
        return {v: k for k, v in _INSTRUMENT_RENAMES.items()}.get(name, name)

    # --- keys ---------------------------------------------------------
    def key_offset(self, instrument: str | None) -> int:
        """Value to add to a MIDI note number to get the stored ``key``."""
        if self.extended_keys or (instrument or "").lower() in _MIDI_BASED:
            return 0
        return -12

    @property
    def base_note(self) -> int:
        """Stored ``basenote`` meaning 'no transposition' (A4)."""
        return 69 if self.extended_keys else 57

    def max_key(self) -> int:
        return 127 if self.extended_keys else 107

    # --- root attributes ----------------------------------------------
    def root_attributes(self) -> dict[str, str]:
        if self.file_version >= CURRENT_FILE_VERSION:
            return {"version": str(self.file_version), "creator": "LMMS",
                    "creatorversion": "1.3.0", "type": "song"}
        return {"version": "1.0", "creator": "LMMS", "creatorversion": "1.2.2", "type": "song"}


LEGACY = Dialect(16)
MODERN = Dialect(CURRENT_FILE_VERSION)


def for_target(target: str) -> Dialect:
    t = str(target).strip().lower()
    if t in ("1.2", "1.2.x", "1.2.2", "legacy", "stable"):
        return LEGACY
    if t in ("1.3", "1.3.x", "1.3.0", "modern", "alpha", "nightly"):
        return MODERN
    raise ValueError(f"Unknown LMMS target version '{target}'. Use '1.2' or '1.3'.")


def detect(root: ET.Element) -> Dialect:
    """Determine the dialect of a loaded project/preset document."""
    version = root.get("version")
    if version is None or version == "1.0":
        return Dialect(_legacy_file_version(root.get("creatorversion", "")))
    try:
        return Dialect(int(version))
    except ValueError:
        return Dialect(_legacy_file_version(root.get("creatorversion", "")))


def normalize(root: ET.Element, dialect: Dialect) -> None:
    """Rename elements/attributes to the canonical names of ``dialect``.

    LMMS tolerates files that mix old and new names (its upgrade steps are
    plain renames), but editing is much simpler with one consistent naming.
    """
    names = {}
    for kind, (legacy, modern, _step) in _RENAMES.items():
        wanted = dialect.tag(kind)
        other = modern if wanted == legacy else legacy
        names[other] = wanted
    store = dialect.pattern_store_type
    attr = dialect.mixer_attr
    other_attr = "fxch" if attr == "mixch" else "mixch"
    for el in root.iter():
        # <pattern>/<midiclip> etc. only exist below <track>; <mixer> below <song>.
        if el.tag in names and el.tag not in ("pattern", "midiclip"):
            el.tag = names[el.tag]
        if el.tag == "trackcontainer" and el.get("type") in ("bbtrackcontainer", "patternstore"):
            el.set("type", store)
        if el.tag in ("instrumenttrack", "sampletrack") and other_attr in el.attrib:
            el.set(attr, el.attrib.pop(other_attr))
        if el.tag == "track":
            for child in el:
                if child.tag in ("pattern", "midiclip"):
                    child.tag = names.get(child.tag, child.tag)
