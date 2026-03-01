# Message Processing Flow

## Main Handler Pipeline (`_handle_message`)

Every MIDI message from any input follows this processing order:

```
1. SysEx
   ├─ Ours? → intercept (handle command, do not forward)
   └─ Not ours? → forward to hardware

2. Transport CC (115–119, value > 0)
   ├─ Intercept mode → consume (handle preset/nav, do not forward)
   └─ Pass-through mode → forward to hardware

3. Note-on (velocity > 0)
   ├─ Rec×6 pending? → toggle write-protect on that preset (consume)
   ├─ Rec×1 pending? → save preset to that note (consume)
   ├─ Load mode? → recall preset for that note (consume)
   ├─ Note mapping matches? → apply transforms, forward results
   └─ No mapping? → forward unchanged

4. Note-off
   ├─ Stored origin info? → release through stored mapping (correct channel)
   ├─ Note mapping matches? → apply transforms, forward results
   └─ No mapping? → forward unchanged

5. Control Change
   a. Process shift CCs (update bitmask)
   b. MIDI reset (CC 121/123) → re-send all destinations
   c. Process joystick CCs (update held/latch bits)
   d. Process CC mapping table (all matching actions fire)
   e. NEVER forward raw CCs — only mapped outputs are sent

6. Polyphonic Aftertouch
   ├─ Note has stored origin? → forward to routed channel
   │   ├─ after_poly_to_chan? → convert to channel aftertouch
   │   └─ otherwise → forward as polytouch on routed channel
   └─ No origin? → forward unchanged

7. Channel Aftertouch
   ├─ P2P channel? → BLOCK (handled by extra input callback)
   └─ Not P2P? → forward unchanged

8. Everything else (pitch bend, etc.) → forward unchanged
```

## Extra Input Callback

Messages from extra hardware inputs are processed in the input callback before entering the main queue:

```
1. Channel filter → reject if not in configured channels
2. Velocity curve → remap velocity via lookup table
3. Note tracking (if P2P, debounce, or vel_dest enabled):
   a. Note-on → track note, send velocity CC, start P2P tracking
   b. Note-off → debounce check, release tracking, send final CCs
   c. Aftertouch → transform through pressure curve, compute P2P
4. Queue message for main handler
```

## Proxy Mode Message Queue

All inputs feed a single `queue.Queue`:
- IAC input (primary): `(iac_port_name, msg)`
- Extra inputs: `(device_name, msg)`
- Velocity CCs: `("_vel_cc", msg)` — forwarded directly, bypass `_handle_message`

The main loop pulls from this queue and dispatches through `_handle_message`.

## Threading Model

| Thread | Purpose |
|---|---|
| Main thread | Queue consumer, runs `_handle_message` |
| IAC input callback | Feeds queue from DAW/MainStage |
| Extra input callbacks | Per-device, feed queue with channel filtering and velocity/pressure processing |
| P2P tick thread | Per-device with P2P enabled. Re-evaluates decay/slew every ~15ms |
| State saver | Periodic flush of `state.yaml` (default every 5s) |
| Center-snap timers | Per-destination `threading.Timer` for idle detection |
