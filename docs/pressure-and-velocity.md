# Pressure & Velocity Processing

Extra input devices (configured under `routing.inputs`) can apply per-channel velocity and pressure processing. This runs in the input callback before messages enter the main queue.

---

## Velocity Curves

Remap note-on velocity through a power curve lookup table.

```yaml
velocity_curve:
  floor: 30       # Minimum output (inputs below low clamp here)
  low: 50         # Input value that maps to floor
  cap: 99         # Maximum output (inputs above cap clamp here)
  curve: 0.5      # Power exponent: < 1 = more sensitive at low end, > 1 = less
```

The 128-entry table is built at startup:
- Index 0 → 0 (note-off semantics).
- [1, low) → floor (quiet playing clamps to floor).
- [low, cap] → [floor, cap] via `floor + (cap - floor) * ((v - low) / (cap - low))^curve`.
- (cap, 127] → cap (hard ceiling).

---

## Pressure-to-Poly (P2P)

Converts channel aftertouch (one value per channel) into per-note polyphonic aftertouch. Each active note gets its own pressure stream with decay, slew limiting, and shelf gating.

### How It Works

1. On **note-on**: the note is tracked. Its initial pressure is set to the note's velocity (or a fixed `pressure_start` value).
2. On **aftertouch**: the raw pressure is transformed through the `pressure_curve` table, then processed through decay/slew.
3. A background **tick thread** (every ~15ms) re-evaluates decay and slew for all active notes, ensuring smooth output even without new physical pressure data.
4. On **note-off**: a final polytouch value of 0 is sent, and the note is removed from tracking.

Raw channel aftertouch on P2P channels coming from the primary input (IAC bus) is blocked to prevent bypassing the floor/slew processing.

### Decay

After note-on, the output pressure floor decays from the start value down to the shelf level over `velocity_decay` milliseconds (or to the pressure curve's floor if no shelf is configured). The actual output is `max(pressure, decay_floor)` — so the player's real-time pressure can always push above the decay floor.

### Shelf

The shelf creates an initial hold level:
- If the start velocity is above the shelf, output holds at the shelf level (not the floor) until the player's physical pressure reaches the shelf value — this "unlocks" the floor.
- If the start velocity is at or below the shelf, output holds at velocity (no decay) until unlock.

The **shelf top** creates a dead zone: pressure values between `shelf` and `shelf_top` are clamped to `shelf`. Values above `shelf_top` are rescaled to cover [shelf, cap].

### Slew Rate Limiting

Asymmetric rate limiting on the output:
- `pressure_slew_up`: max steps/sec for increasing pressure (0 = unlimited).
- `pressure_slew_down`: max steps/sec for decreasing pressure (0 = unlimited).

When only `velocity_decay` is set (legacy mode), `slew_down` defaults to `127 / decay_seconds`.

### Pressure Curve

Same format as velocity curves, applied to raw pressure values before decay/slew processing.

---

## Note Debounce

Prevents very short note-offs from cutting a note prematurely (common with pressure-sensitive pads).

```yaml
note_debounce: 30    # ms — minimum note lifetime
```

If a note-off arrives within `note_debounce` ms of the note-on, it's deferred until the timer expires. If a re-strike arrives before expiry, the pending off is cancelled (no audible gap).

---

## Velocity Destination

Sends the note-on velocity as a CC message, optionally with decay.

```yaml
velocity_destination:
  cc: 46                 # CC number to send velocity as
  channel: "target"      # "source" (same as input), "target" (after note mapping), or int (1-based)
velocity_shelf: 16       # Floor for velocity CC decay (0 = decay to zero)
replace_note_velocity: 100   # Replace note-on velocity with this fixed value
```

The velocity CC is sent on note-on and set to 0 on note-off. If `velocity_decay` is configured, the CC decays from the initial velocity down to `velocity_shelf` over the decay period, driven by the background tick thread.

When `channel: "target"`, the CC is deferred until the note mapping engine resolves the output channel, then sent just before the note-on is forwarded.
