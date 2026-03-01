# Configuration

All config files live in a single directory (default `~/.midi-sx-presets`, override with `--config-dir`).

---

## config.yaml

Top-level service settings.

### Basic Settings

```yaml
port_name: MIDI SX Presets      # Virtual port name (standalone mode only)
device_id: 0x01                 # SysEx device ID (0x00–0x7F)
manufacturer_id: 0x7D           # SysEx manufacturer ID
seq_delay: 0.05                 # Seconds between sequential counter pulses
state_save_interval: 5          # Seconds between periodic state file writes
```

### Routing (Proxy Mode)

When `routing` is present, the service runs in proxy mode instead of standalone.

```yaml
routing:
  iac_input: "IAC Driver Bus 4 - Output"     # From DAW/MainStage
  iac_return: "IAC Driver Bus 5 - Return"    # Back to DAW (preset names, commands)
  hardware_output: "UMC404HD 192k"           # To hardware synth/mixer

  inputs:                                     # Extra hardware input devices
    - device: "Launchpad Pro MK3 LPProMK3 MIDI"
      channels: [1, 2, 4, 10]                # 1-based; empty = all channels
      # See "Extra Input Options" below
```

### Extra Input Options

Each entry under `routing.inputs` supports:

| Key | Type | Description |
|---|---|---|
| `device` | string | MIDI port name (required). |
| `channels` | list[int] | 1-based channel filter. Omit for all channels. |
| `velocity_curve` | dict | Remap velocity values (see below). |
| `pressure_curve` | dict | Remap pressure/aftertouch values (same format as velocity_curve). |
| `pressure_to_poly` | list[int] or bool | Channels (1-based) to convert channel aftertouch → polyphonic aftertouch. `true` = all configured channels. |
| `velocity_decay` | int | Milliseconds for the pressure floor to decay from velocity down to the shelf level after note-on (or to the pressure curve floor if no shelf is configured). |
| `pressure_shelf` | int | MIDI value (0–127). Decay holds at this level until player pressure reaches it. |
| `pressure_shelf_top` | int | Values between `shelf` and `shelf_top` clamp to shelf (dead zone). Values above are rescaled. |
| `pressure_slew_up` | float | Max steps/sec for upward pressure changes (0 = unlimited). |
| `pressure_slew_down` | float | Max steps/sec for downward pressure changes (0 = unlimited). |
| `pressure_start` | `"velocity"` or int | Initial pressure value on note-on. `"velocity"` uses the note's velocity. |
| `pressure_start_decay` | int | Milliseconds for pressure_start decay (when pressure_start is numeric). |
| `note_debounce` | int | Minimum note lifetime in ms before release is forwarded. Short releases are deferred. |
| `velocity_destination` | dict | Send velocity as a CC: `{cc: <int>, channel: "source"/"target"/<int>}`. |
| `velocity_shelf` | int | Floor value for velocity-destination CC decay. |
| `replace_note_velocity` | int | Replace note-on velocity with a fixed value (used with velocity_destination). |
| `per_channel` | dict | Per-channel overrides (keyed by 1-based channel number). Each can override any of the above. |

#### Velocity/Pressure Curve Format

```yaml
velocity_curve:
  floor: 30       # Minimum output velocity (inputs [1, low) clamp here)
  low: 50         # Input value that maps to floor
  cap: 99         # Maximum output velocity
  high: 127       # Input value that maps to cap (default 127; inputs above high clamp to cap)
  curve: 0.5      # Power curve exponent (< 1 = more sensitive, > 1 = less)
```

The same format applies to `pressure_curve`.

### Bank Tags

Filter which destination parameters are included in saves/recalls per preset bank (channel).

```yaml
bank_tags:
  1: [mono]        # Channel 1 presets only save/recall parameters tagged "mono"
  2: [poly]        # Channel 2 presets only save/recall parameters tagged "poly"
```

Banks not listed default to `["default"]` — only parameters with the implicit "default" tag are included.

### Recall Ignore

Parameters to skip during preset recall (useful for parameters you always want to control live).

```yaml
recall_ignore:
  - ns_to_perf_filter          # By destination name
  - {channel: 1, cc: 7}       # By channel (1-based) and CC number
```

---

## cc_sets.yaml

Maps CC numbers to human-readable names. Two formats:

### Flat Format (Single Set)

```yaml
cc_sets:
  1: modulation
  7: volume
  10: {name: pan, default: 64}
```

### Named Sets (Multi-Destination)

```yaml
cc_sets:
  mix_ccs:
    3:  {name: to_duck_out, default: 0}
    7:  {name: to_perf_filter, default: 0}
    10: {name: pan, default: 63}
    102: {name: to_loop_a, default: 0, tags: [looper]}

  polysynth_ccs:
    1:  {name: vcf_cutoff, default: 63}
    72: {name: vcf_release, default: 32}
```

Each entry can be a plain string (name only) or a dict with:

| Key | Description |
|---|---|
| `name` | CC name (required in dict form). |
| `default` | Default value used to initialize destination state. |
| `tags` | List of tag strings. Per-entry tags replace group-level tags. |

Group-level tags apply to all entries in the set that don't have their own `tags`:

```yaml
cc_sets:
  loop_ccs:
    tags: [looper]              # All entries inherit this tag
    1: {name: loop_a_speed}     # Gets tag "looper"
    71: {name: poly_morph, default: 63}  # Also gets "looper" (no explicit tags)
```

Entries with no tags at all (and no group-level tags) get the implicit `["default"]` tag.

---

## destinations.yaml

Maps logical destinations to MIDI channels and CC sets. Each destination has a prefix (prepended to inherited names) and a list of channels.

```yaml
poly:
  prefix: poly
  channels:
    2:
      cc_group: polysynth_ccs     # Inherit names from this cc_set
    11:
      prefix: perf                # Override the destination prefix for this channel
      tags: [poly]                # Channel-level tags (override cc_set tags for non-explicit entries)
      cc_group: mix_ccs

mono:
  prefix: mono
  channels:
    1:
      tags: [mono]
      cc_group: mix_ccs
    5:
      prefix: bass
      tags: [mono]
      cc_group: mix_ccs
      to_duck_out: {default: 110}   # Override default for a specific CC (by name)
```

### Resolution

For each channel entry:
1. All CC names from the `cc_group` are inherited, prefixed with `<prefix>_` (e.g. `poly_vcf_cutoff`).
2. Integer keys or cc_group name keys override specific CCs (overrides are NOT prefixed).
3. The flat maps `(channel, cc_num) → name` and `name → (channel, cc_num)` are built with conflict detection.
4. Defaults and tags propagate from cc_sets through to the resolved destination map.

---

## state.yaml

Automatically written by the service. Contains:

```yaml
destination_states:           # Current CC values: "cc_channel" → value
  '1_1': 17
  '7_1': 110
seq_states:                   # Sequential counter positions
  MonoVoice: 2
preset_cursor:                # Last-navigated note per channel bank
  0: 50
  1: 48
intercept_mode: true          # Transport mode
shift_state: 1                # Current shift bitmask
last_preset:                  # Most recent save/recall
  - 0
  - 50
```

State is saved periodically (default every 5s) and on shutdown. On startup, state is restored so a restart feels seamless.
