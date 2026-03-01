# CC Mapping Engine

Defined in `cc_mappings.yaml`. The mapping engine processes every incoming CC through a pipeline:

1. **Shift CCs** — update the 16-bit shift bitmask (consumed, not forwarded).
2. **MIDI Reset CCs** (121, 123) — re-send all destination values to hardware.
3. **Joystick CCs** — update held/latch shift bits (consumed, not forwarded).
4. **Mapped CCs** — ALL matching actions execute in order.
5. **Unmapped CCs** — silently dropped (not forwarded).

CCs are never forwarded raw. Only mapped outputs are sent.

---

## Shift Definitions

```yaml
shift_definitions:
  - cc: 80
    threshold: 2
    bit: 0          # Controls bit 0 of shift_state
  - cc: 81
    threshold: 2
    bit: 1          # Controls bit 1
```

When `cc_value > threshold`, the bit is set; otherwise cleared. Shift CCs are consumed (never forwarded).

## Joystick Definitions

```yaml
joystick_definitions:
  - negative_cc: 2
    positive_cc: 3
    threshold: 63
    held_negative_bit: 2
    held_positive_bit: 3
    latch_bit: 4
    latch_negative_on: false
```

Joysticks occupy two CCs (negative/positive axes). They update:
- **Held bits** — set while value > threshold, cleared on release.
- **Latch bit** — toggles on each new press (negative sets `latch_negative_on`, positive sets opposite).

All joystick bits feed into the same `shift_state` bitmask used by CC mapping actions.

---

## CC Mapping Actions

```yaml
cc_mappings:
  70:    # Source CC number
    - mask: 0x006F
      compare: 0x0000
      type: relative
      cc: 3
      default: 127
      parameter: bass_to_duck_out    # Resolved to (channel, cc) from destinations
      from_channel: 1                # Only match source channel 1
```

### Action Fields

| Field | Description |
|---|---|
| `mask` | Bitmask applied to `shift_state` before comparing. `0` = always match. |
| `compare` | Expected result of `shift_state & mask`. |
| `type` | `absolute` (output = input) or `relative` (input is 2's complement delta). |
| `cc` | Target CC number. `0` = no-pass (drop the message). |
| `channel` | Target channel (1-based). Omit to use source channel. |
| `from_channel` | Only match this source channel (1-based). Omit to match any. |
| `default` | Initial destination state value. |
| `min` / `max` | Clamp output range (default 0–127). |
| `name` | Descriptive label for logging. |
| `parameter` | Resolve target (cc, channel) from a destination name (e.g. `bass_to_duck_out`). |
| `auto` | Expand to one action per channel that has this unprefixed CC name. |
| `center` | Enable center-snap. `true` = snap to 63, or an int for a custom center value. |
| `seq_reset` | Name of a sequential counter to reset on trigger. |
| `seq_next` | Name of a sequential counter to advance on trigger. |
| `seq_max` | Maximum step count for the named counter. |
| `seq_delay` | Per-counter override of pulse delay (seconds). |

### Matching

ALL actions with matching `(shift_state & mask) == compare` AND `from_channel` (if specified) execute. This is not first-match — multiple actions can fire for one input CC.

### Relative Encoding

Input value is 2's complement: 1–63 = positive delta, 65–127 = negative delta (64 = no change). The delta is accumulated on a per-(cc, channel) destination state, clamped to `[min, max]`.

### Parameter Resolution

`parameter: bass_to_duck_out` is resolved at load time to the `(channel, cc)` pair from `destinations.yaml`. This avoids hardcoding channel numbers in mappings.

`auto: to_duck_out` expands at load time to one action per destination channel that has the unprefixed name `to_duck_out`.

### Destination State

Every mapped CC target has a persistent state value (`destination_states["cc_channel"]`). For relative CCs, deltas accumulate on this state. For absolute CCs, the state is set directly. State persists across presets and restarts.

---

## Center-Snap

When `center: true` (or `center: <int>`) is set on a relative mapping action, the service tracks encoder direction changes. If the user "wiggles" the encoder (4+ direction reversals within 2 seconds) and then stops, the value snaps to center (default 63, or the specified int).

Consistent single-direction movement for 1 second resets the tracker.

---

## Sequential Step Counters

Used for stepping through voice/instrument selectors on hardware that uses a reset + next-pulse protocol.

```yaml
cc_mappings:
  24:
    - {mask: 0, compare: 0, type: absolute, cc: 7, seq_reset: MonoVoice, channel: 14}
  21:
    - {mask: 0, compare: 0, type: absolute, cc: 8, seq_next: MonoVoice, seq_max: 3, channel: 14}
```

On trigger:
- `seq_reset` resets the named counter to step 1.
- `seq_next` advances the counter (wraps at `seq_max`).

The forward CC is sent to hardware as a momentary pulse. Counter positions are saved in presets and state, and restored on recall by pulsing reset + N next pulses with configurable delays.
