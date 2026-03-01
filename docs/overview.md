# MIDI SX Presets — Overview

A background MIDI service that records and recalls CC/PC presets via SysEx commands. Designed to sit between a DAW (e.g. MainStage) and hardware synths, acting as a transparent MIDI proxy that intercepts custom SysEx messages for preset management while forwarding all other MIDI traffic.

## Architecture

```
  MainStage Scripter  →  IAC Bus  →  [midi_preset_service.py]  →  Hardware Output
                                              ↕
                          IAC Return ←  [service]   (preset recall, name, commands)
                                              ↑
                     Extra HW Inputs  ────────┘   (Launchpad, controllers, etc.)
```

The service has two operating modes:

- **Standalone** — creates a virtual MIDI port; both input and output go through it.
- **Proxy** — sits between IAC buses and a hardware output, with optional extra hardware inputs (controllers, pads). This is the primary production mode.

## Core Concepts

| Concept | Description |
|---|---|
| **Preset** | A YAML snapshot of all destination CC values, stored per note number per channel bank. |
| **Destination** | A named (channel, CC) pair — e.g. `poly_vcf_cutoff` → ch2/CC1. Presets save and restore destinations by name. |
| **CC Set** | A named group of CC-number-to-name mappings (e.g. `polysynth_ccs`, `mix_ccs`). Referenced by destinations. |
| **CC Mapping** | Rules that route incoming CCs to output CCs, with shift-state filtering, relative encoding, channel remapping, and more. |
| **Note Mapping** | Rules for note range filtering, transposition, channel remapping, and polyphony management. |
| **Shift State** | A 16-bit bitmask driven by shift CCs and joystick CCs, used to select which CC/note mapping actions fire. |
| **Tag** | A label on destination parameters; presets filter which parameters to save/recall based on bank tags. |
| **Transport** | CCs 115–119 (Rewind, Forward, Stop, Play, Rec) are intercepted for preset navigation and mode switching. |

## Files

| File | Purpose |
|---|---|
| `midi_preset_service.py` | Main service — all runtime logic. |
| `midi_send.py` | CLI utility to send a text command to the service via SysEx. |
| `config.yaml` | Port names, routing, device IDs, bank tags, misc settings. |
| `cc_sets.yaml` | CC-number-to-name mappings, with optional defaults and tags. |
| `mappings.yaml` | Shift/joystick definitions, CC routing rules, note range mappings. |
| `destinations.yaml` | Logical groupings of channels → CC sets, with prefixes and overrides. |
| `state.yaml` | Persisted runtime state (destination values, cursor, shift, seq counters). |
| `presets/` | Directory of per-note YAML preset files (`preset-ch1-048.yaml`, etc.). |
| `start` | Shell script to launch the service. |
| `poly_synth_scripter.js` | MainStage Scripter plugin — now a pass-through (logic moved to Python). |
| `shift_broadcaster_scripter.js` | MainStage Scripter that rebroadcasts shift/joystick CCs to other channels. |
| `requirements.txt` | Python dependencies: `mido`, `python-rtmidi`, `pyyaml`. |

## Quick Start

```bash
pip install -r requirements.txt
python midi_preset_service.py --config-dir ~/.midi-sx-presets
# or simply:
./start
```

Use `--list-ports` to see available MIDI port names. Use `--no-input` or `--no-iac-input` to selectively disable inputs.
