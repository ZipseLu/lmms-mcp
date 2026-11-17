# LMMS MCP Server

An [MCP](https://modelcontextprotocol.io) server that lets AI assistants compose, arrange and mix songs for [LMMS](https://lmms.io), the free open-source DAW.

The server writes regular LMMS project files (`.mmpz` / `.mmp`). You open the result in LMMS, or let the server render it to audio. LMMS does not have to be running while the song is built.

## What it can do

- **Songs from scratch** – tempo, time signature, tracks, notes, arrangement, mix
- **Musical input** – note names (`C4`, `F#3`), a compact melody notation (`"A1:e A1 r A2 A1:q"`), chord symbols (`"Am F C G"`), step-sequencer strings (`"x...x...x...x..."`)
- **Drums** – drum kits from the LMMS factory samples in the Beat/Bassline (pattern) editor, patterns and variations
- **Instruments** – all built-in synths, the sample player, SoundFonts, LMMS presets (`.xpf`) and ZynAddSubFX patches (`.xiz`)
- **Sound shaping** – envelopes, LFOs, filters, arpeggiator, chord stacking
- **Arrangement** – repeat or clear whole sections, audio clips on sample tracks, MIDI file import
- **Mixing** – mixer channels, sends, built-in effects with parameters
- **Automation** – tempo, volumes, pan, filter cutoff, any instrument or effect parameter; LFO controllers
- **Existing projects** – opens and edits projects saved by LMMS 1.0 – 1.3 without losing data it does not understand
- **LMMS 1.2 and 1.3** – writes either file format (see [File format](#file-format))
- **Safety net** – undo, validation ("this track will be silent because…"), failed calls never leave a half-edited project

## Installation

Requires Python 3.10+.

```bash
git clone https://github.com/TypeWolf/lmms-mcp.git
cd lmms-mcp
pip install -e .
```

## Configuration

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "lmms": {
      "command": "python",
      "args": ["-m", "lmms_mcp"],
      "env": {
        "LMMS_PROJECTS_DIR": "C:\\Users\\me\\Documents\\lmms\\projects"
      }
    }
  }
}
```

### opencode (`opencode.json`)

```json
{
  "mcp": {
    "lmms": {
      "type": "local",
      "command": ["python", "-m", "lmms_mcp"],
      "environment": {
        "LMMS_PROJECTS_DIR": "C:\\Users\\me\\Documents\\lmms\\projects"
      }
    }
  }
}
```

### Environment variables

All are optional – an LMMS installed in the default location is found automatically.

| Variable | Purpose |
|----------|---------|
| `LMMS_PROJECTS_DIR` | Folder for relative project paths. Default: `~/Documents/lmms/projects` |
| `LMMS_EXECUTABLE` | Path to `lmms` / `lmms.exe` (for rendering and "open in LMMS") |
| `LMMS_DATA_DIR` | LMMS data folder containing `samples/` and `presets/` (enables the sample and preset browser). Normally `<LMMS install dir>/data`; the `data` folder of an LMMS source checkout works too |
| `LMMS_TARGET_VERSION` | `1.2` or `1.3` – file format of new projects. Default: taken from the installed LMMS |
| `LMMS_USER_DIR` | LMMS working directory with your own `samples/` and `presets/`. Default: `~/Documents/lmms` |
| `LMMS_PLUGIN_DIR` | Folder containing the LMMS plugin libraries if they are not found automatically (AppImage or custom install) |

Ask the assistant to call `get_environment` to see what was detected.

### Plugin availability

The server reads the plugin libraries of the installed LMMS when it can find them. Availability has three states: detected, missing from a nonempty inventory, and unknown. An empty or unreadable inventory, as well as a plugin that is only assumed to be statically linked, is not verification.

`get_environment` reports `plugin_inventory_status`, `instruments_verified_available`, `effects_verified_available`, `instruments_unverified` and `effects_unverified`. The verified fields only mean that a matching plugin file was found; they do not prove that LMMS loads the plugin or produces audio. Offline composition remains supported: `add_instrument_track` and `add_effect` add a warning when availability cannot be checked or the plugin is missing from a nonempty inventory.

Set `LMMS_PLUGIN_DIR` when the plugin folder is not in a standard location (for example an AppImage or a custom install). Linux/AppImage discovery remains best-effort.

## Example

> "Make a 16-bar deep house loop in A minor at 122 BPM and open it in LMMS."

The assistant will typically run something like:

```text
new_project(name="deep-house", tempo=122)
add_drum_kit(kit="electro")
set_beat("Main", {"Kick": "x...x...x...x...", "Clap": "....x.......x...",
                  "Hihat": "..x...x...x...x.", "Openhat": "..............x."})
arrange_pattern("Main", bar=1, length_bars=16)
add_instrument_track("Bass", instrument="lb302", params={"vcf_cut": 0.4}, new_mixer_channel=True)
add_notes("Bass", sequence="A1:e r A1 r:q A1:e C2 r G1:q.", bar=1)
add_instrument_track("Keys", preset="Rhodes")
add_chords("Keys", "Am7 Fmaj7 Cmaj7 G", bar=1, rhythm="x..x..x...x.....")
copy_section(from_bar=1, length_bars=4, to_bar=5, times=3, tracks=["Bass", "Keys"])
add_effect("reverbsc", track="Keys", wet=0.25)
add_automation("cutoff", track="Keys", points=[[1, 600], [9, 9000]])
save_project()
open_in_lmms()
```

## Conventions

| Thing | Convention |
|-------|------------|
| Bars | 1-based position on the song timeline (bar 1 = start); fractions allowed |
| Note start / length | Beats (quarter notes); start counts from the given bar, 0-based |
| Pitch | `C4` = MIDI 60 = middle C (same labels as the LMMS piano roll), or MIDI numbers |
| Velocity, volume | 0–200, 100 = normal (LMMS native) |
| Pan | −100 (left) … 100 (right) |
| Tracks | Referenced by name or index; mixer channels by number (0 = master) |

**Sequence notation** (`add_notes(sequence=…)`): tokens are played one after another.
`C4:1` note for one beat · `r:0.5` rest · `[C4 E4 G4]:2` chord · `E4` reuses the previous duration · `q.`/`e`/`s`/`h`/`w` letter durations · `C4:1@120` velocity · `|` ignored.

**Step strings** (`set_beat`): one character per sixteenth – `x` hit, `X` accent, `o` ghost note, `.` rest.

## Tools

| Area | Tools |
|------|-------|
| Project | `new_project` `open_project` `save_project` `get_project` `set_song` `undo` `validate_project` |
| Tracks | `add_instrument_track` `set_instrument` `update_track` `get_track` `duplicate_track` `move_track` `remove_track` |
| Sound | `set_envelope` `set_filter` `set_arpeggiator` `set_chord_stacking` |
| Notes | `add_notes` `add_chords` `get_notes` `remove_notes` `transpose_notes` |
| Drums | `add_drum_kit` `add_beat_instrument` `set_beat` `arrange_pattern` `duplicate_pattern` |
| Arrangement | `copy_section` `clear_section` `add_sample_track` `add_sample_clip` `import_midi` |
| Mixer / FX | `add_mixer_channel` `update_mixer_channel` `add_effect` `update_effect` |
| Automation | `add_automation` `add_lfo` |
| Library | `list_instruments` `list_effects` `describe_plugin` `list_presets` `list_samples` `music_theory` |
| LMMS | `get_environment` `render_project` `open_in_lmms` |

Resources: `lmms://guide` (workflow and sound-design tips), `lmms://project` (current project).
Prompt: `compose_song(style, tempo, key, bars)`.

## File format

LMMS changed its project format between 1.2 and 1.3 (element names, and note numbers shifted by an octave). The server handles both:

- **`1.2`** – the LMMS 1.2.x layout. LMMS 1.3 upgrades such files automatically when loading, so this is the safe choice and the default when no LMMS installation is detected.
- **`1.3`** – the current layout of LMMS 1.3 (alpha / nightly builds).

When an existing project is opened, it is kept in whatever version it was saved with. Pitches are always given as real MIDI notes; the server converts them.

Effects and instruments that only exist in 1.3 (`compressor`, `lomm`, `dispersion`, `slicert`, …) are flagged when used in a 1.2 project.

## Limitations

- LADSPA, LV2 and VST effects of existing projects are preserved but cannot be created (only the built-in effects can).
- VST instruments (`vestige`) can be referenced by plugin path but not configured.
- `sfxr`, `organic`, `monstro`, `opulenz`, `nes`, `freeboy` and `sid` start with the plugin's default sound; `watsyn`, `vibedstrings` and `xpressive` need a preset.
- Audio clip lengths are detected for wav, ogg and flac; pass `length_beats` for other formats.
- Plugin availability is based on plugin files found in the installed LMMS; file presence does not prove loading or audio output.

## Development

```bash
pip install -e ".[dev]"
pytest

# plugin availability regression tests (no LMMS installation required)
pytest tests/test_plugin_availability.py

# include the tests that load every factory preset and round-trip the LMMS demo songs
LMMS_DATA_DIR=/path/to/lmms/data pytest
```

An optional integration test for MCP SDK 2.x uses a real STDIO client and the selected LMMS
to compose, save, replace the in-memory project, reload and render a short score.
In a separately prepared isolated environment with MCP SDK 2.x, set `LMMS_TEST_STDIO_RENDER=1`
and `LMMS_TEST_EXECUTABLE=/path/to/lmms`, then run
`pytest tests/test_stdio_render.py`. Set `LMMS_PLUGIN_DIR` explicitly if needed;
this test does not require automatic Linux discovery. Optional settings are
`LMMS_TEST_EXPECTED_VERSION` and `LMMS_TEST_ARTIFACT_DIR` (a new directory).

The fixture explicitly selects LMMS format 1.2. It checks pitches, times and
velocities through `get_notes`, track settings through `get_project`, and every
note's key, position, length, volume and pan through independent XML parsing
before reload and after saving the reloaded state again. Per-note pan is not
exposed by `get_notes`; the second save checks that reload preserves it.
`pytest tests/test_stdio_oracle.py` checks that value corruption is
rejected even when note counts remain unchanged.

Without opt-in the real render test skips. The caller provides isolation and
resource limits; the test does not download, extract or install LMMS, call a
model, open the GUI or play audio. WAV duration/energy/clipping checks do not
establish listening quality, VST support, native Windows/macOS compatibility or
all Python/MCP SDK versions.

Layout:

| Module | Content |
|--------|---------|
| `server.py` | MCP tools, resources, prompt |
| `project.py` | The project model – all editing operations on the LMMS XML |
| `dialect.py` | Differences between LMMS file format versions |
| `catalog.py` | Instruments, effects, parameters, drum kits |
| `theory.py` | Pitches, scales, chords, note / step notation |
| `lmms_env.py` | Finding LMMS, samples, presets; rendering |
| `fileio.py` | Reading / writing `.mmp` and `.mmpz` |
| `midi_import.py` | Standard MIDI file reader |

## License

MIT – see [LICENSE](LICENSE).
