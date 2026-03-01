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
                                [--no-input] [--no-iac-input]
"""

import argparse
import queue
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

    def __init__(self, config_dir=None, no_input=False, no_iac_input=False):
        self.config_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
        self.config_dir.mkdir(parents=True, exist_ok=True)

        self.config = self._load_or_create_config()
        (self.cc_sets,
         self.cc_default_set,
         self.cc_set_defaults,
         self.cc_set_tags,
         self.cc_set_explicit_tags) = self._load_cc_sets()
        self.destinations_map = self._load_destinations()
        (self.resolved_destinations,
         self.reverse_destinations,
         self.auto_destinations,
         self.dest_defaults,
         self.dest_tags) = self._resolve_destinations()
        self.bank_tags = self._load_bank_tags()
        # Flat CC-number → default, merged from all sets (fallback when
        # destinations aren't configured).
        self.cc_defaults = {}
        for defs in self.cc_set_defaults.values():
            self.cc_defaults.update(defs)
        self.recall_ignore = self._build_recall_ignore()
        self.presets = self._load_presets()

        # Sequential step counters (populated by _load_cc_mappings)
        self.seq_states = {}   # {name: current_step (1-based)}
        self.seq_defs = {}     # {name: {reset_cc, reset_ch, next_cc, next_ch, max}}

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

        # Seq pulse delay: config.yaml "seq_delay" overrides the class default
        self._seq_delay = self.config.get("seq_delay", self._SEQ_PULSE_DELAY)

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

        # Input control flags (proxy mode only)
        self.no_input = no_input          # --no-input: skip routing.inputs
        self.no_iac_input = no_iac_input  # --no-iac-input: skip routing.iac_input

        # Source device tag for debug logging (set per message in run loop)
        self._msg_source = ""

        # MIDI ports (opened in run())
        self.midi_in = None
        self.midi_out = None
        self.midi_return = None  # Recall output (= midi_out in standalone mode)
        self.extra_inputs = []   # Additional input ports from routing.inputs
        self.p2p_channels = set()  # 0-based channels with pressure-to-poly

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
        saved_seq = data.get("seq_states")
        if isinstance(saved_seq, dict):
            self.seq_states.update(
                {k: int(v) for k, v in saved_seq.items()
                 if isinstance(v, (int, float))}
            )
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
            "seq_states": dict(self.seq_states),
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
    def _normalize_cc_set(raw_set, group_tags=None):
        """Normalise a CC name set.

        Returns ``(names_dict, defaults_dict, tags_dict, explicit_tag_ccs)``.

        Entries may be plain strings or dicts with ``name`` and optional
        ``default`` and ``tags``::

            3: to_duck_out               # plain string, no default/tags
            7: {name: to_perf_filter, default: 127, tags: [mix]}

        *group_tags* (list or None) are inherited by every entry in the set.
        Per-entry tags **replace** group_tags.  Entries with no tags at all
        inherit group_tags, or get the implicit ``["default"]`` tag.

        *explicit_tag_ccs* is the set of CC numbers that had their own
        ``tags`` key (as opposed to inheriting from *group_tags*).
        """
        names = {}
        defaults = {}
        tags = {}
        explicit_tag_ccs = set()
        base_tags = set(group_tags) if group_tags else set()
        for cc_num, entry in (raw_set or {}).items():
            cc_num = int(cc_num)
            if isinstance(entry, dict):
                names[cc_num] = str(entry["name"])
                if "default" in entry:
                    defaults[cc_num] = int(entry["default"])
                entry_tags = entry.get("tags")
                if entry_tags:
                    tags[cc_num] = set(entry_tags)
                    explicit_tag_ccs.add(cc_num)
                elif base_tags:
                    tags[cc_num] = set(base_tags)
                else:
                    tags[cc_num] = {"default"}
            else:
                names[cc_num] = str(entry)
                tags[cc_num] = set(base_tags) if base_tags else {"default"}
        return names, defaults, tags, explicit_tag_ccs

    def _load_cc_sets(self):
        """Load CC name mappings.

        Returns ``(name_sets, default_names, cc_set_defaults, cc_set_tags,
        cc_set_explicit_tags)``.

        *name_sets* and *default_names* are string-only dicts as before.
        *cc_set_defaults* maps ``set_name → {cc_num: default_value}``
        for entries that specified a ``default``.
        *cc_set_tags* maps ``set_name → {cc_num: set_of_tags}``
        for per-CC tag sets (per-entry tags replace group-level tags;
        untagged entries inherit group tags or get ``{"default"}``).
        *cc_set_explicit_tags* maps ``set_name → set_of_cc_nums``
        for CCs that had explicit per-entry tags in the cc_set.

        Named sets may carry a ``tags`` key at the group level::

            cc_sets:
              mix_ccs:
                tags: [mix]
                3: {name: to_duck_out, tags: [routing]}
                7: to_perf_filter           # inherits group tag "mix"

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
                    cc_set_tags = {}
                    cc_set_explicit = {}
                    for set_name, raw_set in raw.items():
                        # Extract group-level tags before normalizing
                        group_tags = None
                        if isinstance(raw_set, dict):
                            group_tags = raw_set.pop("tags", None)
                        names, defs, tags, explicit = self._normalize_cc_set(
                            raw_set, group_tags=group_tags)
                        name_sets[set_name] = names
                        if defs:
                            cc_set_defaults[set_name] = defs
                        if tags:
                            cc_set_tags[set_name] = tags
                        if explicit:
                            cc_set_explicit[set_name] = explicit
                    return (name_sets, name_sets.get("default", {}),
                            cc_set_defaults, cc_set_tags, cc_set_explicit)
                else:
                    # Flat format — single set
                    names, defs, tags, explicit = self._normalize_cc_set(raw)
                    cc_set_defaults = {"default": defs} if defs else {}
                    cc_set_tags = {"default": tags} if tags else {}
                    cc_set_explicit = {"default": explicit} if explicit else {}
                    return ({"default": names}, names,
                            cc_set_defaults, cc_set_tags, cc_set_explicit)
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
        return {"default": default_names}, default_names, {}, {}, {}

    _DEST_RESERVED_KEYS   = {"prefix", "channels"}
    _CH_RESERVED_KEYS     = {"cc_group", "prefix", "tags"}
    _PRESET_RESERVED_KEYS = {"name", "read_only", "channels",
                             "program_change", "cc_values", "seq_states"}

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
          5. Propagate per-(channel, cc) tags from cc_set_tags + overrides.

        Returns a tuple of five flat dicts:
          forward:       ``{(channel, cc_num): name}``
          reverse:       ``{name: (channel, cc_num)}``
          auto:          ``{unprefixed_name: [(channel, cc_num), ...]}``
          dest_defaults: ``{(channel, cc_num): default_value}``
          dest_tags:     ``{(channel, cc_num): set_of_tags}``
        Logs warnings for every conflict found.
        """
        forward  = {}   # (ch, cc_num) -> name
        reverse  = {}   # name -> (ch, cc_num)
        auto     = {}   # unprefixed_name -> [(ch, cc_num), ...]
        defaults = {}   # (ch, cc_num) -> default value
        tags     = {}   # (ch, cc_num) -> set of tag strings
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
                ch_tags = {}
                group_defaults = (self.cc_set_defaults.get(group_name, {})
                                  if group_name else {})
                group_tags = (self.cc_set_tags.get(group_name, {})
                              if group_name else {})
                # Channel-level tags override the cc_set's inherited tags
                # but leave per-entry explicit tags from the cc_set alone.
                ch_level_tags = ch_cfg.get("tags")
                explicit_ccs = (self.cc_set_explicit_tags.get(group_name, set())
                                if group_name else set())
                if group_name and group_name in self.cc_sets:
                    for cc_num, raw_name in self.cc_sets[group_name].items():
                        cc_num = int(cc_num)
                        ch_names[cc_num] = f"{ch_prefix}_{raw_name}" if ch_prefix else raw_name
                        # auto map: unprefixed name → all (ch, cc) targets
                        auto.setdefault(raw_name, []).append((ch, cc_num))
                        # Propagate default from cc_set
                        if cc_num in group_defaults:
                            defaults[(ch, cc_num)] = group_defaults[cc_num]
                        # Propagate tags: channel-level tags override inherited,
                        # but explicit per-entry tags from cc_set are kept.
                        if ch_level_tags and cc_num not in explicit_ccs:
                            ch_tags[cc_num] = set(ch_level_tags)
                        elif cc_num in group_tags:
                            ch_tags[cc_num] = set(group_tags[cc_num])
                        else:
                            ch_tags[cc_num] = {"default"}

                # 2) Override keys: integer CC numbers or cc_group names
                # Build reverse lookup so names from the cc_group can be
                # used as override keys (e.g. ``vcf_cutoff: {default: 80}``).
                group_set = (self.cc_sets.get(group_name, {})
                             if group_name else {})
                name_to_cc = {v: int(k) for k, v in group_set.items()}

                for key, value in ch_cfg.items():
                    if key in self._CH_RESERVED_KEYS:
                        continue
                    # Resolve key → cc_num (integer literal or cc_group name)
                    try:
                        cc_num = int(key)
                    except (ValueError, TypeError):
                        cc_num = name_to_cc.get(str(key))
                        if cc_num is None:
                            continue
                    if isinstance(value, dict):
                        if "name" in value:
                            ch_names[cc_num] = str(value["name"])
                        if "default" in value:
                            defaults[(ch, cc_num)] = int(value["default"])
                        # Per-CC override tags replace inherited tags
                        override_tags = value.get("tags")
                        if override_tags:
                            ch_tags[cc_num] = set(override_tags)
                    else:
                        ch_names[cc_num] = str(value)

                # 3) Merge into flat maps with conflict detection
                for cc_num, name in ch_names.items():
                    fwd_prev = forward.get((ch, cc_num))
                    if fwd_prev is not None and fwd_prev != name:
                        _log("WARN",
                             f"Destination '{dest_id}': ch{ch + 1}/CC{cc_num} "
                             f"already mapped to '{fwd_prev}', "
                             f"overwriting with '{name}'")
                    forward[(ch, cc_num)] = name

                    rev_prev = reverse.get(name)
                    if rev_prev is not None and rev_prev != (ch, cc_num):
                        _log("WARN",
                             f"Destination '{dest_id}': name conflict — "
                             f"'{name}' maps to both ch{rev_prev[0] + 1}/CC{rev_prev[1]} "
                             f"and ch{ch + 1}/CC{cc_num}")
                    reverse[name] = (ch, cc_num)

                # 4) Merge tags into flat map
                for cc_num, tag_set in ch_tags.items():
                    tags[(ch, cc_num)] = tag_set

        if defaults:
            _log("INIT", f"CC defaults from cc_sets: {len(defaults)} entries")
        tag_count = sum(1 for t in tags.values() if t != {"default"})
        if tag_count:
            _log("INIT", f"CC tags: {tag_count} entries with non-default tags")
        return forward, reverse, auto, defaults, tags

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

    def _load_bank_tags(self):
        """Load per-bank tag filters from config.yaml.

        Configured as ``bank_tags``, mapping 1-based channel numbers to
        lists of tag strings.  Only destination states whose tags intersect
        the bank's tag list are included in saves and recalls::

            bank_tags:
              1: [default, synth, mix]
              2: [default, performance]

        Banks not listed default to ``["default"]`` — only parameters with
        the implicit/explicit "default" tag are included.

        Returns ``{channel_0based: set_of_tags}``.
        """
        raw = self.config.get("bank_tags", {})
        bank_tags = {}
        if isinstance(raw, dict):
            for ch, tag_list in raw.items():
                ch_0 = int(ch) - 1
                if isinstance(tag_list, list):
                    bank_tags[ch_0] = set(tag_list)
                elif isinstance(tag_list, str):
                    bank_tags[ch_0] = {tag_list}
        if bank_tags:
            info = ", ".join(f"ch{ch + 1}:{sorted(t)}"
                             for ch, t in sorted(bank_tags.items()))
            _log("INIT", f"Bank tags: {info}")
        return bank_tags

    def _tags_for_dest(self, channel, cc_num):
        """Return the tag set for a (channel, cc_num) destination pair.

        Falls back to ``{"default"}`` if no tags were configured.
        """
        return self.dest_tags.get((channel, cc_num), {"default"})

    def _bank_allows(self, bank_channel, dest_channel, cc_num):
        """Check whether a bank allows a particular destination parameter.

        Returns True if the parameter's tags intersect the bank's allowed
        tags.  Banks not configured in ``bank_tags`` default to
        ``{"default"}``.
        """
        allowed = self.bank_tags.get(bank_channel, {"default"})
        param_tags = self._tags_for_dest(dest_channel, cc_num)
        return bool(allowed & param_tags)

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

        # Build seq definitions from mapping actions
        self.seq_defs = {}
        for source_cc, actions in self.cc_mappings.items():
            for action in actions:
                for seq_key in ("seq_reset", "seq_next"):
                    if seq_key not in action:
                        continue
                    name = action[seq_key]
                    defn = self.seq_defs.setdefault(name, {"max": 4})
                    ch = action.get("channel")
                    ch_0 = (ch - 1) if ch is not None else None
                    if seq_key == "seq_reset":
                        defn["reset_cc"] = action.get("cc")
                        defn["reset_ch"] = ch_0
                    else:
                        defn["next_cc"] = action.get("cc")
                        defn["next_ch"] = ch_0
                        if "seq_max" in action:
                            defn["max"] = action["seq_max"]
                    if "seq_delay" in action:
                        defn["delay"] = action["seq_delay"]

        # Build set of (ch, cc) pairs used by seq counters — these are
        # momentary control CCs and must not be sent during normal CC recall.
        self.seq_ccs = set()
        for defn in self.seq_defs.values():
            if defn.get("reset_cc") is not None and defn.get("reset_ch") is not None:
                self.seq_ccs.add((defn["reset_ch"], defn["reset_cc"]))
            if defn.get("next_cc") is not None and defn.get("next_ch") is not None:
                self.seq_ccs.add((defn["next_ch"], defn["next_cc"]))

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
            # polyphonic_distribution: round-robin voice slots across channels
            dist = nm.get("polyphonic_distribution")
            if dist is not None:
                # Convert 1-based YAML channels to 0-based
                nm["_dist_slots"] = [int(ch) - 1 for ch in dist]
                # Implicitly set max_polyphony from slot count
                if "max_polyphony" not in nm:
                    nm["max_polyphony"] = len(nm["_dist_slots"])

        # Resolve named polyphony instances
        self.poly_instances = self._resolve_poly_instances()

        # Runtime state for the mapping engine
        self.shift_state = 0            # 16-bit bitmask
        self.joystick_states = {}       # index -> {neg_held, pos_held, latch}
        self.destination_states = {}    # "cc_ch" -> current value
        self._map_log_times = {}        # "cc_ch" -> last log timestamp (debounce)
        self.poly_states = {}           # pool_key -> {held: [...], active: [...]}
        self.note_on_origins = {}       # (ch0, note) -> origin info for note-off routing
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

        Parameters are filtered by ``bank_tags``: only destination states
        whose tags intersect the bank's allowed tag list are included.
        Banks not listed in ``bank_tags`` default to ``["default"]``.
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
        # Clear stale data — we'll rebuild from current state
        preset.pop("channels", None)
        preset.pop("cc_values", None)
        preset.pop("seq_states", None)
        for key in list(preset):
            if key not in self._PRESET_RESERVED_KEYS and key in self.reverse_destinations:
                del preset[key]

        # Partition destination_states into known (flat) and unknown (channeled)
        unknown_channels = {}
        named_count = 0
        skipped_count = 0
        for key, value in self.destination_states.items():
            parts = key.split("_")
            cc_num, ch = int(parts[0]), int(parts[1])
            if cc_num == 0:
                continue  # CC 0 is not a valid destination
            if (ch, cc_num) in self.seq_ccs:
                continue  # seq CCs are momentary — saved via seq_states
            # Tag filtering: skip parameters whose tags don't match this bank
            if not self._bank_allows(channel, ch, cc_num):
                skipped_count += 1
                continue
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

        # Include sequential step counter positions
        if self.seq_states:
            preset["seq_states"] = dict(self.seq_states)

        self.presets.setdefault(channel, {})[note] = preset
        self._save_preset_to_disk(channel, note)
        self._last_preset = (channel, note)
        unknown_count = sum(len(ccs) for ccs in unknown_channels.values())
        total = named_count + unknown_count
        tag_info = f", {skipped_count} filtered by tags" if skipped_count else ""
        _log("SAVE", f"Preset ch{channel + 1}/{note} saved ({total} CCs: "
             f"{named_count} named, {unknown_count} in channels{tag_info}).")

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

    def _toggle_write_protect(self, channel, note):
        """Toggle write-protection on a specific preset identified by channel/note."""
        bank = self.presets.get(channel, {})
        preset = bank.get(note)
        if preset is None:
            _log("WARN", f"Preset ch{channel + 1}/{note} not found — nothing to protect.")
            return
        currently_locked = preset.get("read_only", False)
        preset["read_only"] = not currently_locked
        self._save_preset_to_disk(channel, note)
        state = "LOCKED" if not currently_locked else "UNLOCKED"
        name = preset.get("name", f"ch{channel + 1}/{note}")
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
        """Send all stored CC/PC values for *note* in *channel*'s bank.

        Parameters are filtered by ``bank_tags``: only values whose
        destination tags intersect the bank's allowed tags are recalled.
        """
        bank = self.presets.get(channel, {})
        preset = bank.get(note)
        if preset is None:
            _log("WARN", f"No preset stored for ch{channel + 1}/{note}.")
            return

        self._last_preset = (channel, note)

        name = preset.get("name", "")
        _log("RECALL", f"Preset ch{channel + 1}/{note}: \"{name}\"")

        # 1) Top-level named parameters (resolved via reverse_destinations)
        self._recall_named(preset, channel)

        # 2) Channeled CC values (fallback for unknowns / legacy)
        if "channels" in preset:
            self._recall_channeled(preset, channel)
        elif "cc_values" in preset:
            # Legacy flat format — use channel 0 as fallback
            self._recall_single(preset, 0, channel)

        # 3) Sequential step counter positions
        if "seq_states" in preset:
            self._recall_seq_states(preset["seq_states"])

        # Send preset name back via SysEx
        if name:
            name_bytes = self._encode_name(name)
            sysex_data = [self.manufacturer_id, self.device_id, CMD_PRESET_NAME] + name_bytes
            self.midi_return.send(mido.Message("sysex", data=sysex_data))
            _log("  ->", f"Name: \"{name}\"")

    def _recall_named(self, preset, bank_channel):
        """Recall top-level named parameters via reverse_destinations.

        *bank_channel* is the 0-based bank channel used for tag filtering.
        """
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
            if (ch, cc_num) in self.seq_ccs:
                continue  # seq CCs are handled by _recall_seq_states
            if not self._bank_allows(bank_channel, ch, cc_num):
                _log("  --", f"{key} = {value}  (filtered by tags)")
                continue
            self._send_cc(cc_num, ch, value)
            self._set_dest(cc_num, ch, value)
            _log("  ->", f"{key} = {value}  (ch{ch + 1}/CC{cc_num})")

    def _recall_single(self, preset, channel, bank_channel):
        """Recall a legacy preset with flat cc_values (no channel grouping).

        *bank_channel* is the 0-based bank channel used for tag filtering.
        """
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
            if (channel, cc_num) in self.seq_ccs:
                continue  # seq CCs are handled by _recall_seq_states
            if not self._bank_allows(bank_channel, channel, cc_num):
                _log("  --", f"{self._cc_label(cc_num, channel)} = {value}  (filtered by tags)")
                continue
            self._send_cc(cc_num, channel, value)
            self._set_dest(cc_num, channel, value)
            _log("  ->", f"{self._cc_label(cc_num, channel)} = {value}")

    def _recall_channeled(self, preset, bank_channel):
        """Recall a preset with CCs grouped by channel.

        *bank_channel* is the 0-based bank channel used for tag filtering.
        """
        for ch_str, ch_data in preset["channels"].items():
            ch = int(ch_str)  # YAML may store as string
            for cc_name, value in ch_data.get("cc_values", {}).items():
                cc_num = self._cc_name_to_number(cc_name)
                if cc_num is None:
                    continue
                if (ch, cc_num) in self.recall_ignore:
                    _log("  --", f"{self._cc_label(cc_num, ch)} = {value}  (ignored)")
                    continue
                if (ch, cc_num) in self.seq_ccs:
                    continue  # seq CCs are handled by _recall_seq_states
                if not self._bank_allows(bank_channel, ch, cc_num):
                    _log("  --", f"{self._cc_label(cc_num, ch)} = {value}  (filtered by tags)")
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

    def _boot_sync(self):
        """Populate missing destinations from defaults and send all to hardware.

        Called once after MIDI ports are opened so the hardware matches
        the service's internal state from the very first moment.
        """
        filled = 0
        for (ch, cc_num), default in self.dest_defaults.items():
            key = self._dest_key(cc_num, ch)
            if key not in self.destination_states:
                self.destination_states[key] = default
                filled += 1
        if filled:
            _log("BOOT", f"Filled {filled} destination(s) from defaults")
        self._sync_all_destinations()
        # Sync sequential step counters
        if self.seq_defs:
            # Fill defaults for any seq not yet in seq_states
            for name in self.seq_defs:
                if name not in self.seq_states:
                    self.seq_states[name] = 1
            for name, step in self.seq_states.items():
                defn = self.seq_defs.get(name)
                if defn:
                    self._sync_seq(name, defn, step)
            _log("BOOT", f"Synced {len(self.seq_states)} seq state(s)")

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

    # -- Sequential step counter sync ----------------------------------------

    _SEQ_PULSE_DELAY = 0.100  # fallback: seconds between all seq sync steps

    def _sync_seq(self, name, defn, target_step):
        """Send reset pulse + (target_step − 1) next pulses to hardware."""
        reset_cc = defn.get("reset_cc")
        reset_ch = defn.get("reset_ch")
        next_cc = defn.get("next_cc")
        next_ch = defn.get("next_ch")
        if reset_cc is None or reset_ch is None:
            _log("WARN", f"Seq '{name}': missing reset CC/channel — skipped")
            return
        if next_cc is None or next_ch is None:
            _log("WARN", f"Seq '{name}': missing next CC/channel — skipped")
            return
        delay = defn.get("delay", self._seq_delay)
        # Reset pulse
        self._send_cc(reset_cc, reset_ch, 127)
        time.sleep(delay)
        self._send_cc(reset_cc, reset_ch, 0)
        # Next pulses
        nexts = max(0, target_step - 1)
        for _ in range(nexts):
            time.sleep(delay)
            self._send_cc(next_cc, next_ch, 127)
            time.sleep(delay)
            self._send_cc(next_cc, next_ch, 0)
        _log("SEQ", f"'{name}' synced to step {target_step} "
             f"(reset + {nexts} next{'s' if nexts != 1 else ''})")

    def _recall_seq_states(self, saved_seqs):
        """Restore sequential step positions by pulsing reset + next CCs."""
        for name, target_step in saved_seqs.items():
            defn = self.seq_defs.get(name)
            if not defn:
                _log("WARN", f"Seq '{name}' not found in cc_mappings — skipped")
                continue
            target_step = int(target_step)
            if target_step < 1:
                target_step = 1
            self._sync_seq(name, defn, target_step)
            self.seq_states[name] = target_step
        self._mark_dirty()

    # -- Center-snap ("wiggle to center") ------------------------------------

    _CENTER_WINDOW = 2.0        # seconds: reversals must happen within this
    _CENTER_REVERSALS = 4       # direction changes needed to arm snap
    _CENTER_IDLE = 0.20         # seconds of no movement → snap fires
    _CENTER_CONSISTENT = 1.0    # seconds of single-direction → reset tracker
    _CENTER_VALUE = 63          # value to snap to

    def _update_center_tracker(self, dest_key, delta, target_cc, target_ch,
                               min_v, max_v, center_value=None):
        """Track encoder direction changes for a center-enabled destination.

        Called after each relative movement.  When 4+ direction reversals
        happen within 2 s and then the encoder stops, snaps to *center_value*
        (defaults to ``_CENTER_VALUE``).
        Consistent single-direction movement for 1 s resets the tracker.
        """
        if center_value is None:
            center_value = self._CENTER_VALUE
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
                                   min_v, max_v, center_value)

    def _restart_center_timer(self, dest_key, target_cc, target_ch,
                              min_v, max_v, center_value):
        """Cancel any pending idle timer and start a fresh one."""
        old = self._center_timers.pop(dest_key, None)
        if old is not None:
            old.cancel()
        t = threading.Timer(
            self._CENTER_IDLE,
            self._center_idle_fired,
            args=(dest_key, target_cc, target_ch, min_v, max_v, center_value),
        )
        t.daemon = True
        t.start()
        self._center_timers[dest_key] = t

    def _center_idle_fired(self, dest_key, target_cc, target_ch,
                           min_v, max_v, center_value):
        """Called from timer thread when encoder has been idle."""
        self._center_timers.pop(dest_key, None)
        tr = self._center_trackers.get(dest_key)
        if tr is None or not tr["armed"]:
            # Not armed — just a normal pause; reset tracker
            self._reset_center_tracker(dest_key)
            return

        # Snap to center
        center = max(min_v, min(max_v, center_value))
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

            # Sequential step counter actions (momentary trigger)
            if "seq_reset" in action or "seq_next" in action:
                seq_name = action.get("seq_reset") or action.get("seq_next")
                if msg.value > 0:
                    if "seq_reset" in action:
                        self.seq_states[seq_name] = 1
                        _log("SEQ", f"'{seq_name}' reset to 1")
                    else:
                        seq_max = action.get("seq_max",
                                             self.seq_defs.get(seq_name, {}).get("max", 4))
                        current = self.seq_states.get(seq_name, 1)
                        new_val = current + 1 if current < seq_max else 1
                        self.seq_states[seq_name] = new_val
                        _log("SEQ", f"'{seq_name}' -> {new_val}")
                    self._mark_dirty()
                # Forward the momentary CC to hardware
                target_cc = action.get("cc")
                if target_cc:
                    target_ch = (action["channel"] - 1 if "channel" in action
                                 else msg.channel)
                    self._send_cc(target_cc, target_ch, msg.value)
                continue

            # Determine target CC and channel
            target_cc = action["cc"]
            if target_cc == 0:
                continue  # CC 0 = no-pass; drop silently

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
            # center: true → snap to _CENTER_VALUE (63)
            # center: <int> → snap to that value
            center_cfg = action.get("center")
            if center_cfg and delta:
                center_val = (int(center_cfg) if isinstance(center_cfg, int)
                              else None)
                dest_key = self._dest_key(target_cc, target_ch)
                self._update_center_tracker(dest_key, delta,
                                            target_cc, target_ch,
                                            min_v, max_v, center_val)

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
            self.poly_states[key] = {"held": [], "active": [],
                                     "dist_rr": 0}
        return self.poly_states[key]

    def _poly_note_on(self, state, pitch, velocity, max_poly,
                      replace_priority, transpose, out_ch,
                      dist_slots=None):
        """Process note-on through polyphony limiter.

        Tracks notes by their original (pre-transpose) pitch, along with
        the transpose and output channel used when the note was activated.
        Returns a list of action tuples:
          ("on", pitch, velocity, reason, transpose, out_ch)
          ("off", pitch, reason, transpose, out_ch)
        where reason is one of: "new", "retrigger", "replace", "steal".

        When *dist_slots* is provided (a list of 0-based MIDI channels),
        each new voice is assigned to the next available slot in round-robin
        order.  The slot's channel overrides *out_ch*.
        """
        now = time.monotonic()

        if dist_slots:
            slot_ch = self._dist_alloc(state, dist_slots)
        else:
            slot_ch = out_ch

        note_info = {"pitch": pitch, "velocity": velocity, "timestamp": now,
                     "transpose": transpose, "out_ch": slot_ch}

        # Update held-notes list (store requested slot_ch for fallback reuse)
        state["held"] = [n for n in state["held"] if n["pitch"] != pitch]
        state["held"].append(note_info)

        results = []

        # Already active → retrigger on the SAME channel it's already on
        existing = next(
            (n for n in state["active"] if n["pitch"] == pitch), None)
        if existing is not None:
            reuse_ch = existing["out_ch"]
            note_info["out_ch"] = reuse_ch
            state["active"] = [n for n in state["active"]
                               if n["pitch"] != pitch]
            state["active"].append(note_info)
            results.append(("on", pitch, velocity, "retrigger",
                            transpose, reuse_ch))
            return results

        # Room available → just add
        if len(state["active"]) < max_poly:
            state["active"].append(note_info)
            results.append(("on", pitch, velocity, "new",
                            transpose, slot_ch))
            return results

        # Polyphony full → steal a voice (reuse stolen voice's channel slot)
        to_replace = self._select_replace(state["active"], replace_priority)
        if to_replace is not None:
            results.append(("off", to_replace["pitch"], "steal",
                            to_replace["transpose"], to_replace["out_ch"]))
            if dist_slots:
                # Reuse the freed channel slot instead of round-robin
                note_info["out_ch"] = to_replace["out_ch"]
            state["active"] = [n for n in state["active"]
                               if n["pitch"] != to_replace["pitch"]]
            state["active"].append(note_info)
            results.append(("on", pitch, velocity, "replace",
                            transpose, note_info["out_ch"]))

        return results

    def _dist_alloc(self, state, slots):
        """Pick the next distribution slot using round-robin.

        Prefers free slots (channels not currently in use by an active note).
        Falls back to pure round-robin when all slots are occupied.
        """
        active_channels = [n["out_ch"] for n in state["active"]]
        n = len(slots)
        start = state["dist_rr"] % n
        # First pass: find the next free slot from the round-robin cursor
        for i in range(n):
            idx = (start + i) % n
            ch = slots[idx]
            if ch not in active_channels:
                state["dist_rr"] = idx + 1
                return ch
        # All occupied — pure round-robin (voice stealing will free one)
        ch = slots[start]
        state["dist_rr"] = start + 1
        return ch

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
        # Fallback inherits the freed channel slot (for distribution)
        fallback = self._select_fallback(
            state["held"], state["active"], fallback_priority)
        if fallback is not None:
            fallback["out_ch"] = rel_out_ch
            state["active"].append(fallback)
            results.append(("on", fallback["pitch"],
                            fallback["velocity"], "fallback",
                            fallback.get("transpose", transpose),
                            rel_out_ch))

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

    def _process_poly_actions(self, actions, name, pool_label, state, max_poly):
        """Convert polyphony actions to MIDI messages with logging."""
        results = []
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
                    src = getattr(self, "_msg_source", "")
                    _log("NOTE", f"poly {action[3]} "
                         f"note={action[1]}→{out_note} "
                         f"ch{a_out_ch + 1} [{src}] "
                         f"vel={action[2]} "
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
                    src = getattr(self, "_msg_source", "")
                    _log("NOTE", f"poly {action[2]} "
                         f"note={action[1]}→{out_note} "
                         f"ch{a_out_ch + 1} [{src}] "
                         f"\"{name}\" "
                         f"[{pool_label} "
                         f"{len(state['active'])}/{max_poly}]")
        return results

    def _release_tracked_notes(self, pitch, origins):
        """Release notes using stored mapping info from the original note-on.

        Called when a note-off arrives and we have tracked origin info,
        ensuring the release reaches the correct mapping even if shift
        state changed while the key was held.
        """
        results = []
        for info in origins:
            if info["has_poly"]:
                state = self._get_poly_state(info["pool_key"])
                actions = self._poly_note_off(
                    state, pitch, info["fallback_pri"],
                    info["transpose"], info["out_ch"])
                pool_label = (f"'{info['inst_name']}'"
                              if info.get("inst_name")
                              else f"ch{info['out_ch'] + 1}")
                results.extend(self._process_poly_actions(
                    actions, info["name"], pool_label,
                    state, info["max_poly"]))
            else:
                out_note = pitch + info["transpose"]
                if 0 <= out_note <= 127:
                    results.append(mido.Message(
                        "note_off", note=out_note,
                        channel=info["out_ch"], velocity=0))
                    src = getattr(self, "_msg_source", "")
                    _log("NOTE", f"tracked release "
                         f"note={pitch}→{out_note} "
                         f"ch{info['out_ch'] + 1} [{src}] "
                         f"\"{info['name']}\"")
        return results

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

        # --- Note-off: use stored origin info when available ---
        # This ensures the release reaches the correct mapping even if
        # shift state changed while the key was held.
        if not is_note_on:
            stored = self.note_on_origins.pop((msg.channel, note), None)
            if stored is not None:
                return self._release_tracked_notes(note, stored)

        results = []
        matched = False
        origins = []  # track which mappings this note-on matched

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
            dist_slots = nm.get("_dist_slots")
            has_poly = ("max_polyphony" in nm or inst_name is not None
                        or dist_slots is not None)

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
                elif dist_slots is not None:
                    # Distribution without instance name — key by slot list
                    pool_key = ("_dist", tuple(dist_slots))
                    max_poly = nm["max_polyphony"]
                    replace_pri = nm.get("replace_priority", "lowest")
                    fallback_pri = nm.get(
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
                        transpose, out_ch, dist_slots=dist_slots)
                    # Track origin for later note-off routing
                    origins.append({
                        "has_poly": True,
                        "pool_key": pool_key,
                        "fallback_pri": fallback_pri,
                        "max_poly": max_poly,
                        "transpose": transpose,
                        "out_ch": out_ch,
                        "name": name,
                        "inst_name": inst_name,
                        "poly_to_chan": bool(
                            nm.get("after_poly_to_chan")),
                    })
                else:
                    actions = self._poly_note_off(
                        state, note, fallback_pri, transpose, out_ch)

                pool_label = (f"'{inst_name}'"
                              if inst_name else f"ch{out_ch + 1}")
                results.extend(self._process_poly_actions(
                    actions, name, pool_label, state, max_poly))
            else:
                # ---- Simple pass-through with optional transpose/channel ----
                out_note = note + transpose
                if out_note < 0 or out_note > 127:
                    src = getattr(self, "_msg_source", "")
                    _log("NOTE", f"note {note} ch{src_ch_1based} "
                         f"[{src}] "
                         f"transpose {transpose:+d} → {out_note} "
                         f"out of range, skipped \"{name}\"")
                    continue

                out_msg = msg.copy(note=out_note, channel=out_ch)
                results.append(out_msg)

                # Track origin for later note-off routing
                if is_note_on:
                    origins.append({
                        "has_poly": False,
                        "transpose": transpose,
                        "out_ch": out_ch,
                        "name": name,
                    })

                # Debug logging
                src = getattr(self, "_msg_source", "")
                changes = []
                if out_note != note:
                    changes.append(f"note {note}→{out_note}")
                if out_ch != msg.channel:
                    changes.append(f"ch{src_ch_1based}→{out_ch + 1}")
                detail = ", ".join(changes) if changes else "no change"
                line = (f"{msg.type} note={note} ch{src_ch_1based} "
                        f"[{src}] "
                        f"(shift=0x{self.shift_state:04X}) → "
                        f"{detail} \"{name}\"")
                _log("NOTE", line)

        # Store origin info so note-off can find the right mapping
        # even if shift state changes while the key is held.
        if is_note_on and origins:
            self.note_on_origins[(msg.channel, note)] = origins

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
        Rec×6 + note  → toggle write-protect on that note's preset
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
                _log("TRANS", "Rec×6 — note=toggle / Play=unlock / Stop=lock last preset")
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
            if self.intercept_mode and self.rec_counter == 6:
                self.rec_counter = 0
                self._toggle_write_protect(msg.channel, msg.note)
                return  # Intercepted
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

        # Polyphonic aftertouch — follow the note's routed channel
        if msg.type == "polytouch":
            origins = self.note_on_origins.get((msg.channel, msg.note))
            if origins:
                for info in origins:
                    if info["has_poly"]:
                        # Find the note's current output channel from active
                        state = self._get_poly_state(info["pool_key"])
                        active = next(
                            (n for n in state["active"]
                             if n["pitch"] == msg.note), None)
                        if active:
                            out_note = msg.note + active["transpose"]
                            if 0 <= out_note <= 127:
                                if info.get("poly_to_chan"):
                                    # Convert polytouch → channel aftertouch
                                    # on the distributed channel
                                    self._forward(mido.Message(
                                        "aftertouch",
                                        channel=active["out_ch"],
                                        value=msg.value))
                                else:
                                    self._forward(msg.copy(
                                        note=out_note,
                                        channel=active["out_ch"]))
                    else:
                        out_note = msg.note + info["transpose"]
                        if 0 <= out_note <= 127:
                            self._forward(msg.copy(
                                note=out_note,
                                channel=info["out_ch"]))
                return
            self._forward(msg)
            return

        # Program Change — forward to hardware
        if msg.type == "program_change":
            self._forward(msg)
            return

        # Suppress raw channel aftertouch for P2P channels — the extra
        # input callback handles these via polytouch with floor/slew.
        # Without this guard, aftertouch arriving on the primary input
        # (IAC bus, DAW echo) would bypass P2P and hit the synth raw.
        if (msg.type == "aftertouch"
                and hasattr(msg, "channel")
                and msg.channel in self.p2p_channels):
            _log("P2P", f"BLOCKED raw aftertouch ch{msg.channel+1} "
                 f"val={msg.value} from {getattr(self, '_msg_source', '?')}")
            return

        # All other events (pitch bend, channel aftertouch, etc.) — forward unchanged
        if msg.type == "aftertouch":
            _log("P2P", f"FWD raw aftertouch ch{msg.channel+1} "
                 f"val={msg.value} from {getattr(self, '_msg_source', '?')}")
        self._forward(msg)

    def _forward(self, msg):
        """Forward a message to the hardware output (proxy mode only)."""
        if self.routing and self.midi_out:
            self.midi_out.send(msg)

    # -- Extra input helpers ---------------------------------------------------

    @staticmethod
    def _build_velocity_table(floor_vel, low_in, high_in, curve_exp):
        """Build a 128-entry velocity lookup table.

        - Index 0 stays 0 (velocity 0 = note-off semantics).
        - [1, low_in)  → floor_vel  (clamp quiet playing to the floor).
        - [low_in, high_in] → [floor_vel, high_in] via power curve.
        - (high_in, 127] → high_in  (hard ceiling).
        """
        table = [0] * 128
        for v in range(1, 128):
            if v <= low_in:
                table[v] = floor_vel
            elif v <= high_in:
                t = (v - low_in) / (high_in - low_in)
                out = floor_vel + (high_in - floor_vel) * (t ** curve_exp)
                table[v] = max(floor_vel, min(high_in, int(round(out))))
            else:
                # Hard ceiling — anything above high_in clamps
                table[v] = high_in
        return table

    @staticmethod
    def _parse_channel_opts(inp_cfg, ch_set):
        """Build per-channel option dicts from device-level + per_channel overrides.

        Returns a dict keyed by 0-based channel number.  Each value is a dict
        with keys ``vtable``, ``ptable``, ``p2p`` (bool),
        ``decay_s`` (float), ``shelf`` (int), ``shelf_top`` (int),
        ``slew_up`` (float, steps/sec), ``slew_down`` (float,
        steps/sec), ``debounce_s`` (float).  ``ptable`` is an
        optional 128-entry lookup table for pressure values (same
        format as ``vtable``).  Channels without any options are
        omitted.

        Device-level ``velocity_curve``, ``pressure_curve``,
        ``pressure_to_poly``, ``pressure_decay``,
        ``pressure_shelf``, ``pressure_shelf_top``,
        ``slew_up``, ``slew_down`` and
        ``note_debounce`` act as defaults; ``per_channel`` entries
        (keyed by 1-based channel number in the YAML) override
        them.
        """
        build_vt = MidiPresetService._build_velocity_table

        def _parse_vc(vc):
            if not vc:
                return None
            return build_vt(
                int(vc.get("floor", 1)),
                int(vc.get("low", 1)),
                int(vc.get("high", 127)),
                float(vc.get("curve", 1.0)))

        # --- device-level defaults ---
        default_vc = inp_cfg.get("velocity_curve")
        default_vtable = _parse_vc(default_vc)
        default_pc = inp_cfg.get("pressure_curve")
        default_ptable = _parse_vc(default_pc)

        p2p_raw = inp_cfg.get("pressure_to_poly")
        if p2p_raw is True:
            default_p2p_chs = ch_set if ch_set is not None else set(range(16))
        elif p2p_raw:
            default_p2p_chs = {int(c) - 1 for c in p2p_raw}
        else:
            default_p2p_chs = set()

        default_decay_s = float(inp_cfg.get("pressure_decay", 0)) / 1000.0
        # Shelf: absolute MIDI value (0–127) for initial decay target.
        # Decay settles at the shelf until the player's pressure
        # reaches (or exceeds) the shelf level, which "unlocks" the
        # floor.  After unlock, the output can drop to pressure_floor.
        # 0 = no shelf (decay goes straight to floor/sustain).
        default_shelf = int(inp_cfg.get("pressure_shelf", 0))
        # Shelf top: values between shelf and shelf_top are clamped to
        # shelf (dead zone).  Values above shelf_top are rescaled to
        # cover [shelf, high].  0 = no dead zone (shelf_top == shelf).
        default_shelf_top = int(inp_cfg.get("pressure_shelf_top", 0))
        # Slew rates in steps/sec.  0 = unlimited (no limiting).
        # Legacy behaviour: when omitted, slew_down defaults to
        # 127/decay_s (matching the old symmetric rate) and slew_up
        # defaults to 0 (unlimited) so pressing harder responds
        # instantly.
        default_slew_up = float(inp_cfg.get("slew_up", 0))
        default_slew_down = float(inp_cfg.get("slew_down", 0))
        default_debounce_s = float(
            inp_cfg.get("note_debounce", 0)) / 1000.0

        # Determine every channel that needs an entry
        active_chs = ch_set if ch_set is not None else set(range(16))

        per_ch_yaml = inp_cfg.get("per_channel", {})

        opts = {}  # 0-based ch → {vtable, p2p, decay_s}
        for ch0 in active_chs:
            ch1 = ch0 + 1
            override = per_ch_yaml.get(ch1, {}) if per_ch_yaml else {}

            # Velocity curve: per-channel override wins
            if "velocity_curve" in override:
                vtable = _parse_vc(override["velocity_curve"])
            else:
                vtable = default_vtable

            # Pressure curve: per-channel override wins
            if "pressure_curve" in override:
                ptable = _parse_vc(override["pressure_curve"])
            else:
                ptable = default_ptable

            # Pressure-to-poly
            p2p_over = override.get("pressure_to_poly")
            if p2p_over is not None:
                p2p = bool(p2p_over)
            else:
                p2p = ch0 in default_p2p_chs

            # Decay
            if "pressure_decay" in override:
                decay_s = float(override["pressure_decay"]) / 1000.0
            else:
                decay_s = default_decay_s

            # Shelf
            if "pressure_shelf" in override:
                shelf = int(override["pressure_shelf"])
            else:
                shelf = default_shelf
            if "pressure_shelf_top" in override:
                shelf_top = int(override["pressure_shelf_top"])
            else:
                shelf_top = default_shelf_top

            # Slew rates (steps/sec)
            if "slew_up" in override:
                slew_up = float(override["slew_up"])
            else:
                slew_up = default_slew_up
            if "slew_down" in override:
                slew_down = float(override["slew_down"])
            else:
                slew_down = default_slew_down

            # Note debounce
            if "note_debounce" in override:
                debounce_s = float(override["note_debounce"]) / 1000.0
            else:
                debounce_s = default_debounce_s

            opts[ch0] = {"vtable": vtable, "ptable": ptable,
                         "p2p": p2p,
                         "decay_s": decay_s,
                         "shelf": shelf,
                         "shelf_top": shelf_top,
                         "slew_up": slew_up,
                         "slew_down": slew_down,
                         "debounce_s": debounce_s}

        return opts

    def _open_extra_input(self, inp_cfg, msg_queue):
        """Open one extra input device with channel filtering into *msg_queue*."""
        device = inp_cfg.get("device", "")
        if not device:
            _log("WARN", "Input entry missing 'device' — skipped")
            return
        channels = inp_cfg.get("channels", [])
        # channels are 1-based in config; convert to 0-based set (empty = all)
        ch_set = {c - 1 for c in channels} if channels else None

        # Build per-channel options (device defaults + per_channel overrides)
        ch_opts = self._parse_channel_opts(inp_cfg, ch_set)

        # Quick lookups for the callback
        any_p2p = any(o["p2p"] for o in ch_opts.values())

        # Register P2P channels so the main handler can suppress raw
        # channel aftertouch that leaks through the primary input
        # (IAC bus, DAW echo, etc.) — otherwise it would bypass the
        # floor / slew and hit the synth unprocessed.
        self.p2p_channels.update(
            ch0 for ch0, o in ch_opts.items() if o["p2p"])

        def make_cb(ch_filter, channel_opts, has_p2p, source):
            # Per-device note tracker:
            #   active[ch][note] = {"vel", "started", "start_t",
            #                       "last_out", "last_out_t",
            #                       "last_sent", "last_pressure"}
            # On the first aftertouch message after note-on, the note
            # becomes "started" and the decay floor / slew limiter
            # begin from that moment.  A background tick thread
            # re-evaluates decay/slew every ~15 ms so the output
            # keeps updating even when no physical pressure changes
            # arrive.
            #
            # Note debounce (note_debounce config, ms):
            #   pending_off[ch][note] = {"off_msg", "expire_t", "info"}
            # When a note-off arrives too soon after note-on, the note
            # stays in active and the off is deferred.  If a re-strike
            # arrives before the timer expires, the pending off is
            # cancelled (no audible gap).  Expired entries are flushed
            # on every callback invocation.
            active = {} if has_p2p else None
            pending_off = {} if has_p2p else None
            p2p_lock = threading.Lock() if has_p2p else None

            def _put(m):
                msg_queue.put((source, m))

            def _flush_pending(now):
                """Release any debounced note-offs whose timer expired."""
                if not pending_off:
                    return
                for pch in list(pending_off):
                    pnotes = pending_off[pch]
                    for pn in list(pnotes):
                        pend = pnotes[pn]
                        if now >= pend["expire_t"]:
                            info = active.get(pch, {}).pop(pn, None)
                            if info is not None and info["started"]:
                                _put(mido.Message(
                                    "polytouch", channel=pch,
                                    note=pn, value=0))
                            _put(pend["off_msg"])
                            del pnotes[pn]
                    if not pnotes:
                        del pending_off[pch]

            def _p2p_calc(ch, note, info, pressure, copts, now,
                          is_tick=False):
                """Compute decay floor + slew and send polytouch.

                *pressure* must already be ptable-transformed.
                Caller must hold p2p_lock.
                """
                decay_s = copts.get("decay_s", 0)
                slew_up = copts.get("slew_up", 0)
                slew_down = copts.get("slew_down", 0)
                ptable = copts.get("ptable")
                if (decay_s > 0
                        and slew_up == 0 and slew_down == 0):
                    slew_down = 127.0 / decay_s

                vel = info["vel"]
                is_start = False
                if not info["started"]:
                    info["started"] = True
                    info["start_t"] = now
                    info["last_out"] = float(vel)
                    info["last_out_t"] = now
                    is_start = True

                # Shelf dead zone + rescale: values between shelf
                # and shelf_top clamp to shelf; values above
                # shelf_top are rescaled to [shelf, high].
                shelf = copts.get("shelf", 0)
                shelf_top = copts.get("shelf_top", 0)
                if (shelf_top > shelf > 0
                        and pressure >= shelf):
                    high_out = (ptable[127]
                                if ptable is not None else 127)
                    if pressure < shelf_top:
                        pressure = shelf
                    elif shelf_top < high_out:
                        # Rescale [shelf_top, high] → [shelf, high]
                        t = ((pressure - shelf_top)
                             / (high_out - shelf_top))
                        pressure = int(round(
                            shelf + (high_out - shelf) * t))

                target = float(pressure)
                floor_v = 0.0
                if decay_s > 0:
                    elapsed_s = now - info["start_t"]
                    frac = min(1.0, elapsed_s / decay_s)
                    # Decay target: pressure floor (from pressure
                    # curve).  This is the absolute minimum the
                    # decay can settle to (ptable[1] = the
                    # pressure curve's floor output value).
                    pfloor = (float(ptable[1])
                              if ptable is not None else 0.0)
                    decay_target = pfloor
                    # Shelf: decay settles at shelf (above floor)
                    # until the player's pressure reaches the
                    # shelf level, which "unlocks" the floor.
                    if shelf > 0:
                        if (not info["shelf_unlocked"]
                                and pressure >= shelf):
                            info["shelf_unlocked"] = True
                        if not info["shelf_unlocked"]:
                            decay_target = max(
                                decay_target, float(shelf))
                    floor_v = (decay_target
                               + (vel - decay_target)
                               * (1.0 - frac))
                    target = max(target, floor_v)

                pre_slew = target
                # Asymmetric slew rate limiter
                elapsed_o = now - info["last_out_t"]
                prev = info["last_out"]
                slew_tag = ""
                if target > prev and slew_up > 0:
                    max_up = slew_up * elapsed_o
                    target = min(target, prev + max_up)
                    slew_tag = (
                        f" slew_up({prev:.1f}"
                        f"+{max_up:.2f}"
                        f"→{target:.1f})")
                elif target < prev and slew_down > 0:
                    max_dn = slew_down * elapsed_o
                    target = max(target, prev - max_dn)
                    slew_tag = (
                        f" slew_dn({prev:.1f}"
                        f"-{max_dn:.2f}"
                        f"→{target:.1f})")
                info["last_out"] = target
                info["last_out_t"] = now
                value = max(0, min(127, int(round(target))))
                sent = ""
                if value != info["last_sent"]:
                    info["last_sent"] = value
                    _put(mido.Message(
                        "polytouch", channel=ch,
                        note=note, value=value))
                    sent = " SEND"
                if is_start:
                    _log("P2P",
                         f"ch{ch+1}/n{note} "
                         f"vel={vel} START "
                         f"pres={pressure}")
                elif not is_tick and (
                        sent or int(pre_slew) != value):
                    _log("P2P",
                         f"ch{ch+1}/n{note} "
                         f"p={pressure} "
                         f"fl={floor_v:.0f} "
                         f"tgt={pre_slew:.0f}"
                         f"{slew_tag} "
                         f"→{value}"
                         f"{sent}")

            # Background tick thread: re-evaluate decay/slew for
            # active notes so output updates even when no physical
            # pressure messages arrive.
            def _p2p_tick_loop():
                while not self._state_stop.is_set():
                    if active:
                        with p2p_lock:
                            now = time.monotonic()
                            _flush_pending(now)
                            for ch in list(active):
                                ch_notes = active[ch]
                                copts = channel_opts.get(ch, {})
                                for note in list(ch_notes):
                                    info = ch_notes[note]
                                    _p2p_calc(
                                        ch, note, info,
                                        info["last_pressure"],
                                        copts, now,
                                        is_tick=True)
                    self._state_stop.wait(0.015)

            if has_p2p:
                tick_t = threading.Thread(
                    target=_p2p_tick_loop, daemon=True)
                tick_t.start()

            def cb(msg):
                if (ch_filter is not None
                        and hasattr(msg, "channel")
                        and msg.channel not in ch_filter):
                    return

                ch = msg.channel if hasattr(msg, "channel") else None
                copts = channel_opts.get(ch, {}) if ch is not None else {}

                # Velocity curve (per-channel)
                vtable = copts.get("vtable")
                if vtable is not None and hasattr(msg, "velocity"):
                    msg = msg.copy(velocity=vtable[msg.velocity])

                # Track notes for pressure-to-poly conversion
                if active is not None and ch is not None:
                    with p2p_lock:
                        _flush_pending(time.monotonic())
                        debounce_s = copts.get("debounce_s", 0)
                        if msg.type == "note_on" and msg.velocity > 0:
                            pend = (pending_off.get(ch, {})
                                    .pop(msg.note, None))
                            if pend is not None:
                                return
                            now_on = time.monotonic()
                            active.setdefault(ch, {})[msg.note] = {
                                "vel": msg.velocity,
                                "started": True,
                                "start_t": now_on,
                                "last_out": float(msg.velocity),
                                "last_out_t": now_on,
                                "last_sent": msg.velocity,
                                "last_pressure": 0,
                                "shelf_unlocked": False}
                            _log("P2P", f"ch{ch+1}/n{msg.note} "
                                 f"vel={msg.velocity} TRACK")
                        elif (msg.type == "note_off"
                              or (msg.type == "note_on"
                                  and msg.velocity == 0)):
                            if (debounce_s > 0
                                    and msg.note
                                    in active.get(ch, {})):
                                info = active[ch][msg.note]
                                age = (time.monotonic()
                                       - info["start_t"])
                                if (info["started"]
                                        and age < debounce_s):
                                    pending_off.setdefault(
                                        ch, {})[msg.note] = {
                                        "off_msg": msg,
                                        "expire_t": (
                                            info["start_t"]
                                            + debounce_s)}
                                    return
                            info = (active.get(ch, {})
                                    .pop(msg.note, None))
                            if (info is not None
                                    and info["started"]):
                                _log("P2P",
                                     f"ch{ch+1}/n{msg.note} "
                                     f"vel={info['vel']} OFF "
                                     f"last_out="
                                     f"{info['last_out']:.1f}")
                                _put(mido.Message(
                                    "polytouch", channel=ch,
                                    note=msg.note, value=0))
                        elif (msg.type in (
                                  "aftertouch", "polytouch")
                              and copts.get("p2p")):
                            ptable = copts.get("ptable")
                            ch_notes = active.get(ch, {})
                            if msg.type == "polytouch":
                                info = ch_notes.get(msg.note)
                                note_list = (
                                    [(msg.note, info, msg.value)]
                                    if info else [])
                            else:
                                note_list = [
                                    (n, inf, msg.value)
                                    for n, inf in
                                    ch_notes.items()]
                            if note_list:
                                now = time.monotonic()
                                for note, info, pressure \
                                        in note_list:
                                    if ptable is not None:
                                        pressure = (
                                            ptable[pressure])
                                    info["last_pressure"] = (
                                        pressure)
                                    _p2p_calc(
                                        ch, note, info,
                                        pressure, copts, now)
                            return  # suppress original aftertouch
                _put(msg)
            return cb

        try:
            port = mido.open_input(
                device,
                callback=make_cb(ch_set, ch_opts, any_p2p, device))
            self.extra_inputs.append(port)
            ch_str = ", ".join(str(c) for c in channels) if channels else "all"
            extras = []
            # Log per-channel overrides
            per_ch_yaml = inp_cfg.get("per_channel", {})
            # Device-level velocity curve (shown only when no per-channel)
            vc = inp_cfg.get("velocity_curve")
            if vc and not per_ch_yaml:
                extras.append(
                    f"vel_curve(floor={vc.get('floor', 1)} "
                    f"low={vc.get('low', 1)} "
                    f"high={vc.get('high', 127)} "
                    f"curve={vc.get('curve', 1.0)})")
            # Pressure-to-poly summary
            p2p_chs = sorted(ch0 + 1 for ch0, o in ch_opts.items()
                             if o["p2p"])
            if p2p_chs:
                if set(p2p_chs) == set(channels or range(1, 17)):
                    p2p_label = "all"
                else:
                    p2p_label = "ch " + ",".join(str(c) for c in p2p_chs)
                # Collect unique decay values across p2p channels
                decay_vals = sorted({ch_opts[c - 1]["decay_s"]
                                     for c in p2p_chs})
                if len(decay_vals) == 1 and decay_vals[0] > 0:
                    decay_tag = f" decay={int(decay_vals[0]*1000)}ms"
                elif any(d > 0 for d in decay_vals):
                    decay_tag = " decay=per-ch"
                else:
                    decay_tag = ""
                # Collect unique shelf values across p2p channels
                sh_vals = sorted({ch_opts[c - 1]["shelf"]
                                  for c in p2p_chs})
                st_vals = sorted({ch_opts[c - 1]["shelf_top"]
                                  for c in p2p_chs})
                if len(sh_vals) == 1 and sh_vals[0] > 0:
                    sh_label = str(sh_vals[0])
                    if (len(st_vals) == 1
                            and st_vals[0] > sh_vals[0]):
                        sh_label += f"-{st_vals[0]}"
                    shelf_tag = f" shelf={sh_label}"
                elif any(v > 0 for v in sh_vals):
                    shelf_tag = " shelf=per-ch"
                else:
                    shelf_tag = ""
                # Collect unique slew values across p2p channels
                su_vals = sorted({ch_opts[c - 1]["slew_up"]
                                  for c in p2p_chs})
                sd_vals = sorted({ch_opts[c - 1]["slew_down"]
                                  for c in p2p_chs})
                slew_parts = []
                if len(su_vals) == 1 and su_vals[0] > 0:
                    slew_parts.append(
                        f"up={int(su_vals[0])}")
                elif any(v > 0 for v in su_vals):
                    slew_parts.append("up=per-ch")
                if len(sd_vals) == 1 and sd_vals[0] > 0:
                    slew_parts.append(
                        f"dn={int(sd_vals[0])}")
                elif any(v > 0 for v in sd_vals):
                    slew_parts.append("dn=per-ch")
                slew_tag = (" slew="
                            + ",".join(slew_parts)
                            if slew_parts else "")
                # Collect unique debounce values across p2p channels
                db_vals = sorted({ch_opts[c - 1]["debounce_s"]
                                  for c in p2p_chs})
                if len(db_vals) == 1 and db_vals[0] > 0:
                    db_tag = (f" debounce="
                              f"{int(db_vals[0]*1000)}ms")
                elif any(d > 0 for d in db_vals):
                    db_tag = " debounce=per-ch"
                else:
                    db_tag = ""
                # Pressure curve summary
                pc = inp_cfg.get("pressure_curve")
                pc_tag = ""
                if pc:
                    pc_tag = (f" pcurve(fl={pc.get('floor', 1)}"
                              f" hi={pc.get('high', 127)}"
                              f" c={pc.get('curve', 1.0)})")
                extras.append(f"pressure_to_poly({p2p_label}"
                              f"{decay_tag}"
                              f"{shelf_tag}{slew_tag}"
                              f"{pc_tag}{db_tag})")
            # Per-channel overrides summary
            if per_ch_yaml:
                per_parts = []
                for ch1 in sorted(per_ch_yaml):
                    over = per_ch_yaml[ch1]
                    tags = []
                    if "velocity_curve" in over:
                        ovc = over["velocity_curve"]
                        tags.append(
                            f"vel(f={ovc.get('floor', 1)} "
                            f"c={ovc.get('curve', 1.0)})")
                    if "pressure_to_poly" in over:
                        tags.append(
                            "p2p" if over["pressure_to_poly"] else "!p2p")
                    if "pressure_decay" in over:
                        tags.append(f"decay={over['pressure_decay']}ms")
                    if "pressure_curve" in over:
                        opc = over["pressure_curve"]
                        tags.append(
                            f"pcurve(c={opc.get('curve', 1.0)})")
                    if "pressure_shelf" in over:
                        sh_lbl = str(over["pressure_shelf"])
                        if "pressure_shelf_top" in over:
                            sh_lbl += (
                                f"-{over['pressure_shelf_top']}")
                        tags.append(f"shelf={sh_lbl}")
                    if "slew_up" in over:
                        tags.append(
                            f"slew_up={over['slew_up']}")
                    if "slew_down" in over:
                        tags.append(
                            f"slew_dn={over['slew_down']}")
                    if "note_debounce" in over:
                        tags.append(
                            f"debounce={over['note_debounce']}ms")
                    if tags:
                        per_parts.append(f"ch{ch1}:{','.join(tags)}")
                if per_parts:
                    extras.append(f"per_ch[{'; '.join(per_parts)}]")
            suffix = f" {' '.join(extras)}" if extras else ""
            _log("OPEN", f"Input device : {device} (ch {ch_str}){suffix}")
        except OSError as exc:
            _log("WARN", f"Could not open input '{device}': {exc}")

    # -- Run loop -------------------------------------------------------------

    def run(self):
        port_name = self.config.get("port_name", "MIDI SX Presets")
        dest_count = len(self.destinations_map)
        cc_count = sum(len(s) for s in self.cc_sets.values())

        _log("INIT", "MIDI SysEx Preset Manager")
        _log("INIT", f"Config dir   : {self.config_dir}")
        if self.routing:
            _log("INIT", f"Mode         : proxy")
            iac_status = "(disabled)" if self.no_iac_input else self.routing['iac_input']
            _log("INIT", f"IAC input    : {iac_status}")
            _log("INIT", f"IAC return   : {self.routing['iac_return']}")
            _log("INIT", f"Hardware out : {self.routing['hardware_output']}")
            extra_cfgs = self.routing.get("inputs", [])
            if extra_cfgs and not self.no_input:
                for inp in extra_cfgs:
                    ch_list = inp.get("channels", [])
                    ch_str = ", ".join(str(c) for c in ch_list) if ch_list else "all"
                    _log("INIT", f"Input        : {inp.get('device', '?')} (ch {ch_str})")
            elif self.no_input and extra_cfgs:
                _log("INIT", f"Inputs       : {len(extra_cfgs)} configured (disabled)")
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
        if self.seq_defs:
            _log("INIT", f"Seq counters : {len(self.seq_defs)} defined "
                 f"(global delay: {self._seq_delay}s)")
            for sname, sdef in self.seq_defs.items():
                delay_str = f", delay={sdef['delay']}s" if "delay" in sdef else ""
                _log("INIT", f"  '{sname}': max={sdef.get('max', '?')}, "
                     f"reset=CC{sdef.get('reset_cc', '?')}/ch{(sdef.get('reset_ch', 0) or 0) + 1}, "
                     f"next=CC{sdef.get('next_cc', '?')}/ch{(sdef.get('next_ch', 0) or 0) + 1}"
                     f"{delay_str}")
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
                # -- Proxy mode: queue-based multi-input ----------------------
                msg_queue = queue.Queue()

                # IAC input (primary)
                iac_name = self.routing["iac_input"]
                if not self.no_iac_input:
                    self.midi_in = mido.open_input(
                        iac_name,
                        callback=lambda m, s=iac_name: msg_queue.put(
                            (s, m)))

                self.midi_out = mido.open_output(self.routing["hardware_output"])
                self.midi_return = mido.open_output(self.routing["iac_return"])

                # Extra input devices (channel-filtered)
                if not self.no_input:
                    for inp_cfg in self.routing.get("inputs", []):
                        self._open_extra_input(inp_cfg, msg_queue)

                if not self.midi_in and not self.extra_inputs:
                    _log("WARN", "All inputs disabled — no MIDI source")

                # Send saved/default state to hardware so it matches immediately
                self._boot_sync()

                # Unified message loop — all inputs feed the queue
                # Each item is a (source_device_name, msg) tuple.
                while True:
                    try:
                        source, msg = msg_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    self._msg_source = source
                    self._handle_message(msg)
            else:
                # -- Standalone mode ------------------------------------------
                self.midi_in = mido.open_input(port_name, virtual=True)
                self.midi_out = mido.open_output(port_name, virtual=True)
                self.midi_return = self.midi_out
                self._msg_source = port_name

                self._boot_sync()

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
            for port in self.extra_inputs:
                port.close()
            self.extra_inputs.clear()
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
    parser.add_argument(
        "--no-input",
        action="store_true",
        help="Disable extra input devices (routing.inputs).",
    )
    parser.add_argument(
        "--no-iac-input",
        action="store_true",
        help="Disable IAC input (routing.iac_input).",
    )
    args = parser.parse_args()

    if args.list_ports:
        list_ports()
        sys.exit(0)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    service = MidiPresetService(config_dir=args.config_dir,
                                no_input=args.no_input,
                                no_iac_input=args.no_iac_input)
    service.run()


if __name__ == "__main__":
    main()
