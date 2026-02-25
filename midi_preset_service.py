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
  F0 7D <dev> 07 <ascii…> F7   -  Debug text (printed to console, supports ANSI)
  F0 7D <dev> 08 <ascii…> F7   -  Remote command (sent to device via return port)
  F0 7D <dev> 09 <ascii…> F7   -  Device command (device → service, machine-readable)

Usage:
  python midi_preset_service.py [--config-dir DIR] [--list-ports]
"""

import argparse
import signal
import sys
import time
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
CMD_DEBUG_TEXT = 0x07     # Arbitrary text → console (supports ANSI escapes)
CMD_REMOTE_CMD = 0x08     # Text command sent to device via return port
CMD_DEVICE_CMD = 0x09     # Text command from device → service (machine-readable)

# Transport CC numbers (from Scripter output)
CC_REWIND = 115
CC_FORWARD = 116
CC_STOP = 117
CC_PLAY = 118
CC_REC = 119
TRANSPORT_CCS = {CC_REWIND, CC_FORWARD, CC_STOP, CC_PLAY, CC_REC}


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
        self._load_cc_mappings()

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

        # Routing mode (None = standalone virtual port, dict = proxy)
        self.routing = self.config.get("routing")

        # Transport / mode state
        self.intercept_mode = True   # True = intercept transport for preset mgmt
        self.rec_counter = 0         # Consecutive rec presses
        self.preset_cursor = None    # Current note for arrow navigation

        # MIDI ports (opened in run())
        self.midi_in = None
        self.midi_out = None
        self.midi_return = None  # Recall output (= midi_out in standalone mode)

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

    def _load_cc_mappings(self):
        """Load cc_mappings.yaml — shift/joystick definitions and CC routing."""
        path = self.config_dir / "cc_mappings.yaml"
        if path.exists():
            data = _yaml_load(path)
        else:
            data = {}
        self.shift_defs = data.get("shift_definitions", [])
        self.joystick_defs = data.get("joystick_definitions", [])
        self.cc_mappings = data.get("cc_mappings", {})

        # Build lookup sets for fast detection
        self.shift_ccs = {s["cc"] for s in self.shift_defs}
        self.joystick_ccs = set()
        for j in self.joystick_defs:
            self.joystick_ccs.add(j["negative_cc"])
            self.joystick_ccs.add(j["positive_cc"])

        # Runtime state for the mapping engine
        self.shift_state = 0            # 16-bit bitmask
        self.joystick_states = {}       # index -> {neg_held, pos_held, latch}
        self.destination_states = {}    # "cc_ch" -> current value
        self._map_log_times = {}        # "cc_ch" -> last log timestamp (debounce)

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
        elif cmd == CMD_DEBUG_TEXT:
            self._cmd_debug_text(data[3:])
        elif cmd == CMD_DEVICE_CMD:
            self._cmd_device_cmd(data[3:])

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

    def _cmd_debug_text(self, data_bytes):
        """Print arbitrary 7-bit-encoded text to the console."""
        if not data_bytes:
            return
        text = "".join(chr(b) for b in data_bytes)
        print(f"[  DBG ] {text}")

    def _cmd_device_cmd(self, data_bytes):
        """Handle a machine-readable command from the device (Scripter)."""
        if not data_bytes:
            return
        text = "".join(chr(b) for b in data_bytes)
        # Split into command and arguments
        parts = text.strip().split(None, 1)
        cmd = parts[0].lower() if parts else ""
        args = parts[1] if len(parts) > 1 else ""

        handler = self._device_commands.get(cmd)
        if handler:
            handler(self, args)
        else:
            _log("DEVCMD", f"Unknown device command: '{cmd}'")

    # --- Device command handlers (Scripter → service) ---

    def _devcmd_ping(self, args):
        """Respond with a pong so the device knows the service is alive."""
        self._send_remote("pong")

    def _devcmd_status(self, args):
        """Report service status back to the device."""
        n = len(self.presets)
        mode = "proxy" if self.routing else "standalone"
        rec = "recording" if self.recording else "idle"
        tmode = "intercept" if self.intercept_mode else "passthrough"
        self._send_remote(f"status {mode} {rec} presets={n} transport={tmode}")

    def _devcmd_list(self, args):
        """Send back a list of stored preset slots."""
        if not self.presets:
            self._send_remote("list empty")
            return
        slots = sorted(self.presets.keys())
        names = []
        for s in slots:
            name = self.presets[s].get("name", "")
            names.append(f"{s}:{name}")
        self._send_remote("list " + ",".join(names))

    def _devcmd_mode(self, args):
        """Toggle or set transport mode (intercept / passthrough)."""
        arg = args.strip().lower()
        if arg == "intercept":
            self.intercept_mode = True
        elif arg in ("passthrough", "pass"):
            self.intercept_mode = False
        else:
            # Toggle
            self.intercept_mode = not self.intercept_mode
        mode_name = "intercept" if self.intercept_mode else "passthrough"
        self.rec_counter = 0
        self.load_mode = False
        _log("MODE", f"Transport mode: {mode_name}")
        self._send_remote(f"mode {mode_name}")

    # Registry of device commands
    _device_commands = {
        "ping":   _devcmd_ping,
        "status": _devcmd_status,
        "list":   _devcmd_list,
        "mode":   _devcmd_mode,
    }

    def _send_remote(self, text):
        """Send a CMD_REMOTE_CMD SysEx back to the device via the return port."""
        encoded = [min(ord(c), 127) for c in text]
        sysex_data = [self.manufacturer_id, self.device_id, CMD_REMOTE_CMD] + encoded
        if self.midi_return:
            self.midi_return.send(mido.Message("sysex", data=sysex_data))
        _log("DEVCMD", f"-> {text}")

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
            self.midi_return.send(mido.Message("sysex", data=sysex_data))
            _log("  ->", f"Name: \"{name}\"")

    def _recall_single(self, preset, channel):
        """Recall a preset that has no destination grouping."""
        pc = preset.get("program_change")
        if pc is not None:
            self.midi_return.send(mido.Message("program_change", channel=channel, program=pc))
            _log("  ->", f"PC {pc}")
        for cc_name, value in preset.get("cc_values", {}).items():
            cc_num = self._cc_name_to_number(cc_name)
            if cc_num is None:
                _log("WARN", f"Cannot resolve CC '{cc_name}' — skipping.")
                continue
            self.midi_return.send(
                mido.Message("control_change", channel=channel, control=cc_num, value=value)
            )
            self._set_dest(cc_num, channel, value)  # Sync mapping engine state
            _log("  ->", f"CC {cc_name}({cc_num}) = {value}")

    def _recall_multi_dest(self, preset, channel):
        """Recall a preset with destination grouping, sending SysEx markers."""
        for dest_num, dest_data in preset["destinations"].items():
            dest_num = int(dest_num)  # YAML may store as string
            # Destination marker
            marker = [self.manufacturer_id, self.device_id, CMD_DEST_MARKER, dest_num]
            self.midi_return.send(mido.Message("sysex", data=marker))
            _log("  ->", f"Dest {self._dest_display(dest_num)}")

            pc = dest_data.get("program_change")
            if pc is not None:
                self.midi_return.send(
                    mido.Message("program_change", channel=channel, program=pc)
                )
                _log("  ->", f"  PC {pc}")

            for cc_name, value in dest_data.get("cc_values", {}).items():
                cc_num = self._cc_name_to_number(cc_name, dest_num)
                if cc_num is None:
                    _log("WARN", f"  Cannot resolve CC '{cc_name}' — skipping.")
                    continue
                self.midi_return.send(
                    mido.Message("control_change", channel=channel, control=cc_num, value=value)
                )
                self._set_dest(cc_num, channel, value)  # Sync mapping engine state
                _log("  ->", f"  CC {cc_name}({cc_num}) = {value}")

    # -- CC Mapping Engine ----------------------------------------------------

    def _decode_relative(self, value):
        """Decode 2's complement relative value: 1-63 = positive, 65-127 = negative."""
        if 1 <= value <= 63:
            return value
        if 65 <= value <= 127:
            return -(128 - value)
        return 0

    def _dest_key(self, cc, channel):
        return f"{cc}_{channel}"

    def _init_dest(self, cc, channel, default):
        key = self._dest_key(cc, channel)
        if key not in self.destination_states:
            self.destination_states[key] = default
        return key

    def _get_dest(self, cc, channel):
        return self.destination_states.get(self._dest_key(cc, channel), 0)

    def _set_dest(self, cc, channel, value, min_v=0, max_v=127):
        key = self._dest_key(cc, channel)
        value = max(min_v, min(max_v, value))
        self.destination_states[key] = value
        return value

    def _sync_all_destinations(self):
        """Re-send all current destination values (e.g. after MIDI reset)."""
        for key, value in self.destination_states.items():
            parts = key.split("_")
            cc_num, channel = int(parts[0]), int(parts[1])
            self._send_cc(cc_num, channel, value)

    def _send_cc(self, cc, channel, value):
        """Send a mapped CC to hardware output. Also captures during recording."""
        if self.recording:
            dd = self.dest_data.get(self.active_dest)
            if dd is not None:
                dd["ccs"][cc] = value
        if self.midi_out:
            self.midi_out.send(
                mido.Message("control_change",
                             channel=channel,
                             control=cc,
                             value=value)
            )

    def _process_shift(self, msg):
        """Update shift bitmask if msg is a shift CC. Returns True if handled."""
        # Channels are 1-based in config, 0-based in mido — shift CCs match
        # by CC number only (same as original Scripter behaviour).
        for sdef in self.shift_defs:
            if msg.control == sdef["cc"]:
                bit = sdef["bit"]
                old = self.shift_state
                if msg.value > sdef["threshold"]:
                    self.shift_state |= (1 << bit)
                else:
                    self.shift_state &= ~(1 << bit)
                if self.shift_state != old:
                    _log("SHIFT", f"CC{msg.control} bit{bit} → 0x{self.shift_state:04X}")
                return True
        return False

    def _process_joystick(self, msg):
        """Update joystick held/latch bits if msg is a joystick CC.
        Returns True if the CC belongs to a joystick definition."""
        for idx, jdef in enumerate(self.joystick_defs):
            is_negative = msg.control == jdef["negative_cc"]
            is_positive = msg.control == jdef["positive_cc"]
            if not (is_negative or is_positive):
                continue

            # Initialise per-joystick state
            if idx not in self.joystick_states:
                self.joystick_states[idx] = {
                    "neg_held": False, "pos_held": False, "latch": False
                }
            st = self.joystick_states[idx]

            was_held = st["neg_held"] if is_negative else st["pos_held"]
            is_held = msg.value > jdef["threshold"]

            if is_negative:
                st["neg_held"] = is_held
            else:
                st["pos_held"] = is_held

            # Update "held" bits
            old = self.shift_state
            if is_negative and "held_negative_bit" in jdef:
                b = jdef["held_negative_bit"]
                if is_held:
                    self.shift_state |= (1 << b)
                else:
                    self.shift_state &= ~(1 << b)
            if is_positive and "held_positive_bit" in jdef:
                b = jdef["held_positive_bit"]
                if is_held:
                    self.shift_state |= (1 << b)
                else:
                    self.shift_state &= ~(1 << b)

            # Latch on transition: not-held → held
            if "latch_bit" in jdef and not was_held and is_held:
                if is_negative:
                    new_latch = jdef["latch_negative_on"]
                else:
                    new_latch = not jdef["latch_negative_on"]
                st["latch"] = new_latch
                lb = jdef["latch_bit"]
                if new_latch:
                    self.shift_state |= (1 << lb)
                else:
                    self.shift_state &= ~(1 << lb)

            if self.shift_state != old:
                _log("JOY", f"CC{msg.control} → 0x{self.shift_state:04X}")
            return True
        return False

    def _map_log_debounced(self, key, line1):
        """Log mapping debug output, debounced to once per second per key."""
        now = time.monotonic()
        last = self._map_log_times.get(key, 0)
        if now - last < 1.0:
            return
        self._map_log_times[key] = now
        _log("MAP", line1)

    def _process_cc_mapping(self, msg):
        """Run the CC through the mapping table. All matching actions execute."""
        source_cc = msg.control
        if source_cc not in self.cc_mappings:
            return  # Unmapped CC — silently dropped

        actions = self.cc_mappings[source_cc]
        # Source channel: mido is 0-based, YAML is 1-based
        src_ch_1based = msg.channel + 1

        for action in actions:
            # Channel filter
            if "from_channel" in action:
                if action["from_channel"] != src_ch_1based:
                    continue

            # Shift state match
            masked = self.shift_state & action["mask"]
            if masked != action["compare"]:
                continue

            # Determine target CC and channel
            target_cc = action["cc"]
            # Channel: YAML is 1-based, convert to 0-based for mido
            if "channel" in action:
                target_ch = action["channel"] - 1
            else:
                target_ch = msg.channel  # Keep source channel (already 0-based)

            action_type = action.get("type", "absolute")
            default_val = action.get("default", 64 if action_type == "relative" else 0)
            min_v = action.get("min", 0)
            max_v = action.get("max", 127)

            self._init_dest(target_cc, target_ch, default_val)

            if action_type == "relative":
                delta = self._decode_relative(msg.value)
                current = self._get_dest(target_cc, target_ch)
                output = self._set_dest(target_cc, target_ch, current + delta, min_v, max_v)
            else:
                output = self._set_dest(target_cc, target_ch, msg.value, min_v, max_v)

            self._send_cc(target_cc, target_ch, output)

            # Debug: show mapping result (debounced)
            name = action.get("name", "")
            dest_key = self._dest_key(target_cc, target_ch)
            line = (f"CC{source_cc} ch{src_ch_1based} "
                    f"(shift=0x{self.shift_state:04X}) → "
                    f"CC{target_cc} ch{target_ch + 1} = {output} "
                    f"\"{name}\"")
            self._map_log_debounced(dest_key, line)

    # -- Transport handling ---------------------------------------------------

    def _handle_transport(self, cc):
        """Route a transport CC press to the active mode handler."""
        if self.intercept_mode:
            self._transport_intercept(cc)
        else:
            self._transport_passthrough(cc)

    def _transport_intercept(self, cc):
        """Handle transport in intercept mode (CCs consumed for preset mgmt)."""
        if cc == CC_REC:
            if self.load_mode:
                self.load_mode = False
                _log("LOAD", "Load mode cancelled (rec pressed).")
            self.rec_counter += 1
            _log("TRANS", f"Rec (counter={self.rec_counter})")

        elif cc == CC_PLAY:
            if self.rec_counter == 0:
                # Direct load — next note-on selects the preset to recall
                self.load_mode = True
                self.recording = False
                _log("LOAD", "Direct load — send a note-on to select the preset.")
            else:
                _log("TRANS", f"Play ignored (counter={self.rec_counter})")

        elif cc == CC_STOP:
            if self.load_mode:
                self.load_mode = False
                _log("LOAD", "Load mode cancelled.")
            counter = self.rec_counter
            self.rec_counter = 0
            if counter == 0:
                return
            # Dispatch valid counter values (to be defined)
            # For now all nonzero counters clear without action
            _log("TRANS", f"Stop with counter={counter} — cleared")

        elif cc == CC_REWIND:
            self.load_mode = False
            self._navigate_preset(-1)

        elif cc == CC_FORWARD:
            self.load_mode = False
            self._navigate_preset(1)

    def _transport_passthrough(self, cc):
        """Handle transport in pass-through mode (CCs forwarded to hardware)."""
        if cc == CC_REC:
            self.rec_counter += 1
            _log("TRANS", f"Rec (counter={self.rec_counter})")

        elif cc == CC_STOP:
            counter = self.rec_counter
            self.rec_counter = 0
            if counter == 0:
                return
            if counter == 2:
                # Valid action placeholder for pass-through counter=2
                _log("TRANS", f"Stop with counter=2 — action TBD")
                return
            # counter=1 or >2: no action, just clear
            _log("TRANS", f"Stop with counter={counter} — cleared")

    def _navigate_preset(self, direction):
        """Move the preset cursor by *direction* (+1/-1) and recall."""
        slots = sorted(self.presets.keys())
        if not slots:
            _log("WARN", "No presets stored — nothing to navigate.")
            return

        if self.preset_cursor is not None and self.preset_cursor in slots:
            idx = slots.index(self.preset_cursor) + direction
        else:
            # First navigation: start at beginning or end
            idx = 0 if direction > 0 else len(slots) - 1

        idx = idx % len(slots)
        self.preset_cursor = slots[idx]
        _log("NAV", f"Preset {self.preset_cursor} (slot {idx + 1}/{len(slots)})")
        self._recall_preset(self.preset_cursor)

    # -- Main message handler -------------------------------------------------

    def _handle_message(self, msg):
        if msg.type == "sysex":
            if self._is_ours(msg.data):
                self._handle_sysex(msg.data)
                return  # Intercepted — do not forward
            self._forward(msg)
            return

        # Transport CCs (rec, play, stop, arrows)
        if msg.type == "control_change" and msg.control in TRANSPORT_CCS:
            if msg.value > 0:  # Ignore release pulse
                self._handle_transport(msg.control)
            if self.intercept_mode:
                return  # Consumed — do not forward
            self._forward(msg)
            return

        # Note-on (velocity > 0)
        if msg.type == "note_on" and msg.velocity > 0:
            if self.load_mode:
                self.load_mode = False
                self.preset_cursor = msg.note  # Track for arrow navigation
                self._recall_preset(msg.note)
                return  # Intercepted
            self.last_note = (msg.channel, msg.note)
            if self.recording:
                _log("NOTE", f"ch={msg.channel} note={msg.note} (preset tag)")
                return  # Tag note — do not forward
            self._forward(msg)
            return

        # Control Change — process through mapping engine
        # (matches original Scripter processing order)
        if msg.type == "control_change":
            # 1. Shift CCs update bitmask
            self._process_shift(msg)
            # 2. MIDI reset → re-send all destination values
            if msg.control in (121, 123):
                self._sync_all_destinations()
            # 3. Joystick CCs update held/latch bits
            self._process_joystick(msg)
            # 4. Run through CC mapping table (all matching actions fire)
            #    Mapped outputs sent via _send_cc (also captured during recording)
            self._process_cc_mapping(msg)
            return  # CCs never forwarded raw — only mapped outputs are sent

        # Program Change — capture during recording, forward to hardware
        if msg.type == "program_change":
            if self.recording:
                dd = self.dest_data.get(self.active_dest)
                if dd is not None:
                    dd["pc"] = msg.program
                    _log("PC", f"program={msg.program}")
            self._forward(msg)
            return

        # All other events (notes, pitch bend, etc.) — forward unchanged
        self._forward(msg)

    def _forward(self, msg):
        """Forward a message to the hardware output (proxy mode only)."""
        if self.routing and self.midi_out:
            self.midi_out.send(msg)

    # -- Run loop -------------------------------------------------------------

    def run(self):
        port_name = self.config.get("port_name", "MIDI SX Presets")
        dest_count = len(self.destinations_map)
        cc_count = sum(len(s) for s in self.cc_name_sets.values())

        _log("INIT", "MIDI SysEx Preset Manager")
        _log("INIT", f"Config dir   : {self.config_dir}")
        if self.routing:
            _log("INIT", f"Mode         : proxy")
            _log("INIT", f"IAC input    : {self.routing['iac_input']}")
            _log("INIT", f"IAC return   : {self.routing['iac_return']}")
            _log("INIT", f"Hardware out : {self.routing['hardware_output']}")
        else:
            _log("INIT", f"Mode         : standalone")
            _log("INIT", f"Virtual port : {port_name}")
        _log("INIT", f"Device ID    : 0x{self.device_id:02X}")
        _log("INIT", f"Manufacturer : 0x{self.manufacturer_id:02X}")
        _log("INIT", f"Presets      : {len(self.presets)} loaded")
        _log("INIT", f"CC names     : {cc_count} mappings in {len(self.cc_name_sets)} set(s)")
        action_count = sum(len(v) for v in self.cc_mappings.values())
        _log("INIT", f"CC mappings  : {len(self.cc_mappings)} source CCs, {action_count} actions")
        _log("INIT", f"Shift CCs    : {sorted(self.shift_ccs)} | Joystick CCs: {sorted(self.joystick_ccs)}")
        tmode = "intercept" if self.intercept_mode else "passthrough"
        _log("INIT", f"Transport    : {tmode}")
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
        print(f"  Debug text    : F0 {mid:02X} {dev:02X} {CMD_DEBUG_TEXT:02X} <ascii…> F7")
        print(f"  Remote cmd    : F0 {mid:02X} {dev:02X} {CMD_REMOTE_CMD:02X} <ascii…> F7")
        print(f"  Device cmd    : F0 {mid:02X} {dev:02X} {CMD_DEVICE_CMD:02X} <ascii…> F7")
        print()
        print("Transport (intercept mode):")
        print("  Play            : Direct load — next note-on recalls that preset")
        print("  Rewind/Forward  : Navigate presets sequentially (with full recall)")
        print("  Rec             : Increment counter")
        print("  Stop            : Execute/clear counter")
        print()
        print("Device commands: ping, status, list, mode [intercept|passthrough]")
        print()
        print("Listening… (Ctrl-C to quit)")
        print()

        try:
            if self.routing:
                self.midi_in = mido.open_input(self.routing["iac_input"])
                self.midi_out = mido.open_output(self.routing["hardware_output"])
                self.midi_return = mido.open_output(self.routing["iac_return"])
            else:
                self.midi_in = mido.open_input(port_name, virtual=True)
                self.midi_out = mido.open_output(port_name, virtual=True)
                self.midi_return = self.midi_out

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
            if self.midi_return and self.midi_return is not self.midi_out:
                self.midi_return.close()


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
