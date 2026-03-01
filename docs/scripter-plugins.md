# MainStage Scripter Plugins

Two JavaScript files for Apple MainStage's Scripter MIDI effect.

---

## poly_synth_scripter.js

A **pass-through** script. All CC remapping, shift tracking, and transport handling have been moved to the Python service. This Scripter just forwards all MIDI events unchanged:

```js
function HandleMIDI(event) {
    event.send();
}
```

Historically this contained the shift/mapping logic (up to v2.17), which was ported to Python for maintainability and added capabilities.

---

## shift_broadcaster_scripter.js

Rebroadcasts shift/joystick CCs from the source channel to designated target channels. Used in MainStage to fan out control signals to multiple instrument channels.

### Configuration (in-script)

```js
var broadcastMappings = [
    { inputCC: 2,   outputs: [{ cc: 2,   channels: "14" }] },
    { inputCC: 80,  outputs: [{ cc: 80,  channels: "14" }] },
    { inputCC: 121, outputs: [{ cc: 121, channels: "14" }] },  // reset
];
```

Each entry maps an input CC to one or more output CC/channel combinations. `channels` can be:
- A specific channel number (string, 1-based).
- An array of channel numbers.
- `"all"` to send to channels 1–16.

The script has a **Debug Mode** parameter (checkbox) that enables Trace logging of all broadcasts.

Original events for matched CCs are NOT forwarded (the `event.send()` is commented out). Unmatched events are also not forwarded (both pass-through lines are commented out in the current version).
