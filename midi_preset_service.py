#!/usr/bin/env python3
"""
MIDI SysEx Preset Manager

A background service that listens for custom MIDI SysEx commands to record
and recall CC/PC presets. Presets are identified by note-on messages and
stored as human-readable YAML with configurable CC name mappings.

Supports multiple "destinations" per preset — logical groupings of CCs that
can each use their own CC-name mapping.  Destinations are configured in
destinations.yaml and referenced by number (0–127) in the SysEx protocol.

Protocol (SysEx, mfr 0x7D by default):
  F0 7D <dev> 01 [<dest>] F7  -  Start / switch recording [for destination]
  F0 7D <dev> 02 F7            -  Save preset (keyed by last note-on)
  F0 7D <dev> 03 F7            -  Load preset (next note-on selects which)
  F0 7D <dev> 04 <ascii…> F7   -  Preset name (sent back to device on recall)
  F0 7D <dev> 05 <dest> F7     -  Destination marker (sent during recall)
  F0 7D <dev> 06 F7            -  Abandon recording (cancel without saving)

Usage:
  python midi_preset_service.py [--config-dir DIR] [--list-ports]
"""

import argparse
import signal
import sys
from pathlib import Path

try:
    import mido
except ImportError:
    print("Error: 'mido' package not found.")
    print("Install dependencies:  pip install -r requirements.txt")
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("Error: 'pyyaml' package not found.")
    print("Install dependencies:  pip install -r requirements.txt")
    sys.exit(1)


# --- Defaults ----------------------------------------------------------------

DEFAULT_CONFIG_DIR = Path.home() / ".midi-sx-presets"
PRESETS_SUBDIR = "presets"
CC_NAMES_FILENAME = "cc_names.yaml"
CONFIG_FILENAME = "config.yaml"
DESTINATIONS_FILENAME = "destinations.yaml"

# SysEx protocol
SYSEX_MANUFACTURER_ID = 0x7D  # Non-commercial / educational use
DEFAULT_DEVICE_ID = 0x01

# Commands
CMD_START_RECORD = 0x01
CMD_SAVE_PRESET = 0x02
CMD_LOAD_PRESET = 0x03
CMD_PRESET_NAME = 0x04   # Sent back to the device on recall
CMD_DEST_MARKER = 0x05   # Sent before each destination's CCs during recall
CMD_ABANDON = 0x06        # Cancel current recording without saving


# --- YAML helpers ------------------------------------------------------------

def _yaml_dump(data, path):
    """Write data to a YAML file with readable formatting."""
    with open(path, "w") as fh:
        yaml.dump(data, fh, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _yaml_load(path):
    """Read a YAML file, returning an empty dict on missing/empty files."""
    if not path.exists():
        return {}
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


# --- Service -----------------------------------------------------------------

class MidiPresetService:
    """Core service: listens on a virtual MIDI port for SysEx commands and
    records / recalls CC + PC presets stored as YAML."""

    def __init__(self, config_dir=None):
        self.config_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
        self.config_dir.mkdir(parents=True, exist_ok=True)

        self.config = self._load_or_create_config()
        self.cc_name_sets, self.cc_default_names = self._load_cc_names()
        self.destinations_map = self._load_destinations()
        self.presets = self._load_presets()

        # Build reverse lookups (name -> number) for each set
        self.cc_reverse_sets = {
            name: {v: k for k, v in mapping.items()}
            for name, mapping in self.cc_name_sets.items()
        }
        self.cc_reverse_default = {v: k for k, v in self.cc_default_names.items()}

        # Protocol IDs (configurable via config.yaml)
        self.manufacturer_id = self.config.get("manufacturer_id", SYSEX_MANUFACTURER_ID)
        self.device_id = self.config.get("device_id", DEFAULT_DEVICE_ID)

        # Runtime state
        self.recording = False
        self.load_mode = False
        self.multi_dest = False       # True when START_RECORD included a dest byte
        self.active_dest = 0          # Currently-recording destination number
        self.dest_data = {}           # dest_num -> {"ccs": {cc_num: val}, "pc": int|None}
        self.last_note = None         # (channel, note_number)

        # MIDI ports (opened in run())
        self.midi_in = None
        self.midi_out = None

    # -- Config / persistence -------------------------------------------------

    def _load_or_create_config(self):
        path = self.config_dir / CONFIG_FILENAME
        if path.exists():
            return _yaml_load(path)
        defaults = {
            "port_name": "MIDI SX Presets",
            "device_id": DEFAULT_DEVICE_ID,
            "manufacturer_id": SYSEX_MANUFACTURER_ID,
        }
        _yaml_dump(defaults, path)
        return defaults

    def _load_cc_names(self):
        """Load CC name mappings.  Returns (name_sets_dict, default_set).

        Supports two formats in cc_names.yaml:
          Flat:   cc_names: {1: modulation, …}
          Named:  cc_name_sets: {set_name: {1: modulation, …}, …}
        """
        path = self.config_dir / CC_NAMES_FILENAME
        if path.exists():
            data = _yaml_load(path)
            if "cc_name_sets" in data:
                sets = data["cc_name_sets"] or {}
                return sets, sets.get("default", {})
            if "cc_names" in data:
                flat = data["cc_names"] or {}
                return {"default": flat}, flat
        # Create default file (flat format)
        default_names = {
            1: "modulation",
            7: "volume",
            10: "pan",
            11: "expression",
            64: "sustain",
            74: "filter_cutoff",
        }
        _yaml_dump({"cc_names": default_names}, path)
        return {"default": default_names}, default_names

    def _load_destinations(self):
        """Load the optional destinations map (destinations.yaml)."""
        path = self.config_dir / DESTINATIONS_FILENAME
        if path.exists():
            data = _yaml_load(path)
            return data.get("destinations", {})
        return {}

    def _presets_dir(self):
        return self.config_dir / PRESETS_SUBDIR

    @staticmethod
    def _preset_filename(note):
        return f"preset-{note:03d}.yaml"

    def _load_presets(self):
        pdir = self._presets_dir()
        if not pdir.is_dir():
            pdir.mkdir(parents=True, exist_ok=True)
            return {}
        presets = {}
        for path in sorted(pdir.glob("preset-*.yaml")):
            try:
                note = int(path.stem.split("-", 1)[1])
            except (IndexError, ValueError):
                continue
            data = _yaml_load(path)
            if data:
                presets[note] = data
        return presets

    def _save_preset_to_disk(self, note):
        pdir = self._presets_dir()
        pdir.mkdir(parents=True, exist_ok=True)
        _yaml_dump(self.presets[note], pdir / self._preset_filename(note))

    # -- CC name <-> number ---------------------------------------------------

    def _cc_names_for_dest(self, dest):
        """Return the CC-number-to-name dict for a destination."""
        if dest is not None and dest in self.destinations_map:
            set_name = self.destinations_map[dest].get("cc_names", "default")
            if set_name in self.cc_name_sets:
                return self.cc_name_sets[set_name]
        return self.cc_default_names

    def _cc_num_to_name(self, cc_num, dest=None):
        return self._cc_names_for_dest(dest).get(cc_num, cc_num)

    def _cc_name_to_number(self, name, dest=None):
        """Resolve a CC name (or raw int) back to a CC number."""
        if isinstance(name, int):
            return name
        # Try the destination-specific set first
        if dest is not None and dest in self.destinations_map:
            set_name = self.destinations_map[dest].get("cc_names", "default")
            reverse = self.cc_reverse_sets.get(set_name, {})
            if name in reverse:
                return reverse[name]
        # Fall back to default set
        if name in self.cc_reverse_default:
            return self.cc_reverse_default[name]
        try:
            return int(name)
        except (ValueError, TypeError):
            return None

    # -- SysEx helpers --------------------------------------------------------

    def _is_ours(self, data):
        return (len(data) >= 3
                and data[0] == self.manufacturer_id
                and data[1] == self.device_id)

    @staticmethod
    def _encode_name(name):
        """Encode a preset name as 7-bit-safe bytes for SysEx (max 64 chars)."""
        return [min(ord(c), 127) for c in str(name)[:64]]

    def _dest_display(self, dest):
        """Human-readable label for a destination number."""
        if dest in self.destinations_map:
            dname = self.destinations_map[dest].get("name", "")
            if dname:
                return f"{dest} ({dname})"
        return str(dest)

    # -- Command handlers -----------------------------------------------------

    def _handle_sysex(self, data):
        if not self._is_ours(data):
            return
        cmd = data[2]
        if cmd == CMD_START_RECORD:
            dest = data[3] if len(data) > 3 else None
            self._cmd_start_record(dest)
        elif cmd == CMD_SAVE_PRESET:
            self._cmd_save_preset()
        elif cmd == CMD_LOAD_PRESET:
            self._cmd_load_preset()
        elif cmd == CMD_ABANDON:
            self._cmd_abandon()

    def _cmd_start_record(self, dest=None):
        if not self.recording:
            # Begin a new recording session
            self.recording = True
            self.load_mode = False
            self.dest_data = {}
            self.last_note = None

            if dest is not None:
                self.multi_dest = True
                self.active_dest = dest
                self.dest_data[dest] = {"ccs": {}, "pc": None}
                _log("REC", f"Recording started — destination {self._dest_display(dest)}")
            else:
                self.multi_dest = False
                self.active_dest = 0
                self.dest_data[0] = {"ccs": {}, "pc": None}
                _log("REC", "Recording started")
        else:
            # Already recording — switch to a (possibly new) destination
            if dest is not None:
                self.multi_dest = True
                self.active_dest = dest
                if dest not in self.dest_data:
                    self.dest_data[dest] = {"ccs": {}, "pc": None}
                _log("REC", f"Switched to destination {self._dest_display(dest)}")
            else:
                _log("WARN", "Already recording — include a dest byte to switch destination.")

    def _cmd_save_preset(self):
        if not self.recording:
            _log("WARN", "Save requested but not currently recording.")
            return
        if self.last_note is None:
            _log("WARN", "Save requested but no note-on was received during recording.")
            self.recording = False
            return

        channel, note = self.last_note

        # Check write-protection
        existing = self.presets.get(note, {})
        if existing.get("read_only", False):
            _log("DENY", f"Preset {note} is read-only — save rejected.")
            self.recording = False
            return

        # Assemble preset, preserving user-set name / read_only
        preset = {
            "name": existing.get("name", f"Preset {note}"),
            "read_only": existing.get("read_only", False),
            "channel": channel,
        }

        if self.multi_dest:
            destinations = {}
            for dnum, dd in self.dest_data.items():
                entry = {}
                if dd["pc"] is not None:
                    entry["program_change"] = dd["pc"]
                if dd["ccs"]:
                    cc_data = {}
                    for cc_num, value in dd["ccs"].items():
                        cc_data[self._cc_num_to_name(cc_num, dnum)] = value
                    entry["cc_values"] = cc_data
                if entry:
                    destinations[dnum] = entry
            if destinations:
                preset["destinations"] = destinations
        else:
            dd = self.dest_data.get(0, {"ccs": {}, "pc": None})
            if dd["pc"] is not None:
                preset["program_change"] = dd["pc"]
            if dd["ccs"]:
                cc_data = {}
                for cc_num, value in dd["ccs"].items():
                    cc_data[self._cc_num_to_name(cc_num)] = value
                preset["cc_values"] = cc_data

        self.presets[note] = preset
        self._save_preset_to_disk(note)

        total_ccs = sum(len(dd["ccs"]) for dd in self.dest_data.values())
        extra = f" across {len(self.dest_data)} destination(s)" if self.multi_dest else ""
        _log("SAVE", f"Preset {note} saved ({total_ccs} CCs{extra}).")
        self.recording = False

    def _cmd_load_preset(self):
        self.load_mode = True
        self.recording = False
        _log("LOAD", "Load mode — send a note-on to select the preset to recall.")

    def _cmd_abandon(self):
        if self.recording:
            self.recording = False
            self.dest_data = {}
            self.last_note = None
            _log("ABANDON", "Recording abandoned.")
        else:
            _log("WARN", "Abandon requested but not currently recording.")

    # -- Recall ---------------------------------------------------------------

    def _recall_preset(self, note):
        """Send all stored CC/PC values for *note* back out the MIDI port."""
        preset = self.presets.get(note)
        if preset is None:
            _log("WARN", f"No preset stored for note {note}.")
            return

        name = preset.get("name", "")
        channel = preset.get("channel", 0)
        _log("RECALL", f"Preset {note}: \"{name}\"")

        if "destinations" in preset:
            self._recall_multi_dest(preset, channel)
        else:
            self._recall_single(preset, channel)

        # Send preset name back via SysEx
        if name:
            name_bytes = self._encode_name(name)
            sysex_data = [self.manufacturer_id, self.device_id, CMD_PRESET_NAME] + name_bytes
            self.midi_out.send(mido.Message("sysex", data=sysex_data))
            _log("  ->", f"Name: \"{name}\"")

    def _recall_single(self, preset, channel):
        """Recall a preset that has no destination grouping."""
        pc = preset.get("program_change")
        if pc is not None:
            self.midi_out.send(mido.Message("program_change", channel=channel, program=pc))
            _log("  ->", f"PC {pc}")
        for cc_name, value in preset.get("cc_values", {}).items():
            cc_num = self._cc_name_to_number(cc_name)
            if cc_num is None:
                _log("WARN", f"Cannot resolve CC '{cc_name}' — skipping.")
                continue
            self.midi_out.send(
                mido.Message("control_change", channel=channel, control=cc_num, value=value)
            )
            _log("  ->", f"CC {cc_name}({cc_num}) = {value}")

    def _recall_multi_dest(self, preset, channel):
        """Recall a preset with destination grouping, sending SysEx markers."""
        for dest_num, dest_data in preset["destinations"].items():
            dest_num = int(dest_num)  # YAML may store as string
            # Destination marker
            marker = [self.manufacturer_id, self.device_id, CMD_DEST_MARKER, dest_num]
            self.midi_out.send(mido.Message("sysex", data=marker))
            _log("  ->", f"Dest {self._dest_display(dest_num)}")

            pc = dest_data.get("program_change")
            if pc is not None:
                self.midi_out.send(
                    mido.Message("program_change", channel=channel, program=pc)
                )
                _log("  ->", f"  PC {pc}")

            for cc_name, value in dest_data.get("cc_values", {}).items():
                cc_num = self._cc_name_to_number(cc_name, dest_num)
                if cc_num is None:
                    _log("WARN", f"  Cannot resolve CC '{cc_name}' — skipping.")
                    continue
                self.midi_out.send(
                    mido.Message("control_change", channel=channel, control=cc_num, value=value)
                )
                _log("  ->", f"  CC {cc_name}({cc_num}) = {value}")

    # -- Main message handler -------------------------------------------------

    def _handle_message(self, msg):
        if msg.type == "sysex":
            self._handle_sysex(msg.data)
            return

        # Note-on (velocity > 0)
        if msg.type == "note_on" and msg.velocity > 0:
            if self.load_mode:
                self.load_mode = False
                self._recall_preset(msg.note)
                return
            self.last_note = (msg.channel, msg.note)
            if self.recording:
                _log("NOTE", f"ch={msg.channel} note={msg.note} (preset tag)")
            return

        # Track CCs / PCs while recording
        if self.recording:
            dd = self.dest_data.get(self.active_dest)
            if dd is None:
                return
            if msg.type == "control_change":
                dd["ccs"][msg.control] = msg.value
                dest_tag = f" [dest {self.active_dest}]" if self.multi_dest else ""
                name = self._cc_num_to_name(
                    msg.control, self.active_dest if self.multi_dest else None
                )
                _log("CC", f"{name}({msg.control}) = {msg.value}{dest_tag}")
            elif msg.type == "program_change":
                dd["pc"] = msg.program
                dest_tag = f" [dest {self.active_dest}]" if self.multi_dest else ""
                _log("PC", f"program={msg.program}{dest_tag}")

    # -- Run loop -------------------------------------------------------------

    def run(self):
        port_name = self.config.get("port_name", "MIDI SX Presets")
        dest_count = len(self.destinations_map)
        cc_count = sum(len(s) for s in self.cc_name_sets.values())

        _log("INIT", "MIDI SysEx Preset Manager")
        _log("INIT", f"Config dir   : {self.config_dir}")
        _log("INIT", f"Virtual port : {port_name}")
        _log("INIT", f"Device ID    : 0x{self.device_id:02X}")
        _log("INIT", f"Manufacturer : 0x{self.manufacturer_id:02X}")
        _log("INIT", f"Presets      : {len(self.presets)} loaded")
        _log("INIT", f"CC names     : {cc_count} mappings in {len(self.cc_name_sets)} set(s)")
        if dest_count:
            _log("INIT", f"Destinations : {dest_count} configured")
            for dnum, dinfo in self.destinations_map.items():
                _log("INIT", f"  {dnum}: {dinfo.get('name', '?')} "
                     f"(cc_names: {dinfo.get('cc_names', 'default')})")

        print()
        mid = self.manufacturer_id
        dev = self.device_id
        print("SysEx commands (hex bytes including F0/F7):")
        print(f"  Start Record  : F0 {mid:02X} {dev:02X} {CMD_START_RECORD:02X} [<dest>] F7")
        print(f"  Save Preset   : F0 {mid:02X} {dev:02X} {CMD_SAVE_PRESET:02X} F7")
        print(f"  Load Preset   : F0 {mid:02X} {dev:02X} {CMD_LOAD_PRESET:02X} F7")
        print(f"  Abandon       : F0 {mid:02X} {dev:02X} {CMD_ABANDON:02X} F7")
        print(f"  (Name reply)  : F0 {mid:02X} {dev:02X} {CMD_PRESET_NAME:02X} <ascii> F7")
        print(f"  (Dest marker) : F0 {mid:02X} {dev:02X} {CMD_DEST_MARKER:02X} <dest> F7")
        print()
        print("Listening… (Ctrl-C to quit)")
        print()

        try:
            self.midi_in = mido.open_input(port_name, virtual=True)
            self.midi_out = mido.open_output(port_name, virtual=True)

            for msg in self.midi_in:
                self._handle_message(msg)

        except KeyboardInterrupt:
            print("\nShutting down.")
        except OSError as exc:
            _log("ERROR", f"Could not open virtual MIDI port: {exc}")
            _log("ERROR", "Make sure python-rtmidi is installed:  pip install python-rtmidi")
            sys.exit(1)
        finally:
            if self.midi_in:
                self.midi_in.close()
            if self.midi_out:
                self.midi_out.close()


# --- Utilities ---------------------------------------------------------------

def _log(tag, message):
    print(f"[{tag:>7s}] {message}")


def list_ports():
    """Print all available MIDI input and output ports."""
    print("MIDI Input ports:")
    for name in mido.get_input_names():
        print(f"  - {name}")
    print()
    print("MIDI Output ports:")
    for name in mido.get_output_names():
        print(f"  - {name}")


# --- Entry point -------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="MIDI SysEx Preset Manager — record and recall CC/PC presets via SysEx."
    )
    parser.add_argument(
        "--config-dir",
        type=str,
        default=None,
        help=f"Configuration / preset directory (default: {DEFAULT_CONFIG_DIR})",
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="List available MIDI ports and exit.",
    )
    args = parser.parse_args()

    if args.list_ports:
        list_ports()
        sys.exit(0)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    service = MidiPresetService(config_dir=args.config_dir)
    service.run()


if __name__ == "__main__":
    main()
