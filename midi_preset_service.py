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
import threading
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
CC_SETS_FILENAME = "cc_sets.yaml"
CONFIG_FILENAME = "config.yaml"
DESTINATIONS_FILENAME = "destinations.yaml"
STATE_FILENAME = "state.yaml"
STATE_SAVE_INTERVAL = 5  # seconds between periodic state dumps

# SysEx protocol
SYSEX_MANUFACTURER_ID = 0x7D  # Non-commercial / educational use
DEFAULT_DEVICE_ID = 0x01

# Commands
CMD_LOAD_PRESET = 0x03
CMD_PRESET_NAME = 0x04   # Sent back to the device on recall
CMD_DEBUG_TEXT  = 0x07   # Arbitrary text → console (supports ANSI escapes)
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
        (self.cc_sets,
         self.cc_default_set,
         self.cc_set_defaults) = self._load_cc_sets()
        self.destinations_map = self._load_destinations()
        (self.resolved_destinations,
         self.reverse_destinations,
         self.auto_destinations,
         self.dest_defaults) = self._resolve_destinations()
        # Flat CC-number → default, merged from all sets (fallback when
        # destinations aren't configured).
        self.cc_defaults = {}
        for defs in self.cc_set_defaults.values():
            self.cc_defaults.update(defs)
        self.recall_ignore = self._build_recall_ignore()
        self.presets = self._load_presets()
        self._load_cc_mappings()

        # Build reverse lookups (name -> number) for each set
        self.cc_reverse_sets = {
            name: {v: k for k, v in mapping.items()}
            for name, mapping in self.cc_sets.items()
        }
        self.cc_reverse_default = {v: k for k, v in self.cc_default_set.items()}

        # Protocol IDs (configurable via config.yaml)
        self.manufacturer_id = self.config.get("manufacturer_id", SYSEX_MANUFACTURER_ID)
        self.device_id = self.config.get("device_id", DEFAULT_DEVICE_ID)

        # Runtime state
        self.load_mode = False

        # Routing mode (None = standalone virtual port, dict = proxy)
        self.routing = self.config.get("routing")

        # Transport / mode state
        self.intercept_mode = True   # True = intercept transport for preset mgmt
        self.rec_counter = 0         # Consecutive rec presses
        self.preset_cursor = {}      # {channel: note} for arrow navigation
        self._last_preset = None     # (channel, note) of most recent recall/save

        # Persistent state recovery
        self._state_dirty = False
        self._state_stop = threading.Event()
        self._load_state()

        # MIDI ports (opened in run())
        self.midi_in = None
        self.midi_out = None
        self.midi_return = None  # Recall output (= midi_out in standalone mode)

    # -- Config / persistence -------------------------------------------------

    def _state_path(self):
        return self.config_dir / STATE_FILENAME

    def _load_state(self):
        """Restore runtime state from the persistent state file.

        Populates ``destination_states``, ``preset_cursor``,
        ``intercept_mode``, ``shift_state``, and ``load_mode``
        from the last saved snapshot so a restart feels seamless.
        """
        path = self._state_path()
        if not path.exists():
            return
        data = _yaml_load(path)
        if not isinstance(data, dict):
            return
        # Destination values — the core mixer state
        saved_dests = data.get("destination_states")
        if isinstance(saved_dests, dict):
            self.destination_states.update(
                {k: int(v) for k, v in saved_dests.items()
                 if isinstance(v, (int, float))}
            )
        # Preset cursor — now a {channel: note} dict (migrate from legacy scalar)
        saved_cursor = data.get("preset_cursor")
        if isinstance(saved_cursor, dict):
            self.preset_cursor = {int(k): v for k, v in saved_cursor.items()}
        elif saved_cursor is not None:
            # Legacy: single note — assign to channel 0
            self.preset_cursor = {0: int(saved_cursor)}
        if "intercept_mode" in data:
            self.intercept_mode = bool(data["intercept_mode"])
        if "shift_state" in data:
            self.shift_state = int(data["shift_state"])
        saved_lp = data.get("last_preset")
        if isinstance(saved_lp, (list, tuple)) and len(saved_lp) == 2:
            self._last_preset = (int(saved_lp[0]), int(saved_lp[1]))
        n = len(self.destination_states)
        cursor_str = ", ".join(f"ch{ch + 1}={note}"
                               for ch, note in sorted(self.preset_cursor.items()))
        _log("INIT", f"Restored state: {n} destinations, "
             f"cursors=[{cursor_str}]")

    def _save_state(self):
        """Dump current runtime state to disk (atomic write)."""
        data = {
            "destination_states": dict(self.destination_states),
            "preset_cursor": self.preset_cursor,
            "intercept_mode": self.intercept_mode,
            "shift_state": self.shift_state,
            "last_preset": list(self._last_preset) if self._last_preset else None,
        }
        path = self._state_path()
        tmp = path.with_suffix(".tmp")
        _yaml_dump(data, tmp)
        tmp.replace(path)
        self._state_dirty = False

    def _mark_dirty(self):
        """Flag that state has changed and needs persisting."""
        self._state_dirty = True

    def _state_saver_loop(self):
        """Background thread: flush dirty state periodically."""
        interval = self.config.get("state_save_interval", STATE_SAVE_INTERVAL)
        while not self._state_stop.is_set():
            self._state_stop.wait(interval)
            if self._state_dirty:
                try:
                    self._save_state()
                except Exception as exc:
                    _log("WARN", f"State save failed: {exc}")

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

    @staticmethod
    def _normalize_cc_set(raw_set):
        """Normalise a CC name set, returning (names_dict, defaults_dict).

        Entries may be plain strings or dicts with ``name`` and optional
        ``default``::

            3: to_duck_out               # plain string, no default
            7: {name: to_perf_filter, default: 127}   # dict with default
        """
        names = {}
        defaults = {}
        for cc_num, entry in (raw_set or {}).items():
            cc_num = int(cc_num)
            if isinstance(entry, dict):
                names[cc_num] = str(entry["name"])
                if "default" in entry:
                    defaults[cc_num] = int(entry["default"])
            else:
                names[cc_num] = str(entry)
        return names, defaults

    def _load_cc_sets(self):
        """Load CC name mappings.

        Returns ``(name_sets, default_names, cc_set_defaults)``.

        *name_sets* and *default_names* are string-only dicts as before.
        *cc_set_defaults* maps ``set_name → {cc_num: default_value}``
        for entries that specified a ``default``.

        Supports two YAML formats:

          Flat::

              cc_sets:
                1: modulation
                7: {name: volume, default: 100}

          Named sets::

              cc_sets:
                mix_ccs:
                  3: to_duck_out
                  7: {name: to_perf_filter, default: 127}
        """
        path = self.config_dir / CC_SETS_FILENAME
        if path.exists():
            data = _yaml_load(path)
            raw = data.get("cc_sets")
            if isinstance(raw, dict) and raw:
                # Detect named sets vs flat: if any value is itself a dict
                # whose keys are integers, it's a named-sets structure.
                first_val = next(iter(raw.values()))
                is_named = isinstance(first_val, dict)
                if is_named:
                    name_sets = {}
                    cc_set_defaults = {}
                    for set_name, raw_set in raw.items():
                        names, defs = self._normalize_cc_set(raw_set)
                        name_sets[set_name] = names
                        if defs:
                            cc_set_defaults[set_name] = defs
                    return name_sets, name_sets.get("default", {}), cc_set_defaults
                else:
                    # Flat format — single set
                    names, defs = self._normalize_cc_set(raw)
                    cc_set_defaults = {"default": defs} if defs else {}
                    return {"default": names}, names, cc_set_defaults
        # Create default file (flat format)
        default_names = {
            1: "modulation",
            7: "volume",
            10: "pan",
            11: "expression",
            64: "sustain",
            74: "filter_cutoff",
        }
        _yaml_dump({"cc_sets": default_names}, path)
        return {"default": default_names}, default_names, {}

    _DEST_RESERVED_KEYS   = {"prefix", "channels"}
    _CH_RESERVED_KEYS     = {"cc_group", "prefix"}
    _PRESET_RESERVED_KEYS = {"name", "read_only", "channels",
                             "program_change", "cc_values"}

    def _load_destinations(self):
        """Load the optional destinations map (destinations.yaml).

        Each top-level key is a destination identifier.  Reserved keys
        inside a destination are ``prefix`` and ``channels``.
        ``channels`` is a dict keyed by 1-based MIDI channel number;
        inside each channel ``cc_group`` is reserved and any bare integer
        key is a CC override::

            nerdseq:
              prefix: ns
              channels:
                1:
                  cc_group: mix_ccs
                  85: nerdseq_perf_out
                2:
                  cc_group: polysynth_ccs
        """
        path = self.config_dir / DESTINATIONS_FILENAME
        if path.exists():
            data = _yaml_load(path)
            if isinstance(data, dict):
                return {k: v for k, v in data.items()
                        if isinstance(v, dict)}
        return {}

    def _resolve_destinations(self):
        """Resolve destination channel CC names from cc_sets.

        For each destination channel:
          1. Inherit from the channel's cc_group (names prefixed).
          2. Apply bare integer-keyed overrides (not prefixed).
          3. Check for name conflicts across all destinations.
          4. Collect per-(channel, cc) defaults from cc_set_defaults.

        Returns a tuple of four flat dicts:
          forward:       ``{(channel, cc_num): name}``
          reverse:       ``{name: (channel, cc_num)}``
          auto:          ``{unprefixed_name: [(channel, cc_num), ...]}``
          dest_defaults: ``{(channel, cc_num): default_value}``
        Logs warnings for every conflict found.
        """
        forward  = {}   # (ch, cc_num) -> name
        reverse  = {}   # name -> (ch, cc_num)
        auto     = {}   # unprefixed_name -> [(ch, cc_num), ...]
        defaults = {}   # (ch, cc_num) -> default value
        for dest_id, dest_cfg in self.destinations_map.items():
            prefix = dest_cfg.get("prefix", "")
            channels_cfg = dest_cfg.get("channels", {})

            for ch, ch_cfg in channels_cfg.items():
                ch = int(ch) - 1          # YAML is 1-based, internal is 0-based
                if not isinstance(ch_cfg, dict):
                    ch_cfg = {}

                # 1) Inherit from cc_group, prefixed (channel prefix overrides dest)
                ch_prefix = ch_cfg.get("prefix", prefix)
                group_name = ch_cfg.get("cc_group")
                ch_names = {}
                group_defaults = (self.cc_set_defaults.get(group_name, {})
                                  if group_name else {})
                if group_name and group_name in self.cc_sets:
                    for cc_num, raw_name in self.cc_sets[group_name].items():
                        cc_num = int(cc_num)
                        ch_names[cc_num] = f"{ch_prefix}_{raw_name}" if ch_prefix else raw_name
                        # auto map: unprefixed name → all (ch, cc) targets
                        auto.setdefault(raw_name, []).append((ch, cc_num))
                        # Propagate default from cc_set
                        if cc_num in group_defaults:
                            defaults[(ch, cc_num)] = group_defaults[cc_num]

                # 2) Bare integer keys are CC overrides (not prefixed)
                for key, value in ch_cfg.items():
                    if key not in self._CH_RESERVED_KEYS:
                        try:
                            cc_num = int(key)
                            ch_names[cc_num] = str(value)
                        except (ValueError, TypeError):
                            pass

                # 3) Merge into flat maps with conflict detection
                for cc_num, name in ch_names.items():
                    fwd_prev = forward.get((ch, cc_num))
                    if fwd_prev is not None and fwd_prev != name:
                        _log("WARN",
                             f"Destination '{dest_id}': ch{ch}/CC{cc_num} "
                             f"already mapped to '{fwd_prev}', "
                             f"overwriting with '{name}'")
                    forward[(ch, cc_num)] = name

                    rev_prev = reverse.get(name)
                    if rev_prev is not None and rev_prev != (ch, cc_num):
                        _log("WARN",
                             f"Destination '{dest_id}': name conflict — "
                             f"'{name}' maps to both ch{rev_prev[0]}/CC{rev_prev[1]} "
                             f"and ch{ch}/CC{cc_num}")
                    reverse[name] = (ch, cc_num)

        if defaults:
            _log("INIT", f"CC defaults from cc_sets: {len(defaults)} entries")
        return forward, reverse, auto, defaults

    def _build_recall_ignore(self):
        """Build a set of (channel, cc_num) pairs to skip during preset recall.

        Configured in config.yaml as ``recall_ignore``, a list of entries
        that are either a parameter name (string) or a dict with
        ``channel`` (1-based) and ``cc`` keys::

            recall_ignore:
              - ns_to_perf_filter
              - {channel: 1, cc: 7}
        """
        raw = self.config.get("recall_ignore", [])
        ignore = set()
        for entry in raw:
            if isinstance(entry, str):
                target = self.reverse_destinations.get(entry)
                if target is None:
                    _log("WARN", f"recall_ignore: '{entry}' not found "
                         "in destinations — skipping")
                    continue
                ignore.add(target)
            elif isinstance(entry, dict) and "channel" in entry and "cc" in entry:
                ignore.add((int(entry["channel"]) - 1, int(entry["cc"])))
            else:
                _log("WARN", f"recall_ignore: unrecognized entry {entry!r}")
        if ignore:
            _log("INIT", f"Recall ignore: {len(ignore)} parameter(s)")
        return ignore

    def _load_cc_mappings(self):
        """Load cc_mappings.yaml — shift/joystick definitions and CC routing.

        Actions may use ``parameter: name`` (resolved via
        reverse_destinations) instead of explicit ``cc:`` and
        ``channel:``.  Resolution happens here at load time so the
        runtime mapping path stays branchless.
        """
        path = self.config_dir / "cc_mappings.yaml"
        if path.exists():
            data = _yaml_load(path)
        else:
            data = {}
        self.shift_defs = data.get("shift_definitions", [])
        self.joystick_defs = data.get("joystick_definitions", [])
        self.cc_mappings = data.get("cc_mappings", {})

        # Resolve "parameter" / "auto" shorthands → cc + channel (1-based)
        # "parameter" resolves to one (ch, cc) via prefixed name.
        # "auto" expands to one action per channel that has the unprefixed name.
        for source_cc, actions in list(self.cc_mappings.items()):
            expanded = []
            for action in actions:
                if "parameter" in action:
                    param = action.pop("parameter")
                    target = self.reverse_destinations.get(param)
                    if target is None:
                        _log("WARN", f"CC mapping source {source_cc}: "
                             f"parameter '{param}' not found in destinations")
                        expanded.append(action)
                        continue
                    ch, cc_num = target
                    action["cc"] = cc_num
                    action["channel"] = ch + 1
                    expanded.append(action)
                elif "auto" in action:
                    raw = action.pop("auto")
                    targets = self.auto_destinations.get(raw)
                    if not targets:
                        _log("WARN", f"CC mapping source {source_cc}: "
                             f"auto '{raw}' not found in destinations")
                        expanded.append(action)
                        continue
                    for ch, cc_num in targets:
                        copy = dict(action)
                        copy["cc"] = cc_num
                        copy["channel"] = ch + 1
                        copy["name"] = self.resolved_destinations.get(
                            (ch, cc_num), raw)
                        expanded.append(copy)
                else:
                    expanded.append(action)
            self.cc_mappings[source_cc] = expanded

        # Build lookup sets for fast detection
        self.shift_ccs = {s["cc"] for s in self.shift_defs}
        self.joystick_ccs = set()
        for j in self.joystick_defs:
            self.joystick_ccs.add(j["negative_cc"])
            self.joystick_ccs.add(j["positive_cc"])

        # Note range mappings — list of {low, high, mask, compare, ...}
        self.note_mappings = data.get("note_mappings", [])
        for nm in self.note_mappings:
            nm.setdefault("mask", 0)
            nm.setdefault("compare", 0)
            # monophonic shorthand → max_polyphony: 1 + optional instance
            mono = nm.pop("monophonic", None)
            if mono is not None and "max_polyphony" not in nm:
                nm["max_polyphony"] = 1
                if isinstance(mono, str) and "polyphony_instance" not in nm:
                    nm["polyphony_instance"] = mono

        # Resolve named polyphony instances
        self.poly_instances = self._resolve_poly_instances()

        # Runtime state for the mapping engine
        self.shift_state = 0            # 16-bit bitmask
        self.joystick_states = {}       # index -> {neg_held, pos_held, latch}
        self.destination_states = {}    # "cc_ch" -> current value
        self._map_log_times = {}        # "cc_ch" -> last log timestamp (debounce)
        self.poly_states = {}           # pool_key -> {held: [...], active: [...]}
        self._center_trackers = {}      # dest_key -> center-snap wiggle state
        self._center_timers = {}        # dest_key -> threading.Timer for idle detect

    _POLY_PARAM_KEYS = ("max_polyphony", "fallback_priority", "replace_priority")

    def _resolve_poly_instances(self):
        """Validate and collect canonical parameters for named polyphony instances.

        Returns a dict mapping instance_name → {max_polyphony, fallback_priority,
        replace_priority}.  Ranges that define an instance (have max_polyphony)
        register it; ranges that merely join (polyphony_instance without
        max_polyphony) are validated.  Conflicting re-definitions warn and
        keep the first definition.
        """
        defs = {}  # instance_name -> canonical params
        for nm in self.note_mappings:
            inst = nm.get("polyphony_instance")
            if inst is None:
                continue
            has_params = "max_polyphony" in nm
            if not has_params:
                continue
            params = {
                "max_polyphony": nm["max_polyphony"],
                "fallback_priority": nm.get("fallback_priority", "most_recent"),
                "replace_priority": nm.get("replace_priority", "lowest"),
            }
            if inst not in defs:
                defs[inst] = params
            else:
                # Check for conflicts with existing definition
                existing = defs[inst]
                for key in self._POLY_PARAM_KEYS:
                    if params[key] != existing[key]:
                        _log("WARN",
                             f"polyphony_instance '{inst}': "
                             f"conflicting {key} "
                             f"({params[key]} vs {existing[key]}), "
                             f"using first definition")

        # Warn about join-only references to undefined instances
        for nm in self.note_mappings:
            inst = nm.get("polyphony_instance")
            if inst is not None and inst not in defs:
                _log("WARN",
                     f"polyphony_instance '{inst}' referenced but "
                     f"never defined (no range sets max_polyphony for it)")

        return defs

    def _presets_dir(self):
        return self.config_dir / PRESETS_SUBDIR

    @staticmethod
    def _preset_filename(channel, note):
        """Channel is 0-based internally; filenames use 1-based (ch1, ch2…)."""
        return f"preset-ch{channel + 1}-{note:03d}.yaml"

    def _load_presets(self):
        """Load per-channel preset banks from disk.

        Returns ``{channel: {note: data}}`` (channel is 0-based).
        Handles both new ``preset-ch{N}-{note}.yaml`` and legacy
        ``preset-{note}.yaml`` files (legacy loaded into channel 0).
        """
        pdir = self._presets_dir()
        if not pdir.is_dir():
            pdir.mkdir(parents=True, exist_ok=True)
            return {}
        presets = {}  # channel -> {note -> data}
        for path in sorted(pdir.glob("preset-*.yaml")):
            stem = path.stem  # e.g. "preset-ch1-048" or legacy "preset-048"
            parts = stem.split("-")
            # parts: ["preset", "ch1", "048"] or legacy ["preset", "048"]
            try:
                if len(parts) == 3 and parts[1].startswith("ch"):
                    ch = int(parts[1][2:]) - 1   # 1-based file → 0-based
                    note = int(parts[2])
                elif len(parts) == 2:
                    # Legacy format — assign to channel 0
                    ch = 0
                    note = int(parts[1])
                else:
                    continue
            except (ValueError, IndexError):
                continue
            data = _yaml_load(path)
            if data:
                data.pop("channel", None)  # strip legacy field
                presets.setdefault(ch, {})[note] = data
        return presets

    def _save_preset_to_disk(self, channel, note):
        pdir = self._presets_dir()
        pdir.mkdir(parents=True, exist_ok=True)
        _yaml_dump(self.presets[channel][note],
                   pdir / self._preset_filename(channel, note))

    # -- CC name <-> number ---------------------------------------------------

    def _cc_set_for_dest(self, dest):
        """Return the CC-number-to-name dict for a destination."""
        if dest is not None and dest in self.destinations_map:
            set_name = self.destinations_map[dest].get("cc_sets", "default")
            if set_name in self.cc_sets:
                return self.cc_sets[set_name]
        return self.cc_default_set

    def _cc_num_to_name(self, cc_num, dest=None):
        return self._cc_set_for_dest(dest).get(cc_num, cc_num)

    def _cc_name_to_number(self, name, dest=None):
        """Resolve a CC name (or raw int) back to a CC number."""
        if isinstance(name, int):
            return name
        # Try the destination-specific set first
        if dest is not None and dest in self.destinations_map:
            set_name = self.destinations_map[dest].get("cc_sets", "default")
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

    # -- Command handlers -----------------------------------------------------

    def _handle_sysex(self, data):
        if not self._is_ours(data):
            return
        cmd = data[2]
        if cmd == CMD_LOAD_PRESET:
            self._cmd_load_preset()
        elif cmd == CMD_DEBUG_TEXT:
            self._cmd_debug_text(data[3:])
        elif cmd == CMD_DEVICE_CMD:
            self._cmd_device_cmd(data[3:])

    def _save_preset_from_state(self, note, channel):
        """Save the current mapping engine destination states as a preset.

        *channel* (0-based) selects the preset bank.  CCs whose
        (channel, cc) pair has a unique name in resolved_destinations
        are stored as top-level keys.  Everything else falls back to
        the channels/cc_values dict.
        """
        # Check write-protection
        bank = self.presets.get(channel, {})
        existing = bank.get(note, {})
        if existing.get("read_only", False):
            _log("DENY", f"Preset ch{channel + 1}/{note} is read-only — save rejected.")
            return

        # Start from existing preset, preserving any unrecognized keys
        preset = dict(existing)
        preset["name"] = existing.get("name", f"Preset {note}")
        preset["read_only"] = existing.get("read_only", False)
        preset.pop("channel", None)  # strip legacy field
        # Clear stale CC data — we'll rebuild from current state
        preset.pop("channels", None)
        preset.pop("cc_values", None)
        for key in list(preset):
            if key not in self._PRESET_RESERVED_KEYS and key in self.reverse_destinations:
                del preset[key]

        # Partition destination_states into known (flat) and unknown (channeled)
        unknown_channels = {}
        named_count = 0
        for key, value in self.destination_states.items():
            parts = key.split("_")
            cc_num, ch = int(parts[0]), int(parts[1])
            resolved_name = self.resolved_destinations.get((ch, cc_num))
            if resolved_name is not None:
                preset[resolved_name] = value
                named_count += 1
            else:
                name = self._cc_num_to_name(cc_num)
                ch_data = unknown_channels.setdefault(ch, {})
                ch_data[name] = value

        if unknown_channels:
            preset["channels"] = {ch: {"cc_values": ccs}
                                  for ch, ccs in unknown_channels.items()}

        self.presets.setdefault(channel, {})[note] = preset
        self._save_preset_to_disk(channel, note)
        self._last_preset = (channel, note)
        unknown_count = sum(len(ccs) for ccs in unknown_channels.values())
        total = named_count + unknown_count
        _log("SAVE", f"Preset ch{channel + 1}/{note} saved ({total} CCs: "
             f"{named_count} named, {unknown_count} in channels).")

    def _set_write_protect(self, locked):
        """Enable or disable write-protection on the most recently used preset."""
        if self._last_preset is None:
            _log("WARN", "No last preset to protect.")
            return
        ch, note = self._last_preset
        bank = self.presets.get(ch, {})
        preset = bank.get(note)
        if preset is None:
            _log("WARN", f"Preset ch{ch + 1}/{note} not found.")
            return
        preset["read_only"] = locked
        self._save_preset_to_disk(ch, note)
        state = "LOCKED" if locked else "UNLOCKED"
        name = preset.get("name", f"ch{ch + 1}/{note}")
        _log("PROTECT", f"{name} — {state}")

    def _cmd_load_preset(self):
        self.load_mode = True
        _log("LOAD", "Load mode — send a note-on to select the preset to recall.")

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
        n = sum(len(bank) for bank in self.presets.values())
        mode = "proxy" if self.routing else "standalone"
        tmode = "intercept" if self.intercept_mode else "passthrough"
        self._send_remote(f"status {mode} presets={n} transport={tmode}")

    def _devcmd_list(self, args):
        """Send back a list of stored preset slots (across all channel banks)."""
        if not self.presets:
            self._send_remote("list empty")
            return
        names = []
        for ch in sorted(self.presets):
            bank = self.presets[ch]
            for s in sorted(bank):
                name = bank[s].get("name", "")
                names.append(f"ch{ch + 1}/{s}:{name}")
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
        self._mark_dirty()
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

    def _recall_preset(self, channel, note):
        """Send all stored CC/PC values for *note* in *channel*'s bank."""
        bank = self.presets.get(channel, {})
        preset = bank.get(note)
        if preset is None:
            _log("WARN", f"No preset stored for ch{channel + 1}/{note}.")
            return

        self._last_preset = (channel, note)

        name = preset.get("name", "")
        _log("RECALL", f"Preset ch{channel + 1}/{note}: \"{name}\"")

        # 1) Top-level named parameters (resolved via reverse_destinations)
        self._recall_named(preset)

        # 2) Channeled CC values (fallback for unknowns / legacy)
        if "channels" in preset:
            self._recall_channeled(preset)
        elif "cc_values" in preset:
            # Legacy flat format — use channel 0 as fallback
            self._recall_single(preset, 0)

        # Send preset name back via SysEx
        if name:
            name_bytes = self._encode_name(name)
            sysex_data = [self.manufacturer_id, self.device_id, CMD_PRESET_NAME] + name_bytes
            self.midi_return.send(mido.Message("sysex", data=sysex_data))
            _log("  ->", f"Name: \"{name}\"")

    def _recall_named(self, preset):
        """Recall top-level named parameters via reverse_destinations."""
        for key, value in preset.items():
            if key in self._PRESET_RESERVED_KEYS:
                continue
            target = self.reverse_destinations.get(key)
            if target is None:
                continue  # unrecognized name — leave it alone
            ch, cc_num = target
            if not isinstance(value, int):
                continue
            if (ch, cc_num) in self.recall_ignore:
                _log("  --", f"{key} = {value}  (ignored)")
                continue
            self._send_cc(cc_num, ch, value)
            self._set_dest(cc_num, ch, value)
            _log("  ->", f"{key} = {value}  (ch{ch + 1}/CC{cc_num})")

    def _recall_single(self, preset, channel):
        """Recall a legacy preset with flat cc_values (no channel grouping)."""
        pc = preset.get("program_change")
        if pc is not None:
            if self.midi_out:
                self.midi_out.send(
                    mido.Message("program_change", channel=channel, program=pc))
            _log("  ->", f"PC {pc}")
        for cc_name, value in preset.get("cc_values", {}).items():
            cc_num = self._cc_name_to_number(cc_name)
            if cc_num is None:
                continue
            if (channel, cc_num) in self.recall_ignore:
                _log("  --", f"{self._cc_label(cc_num, channel)} = {value}  (ignored)")
                continue
            self._send_cc(cc_num, channel, value)
            self._set_dest(cc_num, channel, value)
            _log("  ->", f"{self._cc_label(cc_num, channel)} = {value}")

    def _recall_channeled(self, preset):
        """Recall a preset with CCs grouped by channel."""
        for ch_str, ch_data in preset["channels"].items():
            ch = int(ch_str)  # YAML may store as string
            for cc_name, value in ch_data.get("cc_values", {}).items():
                cc_num = self._cc_name_to_number(cc_name)
                if cc_num is None:
                    continue
                if (ch, cc_num) in self.recall_ignore:
                    _log("  --", f"{self._cc_label(cc_num, ch)} = {value}  (ignored)")
                    continue
                self._send_cc(cc_num, ch, value)
                self._set_dest(cc_num, ch, value)
                _log("  ->", f"{self._cc_label(cc_num, ch)} = {value}")

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

    def _cc_label(self, cc, channel):
        """Format a CC/channel pair with its resolved name (if known)."""
        name = self.resolved_destinations.get((channel, cc))
        if name:
            return f"CC{cc} ch{channel + 1} \"{name}\""
        return f"CC{cc} ch{channel + 1}"

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
        self._mark_dirty()
        return value

    def _sync_all_destinations(self):
        """Re-send all current destination values (e.g. after MIDI reset)."""
        count = 0
        for key, value in self.destination_states.items():
            parts = key.split("_")
            cc_num, channel = int(parts[0]), int(parts[1])
            self._send_cc(cc_num, channel, value)
            count += 1
        _log("SYNC", f"Re-sent {count} destination(s)")

    def _send_cc(self, cc, channel, value):
        """Send a mapped CC to hardware output."""
        if self.midi_out:
            self.midi_out.send(
                mido.Message("control_change",
                             channel=channel,
                             control=cc,
                             value=value)
            )

    # -- Center-snap ("wiggle to center") ------------------------------------

    _CENTER_WINDOW = 2.0        # seconds: reversals must happen within this
    _CENTER_REVERSALS = 4       # direction changes needed to arm snap
    _CENTER_IDLE = 0.20         # seconds of no movement → snap fires
    _CENTER_CONSISTENT = 1.0    # seconds of single-direction → reset tracker
    _CENTER_VALUE = 63          # value to snap to

    def _update_center_tracker(self, dest_key, delta, target_cc, target_ch,
                               min_v, max_v):
        """Track encoder direction changes for a center-enabled destination.

        Called after each relative movement.  When 4+ direction reversals
        happen within 2 s and then the encoder stops, snaps to 63.
        Consistent single-direction movement for 1 s resets the tracker.
        """
        now = time.monotonic()
        direction = 1 if delta > 0 else -1

        tr = self._center_trackers.get(dest_key)
        if tr is None:
            tr = {
                "last_dir": direction,
                "reversals": 0,
                "first_reversal": now,
                "dir_start": now,        # when current direction run started
                "armed": False,
            }
            self._center_trackers[dest_key] = tr

        prev_dir = tr["last_dir"]

        if direction != prev_dir:
            # Direction changed
            if tr["reversals"] == 0:
                tr["first_reversal"] = now
            tr["reversals"] += 1
            tr["last_dir"] = direction
            tr["dir_start"] = now

            # Check if reversals happened within the window
            if now - tr["first_reversal"] > self._CENTER_WINDOW:
                # Too slow — restart count from this reversal
                tr["reversals"] = 1
                tr["first_reversal"] = now

            # Arm if we've hit the threshold
            if tr["reversals"] >= self._CENTER_REVERSALS:
                tr["armed"] = True
        else:
            # Same direction — check for consistent movement reset
            if now - tr["dir_start"] >= self._CENTER_CONSISTENT:
                self._reset_center_tracker(dest_key)
                return

        # (Re)start the idle timer — if encoder stops while armed, snap
        self._restart_center_timer(dest_key, target_cc, target_ch,
                                   min_v, max_v)

    def _restart_center_timer(self, dest_key, target_cc, target_ch,
                              min_v, max_v):
        """Cancel any pending idle timer and start a fresh one."""
        old = self._center_timers.pop(dest_key, None)
        if old is not None:
            old.cancel()
        t = threading.Timer(
            self._CENTER_IDLE,
            self._center_idle_fired,
            args=(dest_key, target_cc, target_ch, min_v, max_v),
        )
        t.daemon = True
        t.start()
        self._center_timers[dest_key] = t

    def _center_idle_fired(self, dest_key, target_cc, target_ch,
                           min_v, max_v):
        """Called from timer thread when encoder has been idle."""
        self._center_timers.pop(dest_key, None)
        tr = self._center_trackers.get(dest_key)
        if tr is None or not tr["armed"]:
            # Not armed — just a normal pause; reset tracker
            self._reset_center_tracker(dest_key)
            return

        # Snap to center
        center = max(min_v, min(max_v, self._CENTER_VALUE))
        self._set_dest(target_cc, target_ch, center, min_v, max_v)
        self._send_cc(target_cc, target_ch, center)
        name = (self.resolved_destinations.get((target_ch, target_cc))
                or f"CC{target_cc} ch{target_ch + 1}")
        _log("CENTER", f"{name} → {center}")
        self._reset_center_tracker(dest_key)

    def _reset_center_tracker(self, dest_key):
        """Clear tracker state and cancel any pending timer."""
        self._center_trackers.pop(dest_key, None)
        old = self._center_timers.pop(dest_key, None)
        if old is not None:
            old.cancel()

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
            if "default" in action:
                default_val = action["default"]
            elif (target_ch, target_cc) in self.dest_defaults:
                default_val = self.dest_defaults[(target_ch, target_cc)]
            elif target_cc in self.cc_defaults:
                default_val = self.cc_defaults[target_cc]
            else:
                default_val = 64 if action_type == "relative" else 0
            min_v = action.get("min", 0)
            max_v = action.get("max", 127)

            self._init_dest(target_cc, target_ch, default_val)

            if action_type == "relative":
                delta = self._decode_relative(msg.value)
                current = self._get_dest(target_cc, target_ch)
                output = self._set_dest(target_cc, target_ch, current + delta, min_v, max_v)
            else:
                delta = None
                output = self._set_dest(target_cc, target_ch, msg.value, min_v, max_v)

            self._send_cc(target_cc, target_ch, output)

            # Center-snap: track encoder wiggle for center-enabled actions
            if action.get("center") and delta:
                dest_key = self._dest_key(target_cc, target_ch)
                self._update_center_tracker(dest_key, delta,
                                            target_cc, target_ch,
                                            min_v, max_v)

            # Debug: show mapping result (debounced)
            name = (self.resolved_destinations.get(
                        (target_ch, target_cc))
                    or action.get("name", ""))
            dest_key = self._dest_key(target_cc, target_ch)
            line = (f"CC{source_cc} ch{src_ch_1based} "
                    f"(shift=0x{self.shift_state:04X}) → "
                    f"CC{target_cc} ch{target_ch + 1} = {output}"
                    + (f" \"{name}\"" if name else ""))
            self._map_log_debounced(dest_key, line)

    # -- Note range mapping ---------------------------------------------------

    def _get_poly_state(self, key):
        """Get or create polyphony state for a pool (instance name or channel)."""
        if key not in self.poly_states:
            self.poly_states[key] = {"held": [], "active": []}
        return self.poly_states[key]

    def _poly_note_on(self, state, pitch, velocity, max_poly,
                      replace_priority, transpose, out_ch):
        """Process note-on through polyphony limiter.

        Tracks notes by their original (pre-transpose) pitch, along with
        the transpose and output channel used when the note was activated.
        Returns a list of action tuples:
          ("on", pitch, velocity, reason, transpose, out_ch)
          ("off", pitch, reason, transpose, out_ch)
        where reason is one of: "new", "retrigger", "replace", "steal".
        """
        now = time.monotonic()
        note_info = {"pitch": pitch, "velocity": velocity, "timestamp": now,
                     "transpose": transpose, "out_ch": out_ch}

        # Update held-notes list
        state["held"] = [n for n in state["held"] if n["pitch"] != pitch]
        state["held"].append(note_info)

        results = []

        # Already active → retrigger (update timestamp/velocity, re-send)
        if any(n["pitch"] == pitch for n in state["active"]):
            state["active"] = [n for n in state["active"]
                               if n["pitch"] != pitch]
            state["active"].append(note_info)
            results.append(("on", pitch, velocity, "retrigger",
                            transpose, out_ch))
            return results

        # Room available → just add
        if len(state["active"]) < max_poly:
            state["active"].append(note_info)
            results.append(("on", pitch, velocity, "new",
                            transpose, out_ch))
            return results

        # Polyphony full → steal a voice (use stolen note's stored values)
        to_replace = self._select_replace(state["active"], replace_priority)
        if to_replace is not None:
            results.append(("off", to_replace["pitch"], "steal",
                            to_replace["transpose"], to_replace["out_ch"]))
            state["active"] = [n for n in state["active"]
                               if n["pitch"] != to_replace["pitch"]]
            state["active"].append(note_info)
            results.append(("on", pitch, velocity, "replace",
                            transpose, out_ch))

        return results

    def _poly_note_off(self, state, pitch, fallback_priority,
                       transpose, out_ch):
        """Process note-off through polyphony limiter.

        Returns a list of action tuples (same format as _poly_note_on).
        Uses stored transpose/out_ch from the released and fallback notes
        so cross-range actions produce correct output.
        """
        # Remove from held
        state["held"] = [n for n in state["held"] if n["pitch"] != pitch]

        results = []

        # Only act if this note is currently active
        active_note = next(
            (n for n in state["active"] if n["pitch"] == pitch), None)
        if active_note is None:
            return results

        # Use the stored values from when this note was activated
        rel_transpose = active_note.get("transpose", transpose)
        rel_out_ch = active_note.get("out_ch", out_ch)
        results.append(("off", pitch, "release", rel_transpose, rel_out_ch))
        state["active"] = [n for n in state["active"]
                           if n["pitch"] != pitch]

        # Try to activate a held-but-inactive note as fallback
        fallback = self._select_fallback(
            state["held"], state["active"], fallback_priority)
        if fallback is not None:
            state["active"].append(fallback)
            results.append(("on", fallback["pitch"],
                            fallback["velocity"], "fallback",
                            fallback.get("transpose", transpose),
                            fallback.get("out_ch", out_ch)))

        return results

    @staticmethod
    def _select_replace(active, priority):
        """Choose which active note to steal based on replace_priority."""
        if not active:
            return None
        if priority == "lowest":
            return min(active, key=lambda n: n["pitch"])
        if priority == "second_lowest":
            if len(active) < 2:
                return active[0]
            by_pitch = sorted(active, key=lambda n: n["pitch"])
            return by_pitch[1]
        if priority == "highest":
            return max(active, key=lambda n: n["pitch"])
        if priority == "oldest":
            return min(active, key=lambda n: n["timestamp"])
        if priority == "second_oldest":
            if len(active) < 2:
                return active[0]
            by_time = sorted(active, key=lambda n: n["timestamp"])
            return by_time[1]
        if priority == "most_recent":
            return max(active, key=lambda n: n["timestamp"])
        return active[0]

    @staticmethod
    def _select_fallback(held, active, priority):
        """Choose which held-but-inactive note to reactivate."""
        active_pitches = {n["pitch"] for n in active}
        available = [n for n in held if n["pitch"] not in active_pitches]
        if not available:
            return None
        if priority == "most_recent":
            return max(available, key=lambda n: n["timestamp"])
        if priority == "highest":
            return max(available, key=lambda n: n["pitch"])
        if priority == "lowest":
            return min(available, key=lambda n: n["pitch"])
        return available[-1]  # fallback: most recently added

    def _process_note_mapping(self, msg):
        """Run a note message through the note range mapping table.

        Returns a list of mido messages to forward (may be empty if the
        note matched a range that produced no valid output, or multiple
        if several ranges matched).  Returns ``None`` if no ranges
        matched at all (caller should pass through unchanged).
        """
        if not self.note_mappings:
            return None

        note = msg.note
        src_ch_1based = msg.channel + 1
        is_note_on = msg.type == "note_on" and msg.velocity > 0
        results = []
        matched = False

        for nm in self.note_mappings:
            # Range check (inclusive)
            if note < nm["low"] or note > nm["high"]:
                continue

            # Channel filter
            if "from_channel" in nm:
                if nm["from_channel"] != src_ch_1based:
                    continue

            # Shift state match
            if (self.shift_state & nm["mask"]) != nm["compare"]:
                continue

            matched = True

            out_ch = (nm["channel"] - 1) if "channel" in nm else msg.channel
            transpose = nm.get("transpose", 0)
            name = nm.get("name", "")

            inst_name = nm.get("polyphony_instance")
            has_poly = "max_polyphony" in nm or inst_name is not None

            if has_poly:
                # ---- Polyphony-managed note processing ----
                # Resolve pool key and parameters
                if inst_name is not None:
                    pool_key = inst_name
                    params = self.poly_instances.get(inst_name, {})
                    max_poly = params.get("max_polyphony", 1)
                    replace_pri = params.get("replace_priority", "lowest")
                    fallback_pri = params.get(
                        "fallback_priority", "most_recent")
                else:
                    # Legacy: no instance name, key by output channel
                    pool_key = out_ch
                    max_poly = nm["max_polyphony"]
                    replace_pri = nm.get("replace_priority", "lowest")
                    fallback_pri = nm.get(
                        "fallback_priority", "most_recent")

                state = self._get_poly_state(pool_key)

                if is_note_on:
                    actions = self._poly_note_on(
                        state, note, msg.velocity, max_poly, replace_pri,
                        transpose, out_ch)
                else:
                    actions = self._poly_note_off(
                        state, note, fallback_pri, transpose, out_ch)

                pool_label = (f"'{inst_name}'"
                              if inst_name else f"ch{out_ch + 1}")
                for action in actions:
                    if action[0] == "on":
                        # action: ("on", pitch, vel, reason, transpose, out_ch)
                        a_transpose = action[4]
                        a_out_ch = action[5]
                        out_note = action[1] + a_transpose
                        if 0 <= out_note <= 127:
                            results.append(mido.Message(
                                "note_on", note=out_note, channel=a_out_ch,
                                velocity=action[2]))
                            _log("NOTE", f"poly {action[3]} "
                                 f"note={action[1]}→{out_note} "
                                 f"ch{a_out_ch + 1} vel={action[2]} "
                                 f"\"{name}\" "
                                 f"[{pool_label} "
                                 f"{len(state['active'])}/{max_poly}]")
                    elif action[0] == "off":
                        # action: ("off", pitch, reason, transpose, out_ch)
                        a_transpose = action[3]
                        a_out_ch = action[4]
                        out_note = action[1] + a_transpose
                        if 0 <= out_note <= 127:
                            results.append(mido.Message(
                                "note_off", note=out_note, channel=a_out_ch,
                                velocity=0))
                            _log("NOTE", f"poly {action[2]} "
                                 f"note={action[1]}→{out_note} "
                                 f"ch{a_out_ch + 1} \"{name}\" "
                                 f"[{pool_label} "
                                 f"{len(state['active'])}/{max_poly}]")
            else:
                # ---- Simple pass-through with optional transpose/channel ----
                out_note = note + transpose
                if out_note < 0 or out_note > 127:
                    _log("NOTE", f"note {note} ch{src_ch_1based} "
                         f"transpose {transpose:+d} → {out_note} "
                         f"out of range, skipped \"{name}\"")
                    continue

                out_msg = msg.copy(note=out_note, channel=out_ch)
                results.append(out_msg)

                # Debug logging
                changes = []
                if out_note != note:
                    changes.append(f"note {note}→{out_note}")
                if out_ch != msg.channel:
                    changes.append(f"ch{src_ch_1based}→{out_ch + 1}")
                detail = ", ".join(changes) if changes else "no change"
                line = (f"{msg.type} note={note} ch{src_ch_1based} "
                        f"(shift=0x{self.shift_state:04X}) → "
                        f"{detail} \"{name}\"")
                _log("NOTE", line)

        if not matched:
            return None
        return results

    # -- Transport handling ---------------------------------------------------

    def _handle_transport(self, cc, channel):
        """Route a transport CC press to the active mode handler.

        *channel* is the 0-based MIDI channel of the transport CC message,
        used to select which preset bank to navigate.
        """
        if self.intercept_mode:
            self._transport_intercept(cc, channel)
        else:
            self._transport_passthrough(cc)

    def _transport_intercept(self, cc, channel):
        """Handle transport in intercept mode (CCs consumed for preset mgmt).

        *channel* (0-based) selects the preset bank for arrow navigation.

        Rec×1 + note  → save preset to that note (bank chosen by note channel)
        Rec×1 + Play  → save into most recently loaded/saved preset
        Rec×3 + Stop  → switch to pass-through mode
        Rec×3 + Play  → switch to pass-through mode
        Rec×6 + Play  → disable write-protect on last preset
        Rec×6 + Stop  → enable write-protect on last preset
        Play          → load mode (next note recalls preset)
        Stop          → cancel any pending mode
        """
        if cc == CC_REC:
            if self.load_mode:
                self.load_mode = False
                _log("LOAD", "Load mode cancelled (rec pressed).")
            self.rec_counter += 1
            if self.rec_counter == 1:
                _log("SAVE", "Rec×1 — press a note to save, or Play to save to last preset")
            elif self.rec_counter == 3:
                _log("TRANS", "Rec×3 — press Stop or Play for pass-through")
            elif self.rec_counter == 6:
                _log("TRANS", "Rec×6 — Play=unlock / Stop=lock last preset")
            else:
                _log("TRANS", f"Rec (counter={self.rec_counter})")

        elif cc == CC_PLAY:
            counter = self.rec_counter
            self.rec_counter = 0
            if counter == 3:
                self.intercept_mode = False
                self._mark_dirty()
                _log("MODE", "Switched to PASS-THROUGH mode")
            elif counter == 6:
                self._set_write_protect(False)
            elif counter == 1:
                if self._last_preset:
                    ch, note = self._last_preset
                    self.preset_cursor[ch] = note
                    self._mark_dirty()
                    self._save_preset_from_state(note, ch)
                else:
                    _log("WARN", "No last preset — press a note instead")
            elif counter == 0:
                self.load_mode = True
                _log("LOAD", "Direct load — send a note-on to select the preset.")

        elif cc == CC_STOP:
            counter = self.rec_counter
            self.rec_counter = 0
            if counter == 3:
                self.intercept_mode = False
                self._mark_dirty()
                _log("MODE", "Switched to PASS-THROUGH mode")
            elif counter == 6:
                self._set_write_protect(True)
            else:
                if self.load_mode:
                    self.load_mode = False
                    _log("LOAD", "Load mode cancelled.")

        elif cc == CC_REWIND:
            self.rec_counter = 0
            self.load_mode = False
            self._navigate_preset(channel, -1)

        elif cc == CC_FORWARD:
            self.rec_counter = 0
            self.load_mode = False
            self._navigate_preset(channel, 1)

    def _transport_passthrough(self, cc):
        """Handle transport in pass-through mode (CCs forwarded to hardware).

        Rec×3 + Stop → switch back to intercept mode
        Rec×3 + Play → switch back to intercept mode
        """
        if cc == CC_REC:
            self.rec_counter += 1
            if self.rec_counter == 3:
                _log("TRANS", "Rec×3 — press Stop or Play to enter intercept")
            else:
                _log("TRANS", f"Rec (counter={self.rec_counter})")

        elif cc == CC_PLAY:
            counter = self.rec_counter
            self.rec_counter = 0
            if counter == 3:
                self.intercept_mode = True
                self._mark_dirty()
                _log("MODE", "Switched to INTERCEPT mode")
            # Play is also forwarded to hardware (handled by caller)

        elif cc == CC_STOP:
            counter = self.rec_counter
            self.rec_counter = 0
            if counter == 3:
                self.intercept_mode = True
                self._mark_dirty()
                _log("MODE", "Switched to INTERCEPT mode")
            # Stop is also forwarded to hardware (handled by caller)

    def _navigate_preset(self, channel, direction):
        """Move the preset cursor for *channel*'s bank by *direction* and recall."""
        bank = self.presets.get(channel, {})
        slots = sorted(bank.keys())
        if not slots:
            _log("WARN", f"No presets in ch{channel + 1} bank — nothing to navigate.")
            return

        cursor = self.preset_cursor.get(channel)
        if cursor is not None and cursor in slots:
            idx = slots.index(cursor) + direction
        else:
            # First navigation: start at beginning or end
            idx = 0 if direction > 0 else len(slots) - 1

        idx = idx % len(slots)
        self.preset_cursor[channel] = slots[idx]
        self._mark_dirty()
        _log("NAV", f"Preset ch{channel + 1}/{slots[idx]} "
             f"(slot {idx + 1}/{len(slots)})")
        self._recall_preset(channel, slots[idx])

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
                self._handle_transport(msg.control, msg.channel)
            if self.intercept_mode:
                return  # Consumed — do not forward
            self._forward(msg)
            return

        # Note-on (velocity > 0)
        if msg.type == "note_on" and msg.velocity > 0:
            if self.intercept_mode and self.rec_counter == 1:
                self.rec_counter = 0
                self.preset_cursor[msg.channel] = msg.note
                self._mark_dirty()
                self._save_preset_from_state(msg.note, msg.channel)
                return  # Intercepted
            if self.load_mode:
                self.load_mode = False
                self.preset_cursor[msg.channel] = msg.note
                self._mark_dirty()
                self._recall_preset(msg.channel, msg.note)
                return  # Intercepted
            mapped = self._process_note_mapping(msg)
            if mapped is not None:
                for m in mapped:
                    self._forward(m)
                return
            self._forward(msg)
            return

        # Note-off (or note-on with velocity 0)
        if msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            mapped = self._process_note_mapping(msg)
            if mapped is not None:
                for m in mapped:
                    self._forward(m)
                return
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
            self._process_cc_mapping(msg)
            return  # CCs never forwarded raw — only mapped outputs are sent

        # Program Change — forward to hardware
        if msg.type == "program_change":
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
        cc_count = sum(len(s) for s in self.cc_sets.values())

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
        total_presets = sum(len(bank) for bank in self.presets.values())
        bank_info = ", ".join(f"ch{ch + 1}:{len(bank)}"
                              for ch, bank in sorted(self.presets.items()))
        _log("INIT", f"Presets      : {total_presets} loaded"
             + (f" ({bank_info})" if bank_info else ""))
        _log("INIT", f"CC sets      : {cc_count} mappings in {len(self.cc_sets)} set(s)")
        action_count = sum(len(v) for v in self.cc_mappings.values())
        _log("INIT", f"CC mappings  : {len(self.cc_mappings)} source CCs, {action_count} actions")
        poly_count = sum(1 for nm in self.note_mappings
                        if "max_polyphony" in nm
                        or "polyphony_instance" in nm)
        _log("INIT", f"Note mappings: {len(self.note_mappings)} range(s)"
             + (f" ({poly_count} with polyphony)" if poly_count else ""))
        if self.poly_instances:
            for inst_name, params in self.poly_instances.items():
                _log("INIT", f"  poly '{inst_name}': "
                     f"max={params['max_polyphony']} "
                     f"fallback={params['fallback_priority']} "
                     f"replace={params['replace_priority']}")
        _log("INIT", f"Shift CCs    : {sorted(self.shift_ccs)} | Joystick CCs: {sorted(self.joystick_ccs)}")
        tmode = "intercept" if self.intercept_mode else "passthrough"
        _log("INIT", f"Transport    : {tmode}")
        save_int = self.config.get("state_save_interval", STATE_SAVE_INTERVAL)
        _log("INIT", f"State file   : {self._state_path()} "
             f"(save every {save_int}s)")
        if dest_count:
            _log("INIT", f"Destinations : {dest_count} configured, "
                 f"{len(self.resolved_destinations)} resolved names")
            for dest_id, dest_cfg in self.destinations_map.items():
                prefix = dest_cfg.get("prefix", "")
                channels_cfg = dest_cfg.get("channels", {})
                ch_list = sorted(int(c) for c in channels_cfg)
                _log("INIT", f"  {dest_id}: prefix='{prefix}', channels={ch_list}")
                for ch_yaml in ch_list:
                    ch_0 = ch_yaml - 1  # YAML 1-based → internal 0-based
                    ch_cfg = channels_cfg.get(ch_yaml, channels_cfg.get(str(ch_yaml), {}))
                    group = ch_cfg.get("cc_group", "-") if isinstance(ch_cfg, dict) else "-"
                    n_overrides = sum(1 for k in (ch_cfg if isinstance(ch_cfg, dict) else {})
                                      if k not in self._CH_RESERVED_KEYS
                                      and isinstance(k, int))
                    n_resolved = sum(1 for (c, _) in self.resolved_destinations
                                     if c == ch_0)
                    parts = f"group={group}, {n_resolved} names"
                    if n_overrides:
                        parts += f" ({n_overrides} override(s))"
                    _log("INIT", f"    ch {ch_yaml}: {parts}")
            # Flat resolved mapping table
            _log("INIT", "Resolved parameter map:")
            for (ch, cc_num), name in sorted(self.resolved_destinations.items()):
                default = self.dest_defaults.get((ch, cc_num))
                default_str = f"  default={default}" if default is not None else ""
                _log("INIT", f"    ch{ch + 1}/CC{cc_num:>3d}  {name}{default_str}")

        print()
        mid = self.manufacturer_id
        dev = self.device_id
        print("SysEx commands (hex bytes including F0/F7):")
        print(f"  Load Preset   : F0 {mid:02X} {dev:02X} {CMD_LOAD_PRESET:02X} F7")
        print(f"  (Name reply)  : F0 {mid:02X} {dev:02X} {CMD_PRESET_NAME:02X} <ascii> F7")
        print(f"  Debug text    : F0 {mid:02X} {dev:02X} {CMD_DEBUG_TEXT:02X} <ascii…> F7")
        print(f"  Remote cmd    : F0 {mid:02X} {dev:02X} {CMD_REMOTE_CMD:02X} <ascii…> F7")
        print(f"  Device cmd    : F0 {mid:02X} {dev:02X} {CMD_DEVICE_CMD:02X} <ascii…> F7")
        print()
        print("Transport (intercept mode):")
        print("  Play            : Load — next note-on recalls that preset")
        print("  Rec×1 + note    : Save current state to that note")
        print("  Rec×3 + Stop    : Switch to pass-through mode")
        print("  Rec×3 + Play    : Switch to pass-through mode")
        print("  Rewind/Forward  : Navigate presets sequentially")
        print("  Stop            : Cancel / clear counter")
        print()
        print("Device commands: ping, status, list, mode [intercept|passthrough]")
        print()
        print("Listening… (Ctrl-C to quit)")
        print()

        # Start background state saver
        self._state_stop.clear()
        saver = threading.Thread(target=self._state_saver_loop, daemon=True)
        saver.start()

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
            self._state_stop.set()
            saver.join(timeout=2)
            # Cancel any pending center-snap timers
            for t in self._center_timers.values():
                t.cancel()
            self._center_timers.clear()
            self._center_trackers.clear()
            # Final state flush
            try:
                self._save_state()
                _log("STATE", "State saved.")
            except Exception as exc:
                _log("WARN", f"Final state save failed: {exc}")
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
