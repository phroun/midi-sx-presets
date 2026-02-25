// POLY SYNTH SCRIPT DRUM MAPPER 2.17
// Advanced CC Remapper with Multiple Shift States and Conditional Routing
// Up to 16 shift states tracked as bitmask, with conditional action execution
var PluginParameters = [
    {
        name: "Debug Mode",
        type: "checkbox",
        defaultValue: 0
    },
    {
        name: "Shift Debug",
        type: "checkbox",
        defaultValue: 0
    }
];

// --- SysEx Protocol Constants ------------------------------------------------

var SYSEX_MFR = 0x7D;        // Non-commercial / educational
var SYSEX_DEV = 0x01;        // Device ID (must match config.yaml)

var CMD_START_RECORD = 0x01;
var CMD_SAVE_PRESET  = 0x02;
var CMD_LOAD_PRESET  = 0x03;
var CMD_PRESET_NAME  = 0x04;
var CMD_DEST_MARKER  = 0x05;
var CMD_ABANDON      = 0x06;
var CMD_DEBUG_TEXT    = 0x07;  // Scripter → Python console
var CMD_REMOTE_CMD   = 0x08;  // Python CLI → Scripter
var CMD_DEVICE_CMD   = 0x09;  // Scripter → Python service (machine-readable)

// --- SysEx Helpers -----------------------------------------------------------

function sendSysex(cmd, dataBytes) {
    var sx = new Sysex();
    var bytes = [0xF0, SYSEX_MFR, SYSEX_DEV, cmd];
    if (dataBytes) {
        for (var i = 0; i < dataBytes.length; i++) {
            bytes.push(dataBytes[i]);
        }
    }
    bytes.push(0xF7);
    sx.data = bytes;
    sx.send();
}

function sendDebugText(text) {
    var encoded = [];
    for (var i = 0; i < text.length; i++) {
        encoded.push(Math.min(text.charCodeAt(i), 127));
    }
    sendSysex(CMD_DEBUG_TEXT, encoded);
}

function sendDeviceCmd(text) {
    var encoded = [];
    for (var i = 0; i < text.length; i++) {
        encoded.push(Math.min(text.charCodeAt(i), 127));
    }
    sendSysex(CMD_DEVICE_CMD, encoded);
}

// --- Remote Command Handling -------------------------------------------------

function handleIncomingSysex(event) {
    // SysEx format: F0 <mfr> <dev> <cmd> [payload…] F7
    if (event.data.length < 4) return;
    if (event.data[0] !== 0xF0) return;
    if (event.data[1] !== SYSEX_MFR || event.data[2] !== SYSEX_DEV) return;

    var cmd = event.data[3];

    var textBytes = [];
    for (var i = 4; i < event.data.length; i++) {
        if (event.data[i] === 0xF7) break;
        textBytes.push(event.data[i]);
    }
    var text = "";
    for (var i = 0; i < textBytes.length; i++) {
        text += String.fromCharCode(textBytes[i]);
    }

    if (cmd === CMD_REMOTE_CMD) {
        handleRemoteCommand(text.trim());
    }
    // Future: handle CMD_DEST_MARKER, CMD_PRESET_NAME for recall mode
}

function handleRemoteCommand(text) {
    var cmd = text.toLowerCase();

    if (cmd === "shift") {
        cmdShiftState();
    } else if (cmd === "state") {
        cmdDestState();
    } else if (cmd === "help") {
        cmdHelp();
    } else {
        sendDebugText("Unknown command: '" + text + "'");
        cmdHelp();
    }
}

function cmdShiftState() {
    var hex = "0x" + ("0000" + shiftState.toString(16).toUpperCase()).slice(-4);
    var bin = "0b" + ("0000000000000000" + shiftState.toString(2)).slice(-16);
    var parts = [];
    for (var i = 0; i < shiftDefinitions.length; i++) {
        var s = shiftDefinitions[i];
        var on = (shiftState & (1 << s.bit)) !== 0;
        parts.push("CC" + s.cc + "=" + (on ? "ON" : "off"));
    }
    for (var j = 0; j < joystickDefinitions.length; j++) {
        var joy = joystickDefinitions[j];
        initJoystickState(j);
        var st = joystickStates[j];
        if (joy.heldNegativeBit !== undefined) {
            var on = (shiftState & (1 << joy.heldNegativeBit)) !== 0;
            parts.push("J" + j + "-=" + (on ? "HELD" : "off"));
        }
        if (joy.heldPositiveBit !== undefined) {
            var on = (shiftState & (1 << joy.heldPositiveBit)) !== 0;
            parts.push("J" + j + "+=" + (on ? "HELD" : "off"));
        }
        if (joy.latchBit !== undefined) {
            var on = (shiftState & (1 << joy.latchBit)) !== 0;
            parts.push("J" + j + "L=" + (on ? "ON" : "off"));
        }
    }
    sendDebugText("Shift: " + hex + " " + bin);
    if (parts.length > 0) {
        sendDebugText("  " + parts.join("  "));
    }
}

function cmdDestState() {
    var count = 0;
    for (var key in destinationStates) {
        count++;
    }
    sendDebugText("Destinations: " + count + " active");
    for (var key in destinationStates) {
        var val = destinationStates[key];
        var parts = key.split("_");
        sendDebugText("  CC" + parts[0] + " ch" + parts[1] + " = " + val);
    }
}

function cmdHelp() {
    sendDebugText("Commands: shift, state, help");
    sendDebugText("  shift  - show current shift bitmask");
    sendDebugText("  state  - dump all destination values");
    sendDebugText("  help   - this message");
}

// --- Shift Definitions -------------------------------------------------------

// Define shift CCs (up to 16)
// Each entry: { cc: number, threshold: number, bit: number (0-15) }
var shiftDefinitions = [
    { cc: 80, threshold: 2, bit: 0 },  // Shift 1 - controls bit 0
    { cc: 81, threshold: 2, bit: 1 },  // Shift 2 - controls bit 1
    // Add up to 16 total
];
var shiftState = 0; // 16-bit value tracking all shift states

// --- Joystick Definitions ----------------------------------------------------

// Define joystick controls
// Each entry creates up to 3 shift states from 2 CCs (one for each axis direction)
var joystickDefinitions = [
    {
        negativeCC: 2,         // CC number for negative direction
        positiveCC: 3,         // CC number for positive direction
        threshold: 63,          // Value > threshold = "held"
        heldNegativeBit: 2,     // Bit number for "currently holding negative" (optional)
        heldPositiveBit: 3,     // Bit number for "currently holding positive" (optional)
        latchBit: 4,            // Bit number for latched "last direction" (optional)
        latchNegativeOn: false  // true = negative sets latch on, positive sets off
    },
    {
        negativeCC: 12,         // CC number for negative direction
        positiveCC: 13,         // CC number for positive direction
        threshold: 63,          // Value > threshold = "held"
        heldNegativeBit: 5,     // Bit number for "currently holding negative" (optional)
        heldPositiveBit: 6,     // Bit number for "currently holding positive" (optional)
        latchBit: 7,            // Bit number for latched "last direction" (optional)
        latchNegativeOn: false  // true = negative sets latch on, positive sets off
    },
    // Add more joysticks as needed
];

// Track joystick state for latch detection
var joystickStates = {};

function initJoystickState(index) {
    if (!(index in joystickStates)) {
        joystickStates[index] = {
            negativeHeld: false,
            positiveHeld: false,
            latchState: false
        };
    }
}

// --- CC Mapping Engine -------------------------------------------------------

// Enhanced mapping structure
// Format: sourceCC: [ array of action objects ]
// Each action:
// {
//   mask: bitmask to AND with shiftState (required)
//   compare: value to match after AND (required)
//   type: "absolute" or "relative" (optional, defaults to "absolute")
//   cc: target CC number (optional)
//   channel: channel override (optional, uses source channel if not specified)
//   default: default value for this destination (optional)
//   min: minimum value (optional, defaults to 0)
//   max: maximum value (optional, defaults to 127)
//   callback: function(sourceCC, sourceValue, currentDestValue) (optional)
// }
// All matching actions execute in sequence

function transportKey(sourceCC, sourceValue, currentDestValue) {
    Trace(sourceCC);
    var newEvent = new ControlChange();
    newEvent.number = sourceCC;
    newEvent.value = sourceValue;
    newEvent.channel = 2;
    newEvent.sendAfterMilliseconds(1);
    var newEvent = new ControlChange();
    newEvent.number = sourceCC;
    newEvent.value = 0;
    newEvent.channel = 2;
    newEvent.sendAfterMilliseconds(20);
}

var ccMappings = {
    // Mod Wheel
    1: [
        { mask: 0, compare: 0, type: "absolute", cc: 1, default: 64, name: "Mod Wheel" }
    ],
    // Joystick Vertical
    2: [
        {  mask: 0x0000, compare: 0x0000, type: "absolute", cc: 0, default: 0, channel: 15, name: "No Pass" }
    ],
    3: [
        {  mask: 0x0000, compare: 0x0000, type: "absolute", cc: 0, default: 0, channel: 15, name: "No Pass" }
    ],
    // Joystick Horizontal
    12: [
        {  mask: 0x0000, compare: 0x0000, type: "absolute", cc: 0, default: 0, channel: 15, name: "No Pass" }
    ],
    13: [
        {  mask: 0x0000, compare: 0x0000, type: "absolute", cc: 0, default: 0, channel: 15, name: "No Pass" }
    ],
    // Control Pads - always send to same CC regardless of shift state
    16: [
        { mask: 0x0000, compare: 0x0000, type: "absolute", cc: 9, default: 0, channel: 14, name: "Loop Reset" }
    ],
    17: [
        { mask: 0x0080, compare: 0x0000, type: "absolute", cc: 10, default: 0, channel: 14, name: "Loop A Retrig" },
        { mask: 0x0080, compare: 0x0080, type: "absolute", cc: 13, default: 0, channel: 14, name: "Loop B Retrig" },
        { mask: 0x0000, compare: 0x0000, type: "absolute", cc: 0, default: 0, channel: 15, name: "No Pass" }
    ],
    18: [
        { mask: 0x0080, compare: 0x0000, type: "absolute", cc: 11, default: 0, channel: 14, name: "Loop A Record" },
        { mask: 0x0080, compare: 0x0080, type: "absolute", cc: 14, default: 0, channel: 14, name: "Loop B Record" },
        { mask: 0x0000, compare: 0x0000, type: "absolute", cc: 0, default: 0, channel: 15, name: "No Pass" }
    ],
    19: [
        { mask: 0x0080, compare: 0x0000, type: "absolute", cc: 12, default: 0, channel: 14, name: "Loop A Erase" },
        { mask: 0x0080, compare: 0x0080, type: "absolute", cc: 15, default: 0, channel: 14, name: "Loop B Erase" },
        { mask: 0x0000, compare: 0x0000, type: "absolute", cc: 0, default: 0, channel: 15, name: "No Pass" }
    ],
    20: [{ mask: 0x0000, compare: 0x0000, type: "absolute", cc: 69, default: 0, channel: 10, name: "Solo" }],
    21: [{ mask: 0x0000, compare: 0x0000, type: "absolute", cc: 8, default: 0, channel: 14, name: "Next" }],
    22: [{ mask: 0x0000, compare: 0x0000, type: "absolute", cc: 22, default: 0, name: "Pad 7" }],
    23: [{ mask: 0x0000, compare: 0x0000, type: "absolute", cc: 24, default: 0, name: "Pad 8" }],

    // Drum Pads - always send to same CC regardless of shift state
    42: [{ mask: 0x0000, compare: 0x0000, type: "absolute", cc: 42, default: 0, name: "Closed Hat" }],
    38: [{ mask: 0x0000, compare: 0x0000, type: "absolute", cc: 38, default: 0, name: "Snare" }],
    36: [{ mask: 0x0000, compare: 0x0000, type: "absolute", cc: 36, default: 0, name: "Kick" }],
    43: [{ mask: 0x0000, compare: 0x0000, type: "absolute", cc: 43, default: 0, name: "Floor/Perc" }],
    46: [{ mask: 0x0000, compare: 0x0000, type: "absolute", cc: 46, default: 0, name: "Open Hat" }],
    49: [{ mask: 0x0000, compare: 0x0000, type: "absolute", cc: 49, default: 0, name: "Crash" }],
    51: [{ mask: 0x0000, compare: 0x0000, type: "absolute", cc: 51, default: 0, name: "Ride" }],

    // Knobs - different CCs for unshifted vs shifted (using bit 0)
    70: [ // K1
        { fromChannel: 1, mask: 0x006F, compare: 0x0000, type: "relative", cc: 3, default: 127, channel: 5, name: "To Dry Out" }, // Solo, Unshifted = Bass Filter
        { fromChannel: 1, mask: 0x006F, compare: 0x0002, type: "relative", cc: 3, default: 0, channel: 1, name: "To Dry Out" }, // Solo, GreenShift = Raw Mono
        { fromChannel: 2, mask: 0x006F, compare: 0x0000, type: "relative", cc: 3, default: 0, name: "To Dry Out" }, // Poly, Unshifted = Raw Poly
        { fromChannel: 2, mask: 0x006F, compare: 0x0003, type: "relative", cc: 3, default: 127, channel: 11, name: "To Dry Out" }, // Poly, TealShift = Perform Filter
        // Loop A to Dry Out:
        { mask: 0x008C, compare: 0x0004, type: "relative", cc: 3, default: 0, channel: 3, name: "Loop A to Dry Out" }, // Vert-Dn Shift + "A"
        { mask: 0x008C, compare: 0x0008, type: "relative", cc: 3, default: 0, channel: 3, name: "Loop A to Dry Out" }, // Vert-Up Shift + "A"
        { mask: 0x0060, compare: 0x0020, type: "relative", cc: 3, default: 0, channel: 3, name: "Loop A to Dry Out" }, // Vert-Lf Shift + "A"
        // Loop B to Dry Out:
        { mask: 0x008C, compare: 0x0084, type: "relative", cc: 3, default: 0, channel: 4, name: "Loop B to Dry Out" }, // Vert-Dn Shift + "B"
        { mask: 0x008C, compare: 0x0088, type: "relative", cc: 3, default: 0, channel: 4, name: "Loop B to Dry Out" }, // Vert-Up Shift + "B"
        { mask: 0x0060, compare: 0x0040, type: "relative", cc: 3, default: 0, channel: 4, name: "Loop B to Dry Out" }, // Vert-Rt Shift + "B"
        // PWM Rate:
        { fromChannel: 2, mask: 0x006F, compare: 0x0001, type: "relative", cc: 89, default: 64, name: "PWM Rate" }, // Poly, BlueShift = FX
        // Attack:
        { fromChannel: 2, mask: 0x006F, compare: 0x0002, type: "relative", cc: 78, default: 0, name: "Amp-Attack" }  // Poly, GreenShift = Envelopes
    ],
    71: [ // K2
        // To Loop:
        { fromChannel: 1, mask: 0x00EF, compare: 0x0000, type: "relative", cc: 102, default: 0, channel: 5, name: "To Loop A" }, // "A", Solo, Unshifted = Bass Filter
        { fromChannel: 1, mask: 0x00EF, compare: 0x0080, type: "relative", cc: 103, default: 0, channel: 5, name: "To Loop B" }, // "B", Solo, Unshifted = Bass Filter
        { fromChannel: 1, mask: 0x00EF, compare: 0x0002, type: "relative", cc: 102, default: 127, channel: 1, name: "To Loop A" }, // "A", Solo, GreenShift = Raw Mono
        { fromChannel: 1, mask: 0x00EF, compare: 0x0082, type: "relative", cc: 103, default: 127, channel: 1, name: "To Loop B" }, // "B", Solo, GreenShift = Raw Mono
        { fromChannel: 2, mask: 0x00EF, compare: 0x0000, type: "relative", cc: 102, default: 127, name: "To Loop Ar" }, // "A", Poly, Unshifted
        { fromChannel: 2, mask: 0x00EF, compare: 0x0080, type: "relative", cc: 103, default: 127, name: "To Loop Br" }, // "B", Poly, Unshifted
        { fromChannel: 2, mask: 0x00EF, compare: 0x0003, type: "relative", cc: 102, default: 0, channel: 11, name: "To Loop Ap" }, // "A", Poly, TealShift = Performance Filter
        { fromChannel: 2, mask: 0x00EF, compare: 0x0083, type: "relative", cc: 103, default: 0, channel: 11, name: "To Loop Bp" }, // "B", Poly, TealShift = Performance Filter
        // Loop to Loop:
        { mask: 0x0060, compare: 0x0020, type: "relative", cc: 102, default: 127, channel: 3, name: "To Loop A" }, // "A", Poly, Unshifted
        { mask: 0x0060, compare: 0x0040, type: "relative", cc: 102, default: 127, channel: 4, name: "To Loop A" }, // "B", Poly, Unshifted
        // Vibrato Rate:
        { fromChannel: 2, mask: 0x008F, compare: 0x0001, type: "relative", cc: 88, default: 64, name: "Vibrato Rate" }, // PolyBlue, Shift+"A", Vibato Rate
        // Vibrato Attack:
        { fromChannel: 2, mask: 0x008F, compare: 0x0081, type: "relative", cc: 76, default: 64, name: "Vibrato Attack" }, // PolyBlue, Shift+"B", Vibrato Attack
        // Decay:
        { fromChannel: 2, mask: 0x006F, compare: 0x0002, type: "relative", cc: 80, default: 10, name: "Amp-Decay" }  // Poly, GreenShift = Envelopes
    ],
    72: [ // K3
        // To DSP:
        { fromChannel: 1, mask: 0x006F, compare: 0x0000, type: "relative", cc: 11, default: 0, channel: 5, name: "To DSP" }, // Mono, Unshifted = Bass Filter
        { fromChannel: 1, mask: 0x006F, compare: 0x0002, type: "relative", cc: 11, default: 0, channel: 1, name: "To DSP" }, // Mono, GreenShift = Raw Mono
        { fromChannel: 2, mask: 0x006F, compare: 0x0000, type: "relative", cc: 11, default: 0, name: "To DSP" }, // Poly, Unshited = Raw Poly
        { fromChannel: 2, mask: 0x006F, compare: 0x0003, type: "relative", cc: 11, default: 0, channel: 11, name: "To DSP" }, // Poly, TealShifted = Peformance Filter
        // Loop to Loop:
        { mask: 0x0060, compare: 0x0020, type: "relative", cc: 103, default: 127, channel: 3, name: "To Loop B" }, // "A", Poly, Unshifted
        { mask: 0x0060, compare: 0x0040, type: "relative", cc: 103, default: 127, channel: 4, name: "To Loop B" }, // "B", Poly, Unshifted
        // Loop A to DSP:
        { mask: 0x0088, compare: 0x0008, type: "relative", cc: 11, default: 0, channel: 3, name: "Loop A to DSP" }, // Vertical Shift + "A"
        // Loop B to DSP:
        { mask: 0x0088, compare: 0x0088, type: "relative", cc: 11, default: 0, channel: 4, name: "Loop B to DSP" }, // Vertical Shift + "B"
        // Tremolo Rate:
        { fromChannel: 2, mask: 0x006F, compare: 0x0001, type: "relative", cc: 86, default: 64, name: "Tremolo Rate" }, // Poly, BlueShift
        // Sustain:
        { fromChannel: 2, mask: 0x006F, compare: 0x0002, type: "relative", cc: 79, default: 48, name: "Amp-Sustain" }  // Poly, GreenShift = Envelopes
    ],
    73: [ // K4
        // Volume To Perform:
        { fromChannel: 1, mask: 0x006F, compare: 0x0000, type: "relative", cc: 7, default: 0, channel: 5, name: "Vol to Perform" }, // Mono, Unshifted = Bass Filter
        { fromChannel: 1, mask: 0x006F, compare: 0x0002, type: "relative", cc: 7, default: 0, channel: 1, name: "Vol to Perform" }, // Mono, GreenShifted = Raw Mono
        { fromChannel: 2, mask: 0x006F, compare: 0x0000, type: "relative", cc: 7, default: 127, name: "Vol to Perform" }, // Poly, Unshifted = Raw Poly
        { fromChannel: 2, mask: 0x006F, compare: 0x0003, type: "relative", cc: 71, default: 0, channel: 14, name: "Wave Morph" }, // Poly, TealShifted (Wave Morph)
        // Loop A to Perform:
        { mask: 0x00EC, compare: 0x0004, type: "relative", cc: 7, default: 127, channel: 3, name: "Loop A to Peform" }, // Vert-Dn Shift + "A"
        { mask: 0x00EC, compare: 0x0008, type: "relative", cc: 7, default: 127, channel: 3, name: "Loop A to Peform" }, // Vert-Up Shift + "A"
        { mask: 0x0060, compare: 0x0020, type: "relative", cc: 7, default: 127, channel: 3, name: "Loop A to Peform" }, // Vert-Lf Shift + "A"
        // Loop B to Perform:
        { mask: 0x00EC, compare: 0x0084, type: "relative", cc: 7, default: 127, channel: 4, name: "Loop B to Peform" }, // Vert-Dn Shift + "B"
        { mask: 0x00EC, compare: 0x0088, type: "relative", cc: 7, default: 127, channel: 4, name: "Loop B to Peform" }, // Vert-Up Shift + "B"
        { mask: 0x0060, compare: 0x0040, type: "relative", cc: 7, default: 127, channel: 4, name: "Loop B to Peform" }, // Vert-Rt Shift + "B"
        // Wah Amount:
        { fromChannel: 2, mask: 0x006F, compare: 0x0001, type: "relative", cc: 87, channel: 2, default: 64, name: "Wah Amount" }, // Poly, BlueShift
        // Release:
        { fromChannel: 2, mask: 0x006F, compare: 0x0002, type: "relative", cc: 77, channel: 2, default: 0, name: "Amp-Release" } // Poly, GreenShift = Envelopes
    ],
    74: [ // K5
        // Stereo Pan:
        { fromChannel: 1, mask: 0x006F, compare: 0x0000, type: "relative", cc: 10, default: 64, channel: 5, name: "Stereo1b Pan" }, // Mono, Unshifted = Bass Filter
        { fromChannel: 1, mask: 0x006F, compare: 0x0002, type: "relative", cc: 10, default: 64, channel: 1, name: "Stereo1r Pan" }, // Mono, GreenShifted = Raw Mono
        { fromChannel: 2, mask: 0x006F, compare: 0x0000, type: "relative", cc: 10, default: 64, name: "Stereo2r Pan" }, // Poly, Unshifted = Raw Poly
        { fromChannel: 2, mask: 0x006F, compare: 0x0003, type: "relative", cc: 10, default: 64, channel: 11, name: "Stereo2p Pan" }, // Poly, TealShifted = Performance Filter
        // Loop A Stereo Pan
        { mask: 0x00EC, compare: 0x0004, type: "relative", cc: 10, default: 64, channel: 3, name: "Loop A Stereo Pan" }, // Vert-Dn Shift + "A"
        { mask: 0x00EC, compare: 0x0008, type: "relative", cc: 10, default: 64, channel: 3, name: "Loop A Stereo Pan" }, // Vert-Up Shift + "A"
        // Loop B Stereo Pan
        { mask: 0x00EC, compare: 0x0084, type: "relative", cc: 10, default: 64, channel: 4, name: "Loop B Stereo Pan" },  // Vert-Up Shift + "B"
        { mask: 0x00EC, compare: 0x0088, type: "relative", cc: 10, default: 64, channel: 4, name: "Loop B Stereo Pan" },  // Vert-Dn Shift + "B"
        // Loop A Speed:
        { mask: 0x0068, compare: 0x0020, type: "relative", cc: 1, default: 64, channel: 14, name: "Loop A Speed" }, // Holding "A"
        // Loop B Speed:
        { mask: 0x0068, compare: 0x0040, type: "relative", cc: 4, default: 64, channel: 14, name: "Loop B Speed" },  // Holding "B"
        // PWM Amount:
        { fromChannel: 2, mask: 0x006F, compare: 0x0001, type: "relative", cc: 95, default: 64, name: "PWM Amount" }, // Poly, BlueShift
        // Filter Attack:
        { fromChannel: 2, mask: 0x006F, compare: 0x0002, type: "relative", cc: 73, default: 32, name: "F-Attack" } // Poly, GreenShift = Envelopes
    ],
    75: [ // K6
        // To FX1
        { fromChannel: 1, mask: 0x006F, compare: 0x0000, type: "relative", cc: 90, default: 0, channel: 5, name: "To FX1" }, // Mono, Unshifted = Bass Filter
        { fromChannel: 1, mask: 0x006F, compare: 0x0002, type: "relative", cc: 90, default: 0, channel: 1, name: "To FX1" }, // Mono, GreenShifted = Raw Mono
        { fromChannel: 2, mask: 0x006F, compare: 0x0000, type: "relative", cc: 90, default: 0, name: "To FX1" }, // Poly, Unshifted = Raw Poly
        { fromChannel: 2, mask: 0x006F, compare: 0x0003, type: "relative", cc: 90, default: 0, channel: 11, name: "To FX1" }, // Poly, TealShifted = Performance Filter
        // Loop A to FX1:
        { mask: 0x008C, compare: 0x0004, type: "relative", cc: 90, default: 0, channel: 3, name: "Loop A to FX1" }, // Vert-Up Shift + "A"
        { mask: 0x008C, compare: 0x0008, type: "relative", cc: 90, default: 0, channel: 3, name: "Loop A to FX1" }, // Vert-Dn Shift + "A"
        // Loop B to FX1:
        { mask: 0x008C, compare: 0x0084, type: "relative", cc: 90, default: 0, channel: 4, name: "Loop B to FX1" },  // Vert-Up Shift + "B"
        { mask: 0x008C, compare: 0x0088, type: "relative", cc: 90, default: 0, channel: 4, name: "Loop B to FX1" },  // Vert-Dn Shift + "B"
        // Loop A Length:
        { mask: 0x0068, compare: 0x0020, type: "relative", cc: 2, default: 127, channel: 14, name: "Loop A Length" }, // Holding "A"
        // Loop B Length:
        { mask: 0x0068, compare: 0x0040, type: "relative", cc: 5, default: 127, channel: 14, name: "Loop B Length" },  // Holding "B"
        // Vibato Amount:
        { fromChannel: 2, mask: 0x006F, compare: 0x0001, type: "relative", cc: 94, default: 64, name: "Vibrato Amount" }, // Poly, BlueShift
        // Filter Decay:
        { fromChannel: 2, mask: 0x006F, compare: 0x0002, type: "relative", cc: 75, default: 32, name: "F-Decay" } // Poly, GreenShift = Envelopes
    ],
    76: [ // K7
        // To FX2:
        { fromChannel: 1, mask: 0x006F, compare: 0x0000, type: "relative", cc: 91, default: 0, channel: 5, name: "To FX2" }, // Mono, Unshifted = Bass Filter
        { fromChannel: 1, mask: 0x006F, compare: 0x0002, type: "relative", cc: 91, default: 0, channel: 1, name: "To FX2" }, // Mono, GreenShifted = Raw Mono
        { fromChannel: 2, mask: 0x006F, compare: 0x0000, type: "relative", cc: 91, default: 0, name: "To FX2" }, // Poly, Unshifted = Raw Poly
        { fromChannel: 2, mask: 0x006F, compare: 0x0003, type: "relative", cc: 91, default: 0, channel: 11, name: "To FX2" }, // Poly, TealShifted = Performance Filter
        // Loop A to FX2:
        { mask: 0x008C, compare: 0x0004, type: "relative", cc: 91, default: 0, channel: 3, name: "Loop A to FX2" }, // Vert-Up Shift + "A"
        { mask: 0x008C, compare: 0x0008, type: "relative", cc: 91, default: 0, channel: 3, name: "Loop A to FX2" }, // Vert-Dn Shift + "A"
        // Loop B to FX2:
        { mask: 0x008C, compare: 0x0084, type: "relative", cc: 91, default: 0, channel: 4, name: "Loop B to FX2" },  // Vert-Up Shift + "B"
        { mask: 0x008C, compare: 0x0088, type: "relative", cc: 91, default: 0, channel: 4, name: "Loop B to FX2" },  // Vert-Dn Shift + "B"
        // Loop A Position:
        { mask: 0x0068, compare: 0x0020, type: "relative", cc: 3, default: 0, channel: 14, name: "Loop A Position" }, // Holding "A"
        // Loop B Position:
        { mask: 0x0068, compare: 0x0040, type: "relative", cc: 6, default: 0, channel: 14, name: "Loop B Position" }, // Holding "B"
        // Tremolo Amount:
        { fromChannel: 2, mask: 0x006F, compare: 0x0001, type: "relative", cc: 92, default: 64, name: "Tremolo Amount" }, // Poly, BlueShift
        // Filter Sustain:
        { fromChannel: 2, mask: 0x006F, compare: 0x0002, type: "relative", cc: 74, default: 32, name: "F-Sustain" } // Poly, GreenShift = Envelopes
    ],
    77: [ // K8
        // Perform Dry Out (Global):
        { fromChannel: 1, mask: 0x006D, compare: 0x0000, type: "relative", cc: 3, default: 127, channel: 11, name: "Perform Out Global" }, // Mono, Unshift or Green
        { fromChannel: 2, mask: 0x006F, compare: 0x0000, type: "relative", cc: 3, default: 127, channel: 11, name: "Perform Out Global" }, // Poly, Unshifted
        { fromChannel: 2, mask: 0x006F, compare: 0x0003, type: "relative", cc: 3, default: 127, channel: 11, name: "Perform Out Global" }, // Poly, Unshifted
        // From Perform to Loop A:
        { mask: 0x008C, compare: 0x0004, type: "relative", cc: 102, default: 0, channel: 11, name: "Perform to Loop A" }, // Vertical Shift + "A"
        { mask: 0x008C, compare: 0x0008, type: "relative", cc: 102, default: 0, channel: 11, name: "Perform to Loop A" }, // Vertical Shift + "A"
        { mask: 0x0060, compare: 0x0020, type: "relative", cc: 102, default: 0, channel: 11, name: "Perform to Loop A" }, // Vertical Shift + "A"
        // From Perform to Loop B:
        { mask: 0x008C, compare: 0x0084, type: "relative", cc: 103, default: 0, channel: 11, name: "Perform to Loop B" },  // Vertical Shift + "B"
        { mask: 0x008C, compare: 0x0088, type: "relative", cc: 103, default: 0, channel: 11, name: "Perform to Loop B" },  // Vertical Shift + "B"
        { mask: 0x0060, compare: 0x0040, type: "relative", cc: 103, default: 0, channel: 11, name: "Perform to Loop B" },  // Vertical Shift + "B"
        // Wah Amount:
        { fromChannel: 2, mask: 0x006F, compare: 0x0001, type: "relative", cc: 93, default: 64, channel: 14, name: "Wah Amount" }, // Poly, BlueShift
        // Filter Release:
        { fromChannel: 2, mask: 0x006F, compare: 0x0002, type: "relative", cc: 72, default: 32, channel: 14, name: "F-Release" } // Poly, GreenShift = Envelopes
    ],
    115: [
        { mask: 0x0000, compare: 0x0000, type: "relative", cc: 115, default: 0, callback: transportKey, name: "Rewind" }
    ],
    116: [
        { mask: 0x0000, compare: 0x0000, type: "relative", cc: 116, default: 0, callback: transportKey, name: "Forward" }
    ],
    117: [
        { mask: 0x0000, compare: 0x0000, type: "relative", cc: 117, default: 0, callback: transportKey, name: "Stop" }
    ],
    118: [
        { mask: 0x0000, compare: 0x0000, type: "relative", cc: 118, default: 0, callback: transportKey, name: "Play" }
    ],
    119: [
        { mask: 0x0000, compare: 0x0000, type: "relative", cc: 119, default: 0, callback: transportKey, name: "Rec" }
    ]
};

// --- Destination State -------------------------------------------------------

var destinationStates = {};

function initializeDestination(ccNum, channel, defaultVal) {
    var key = ccNum + "_" + channel;
    if (!(key in destinationStates)) {
        destinationStates[key] = defaultVal !== undefined ? defaultVal : 0;
    }
}

function getDestinationValue(ccNum, channel) {
    var key = ccNum + "_" + channel;
    return destinationStates[key] !== undefined ? destinationStates[key] : 0;
}

function setDestinationValue(ccNum, channel, value, min, max) {
    var key = ccNum + "_" + channel;
    min = min !== undefined ? min : 0;
    max = max !== undefined ? max : 127;
    value = Math.max(min, Math.min(max, value));
    destinationStates[key] = value;
    return value;
}

function decodeRelative(value) {
    // 2's complement: 1-63 = increment, 65-127 = decrement
    if (value >= 1 && value <= 63) {
        return value;
    } else if (value >= 65 && value <= 127) {
        return -(128 - value);
    }
    return 0;
}

function syncAllDestinations() {
    // Send all current destination values to their outputs
    for (var key in destinationStates) {
        var parts = key.split("_");
        var ccNum = parseInt(parts[0]);
        var channel = parseInt(parts[1]);
        var value = destinationStates[key];

        var syncEvent = new ControlChange();
        syncEvent.number = ccNum;
        syncEvent.value = value;
        syncEvent.channel = channel;
        syncEvent.send();
    }
}

// --- Main MIDI Handler -------------------------------------------------------

function HandleMIDI(event) {
    var debugMsg = "";

    // --- SysEx handling (remote commands from Python service) -----------------
    if (event instanceof Sysex) {
        handleIncomingSysex(event);
        return;
    }

    if (event instanceof ControlChange) {
        var debugMode = GetParameter("Debug Mode");
        var shiftDebug = GetParameter("Shift Debug");

        // Check if this is a shift CC
        for (var i = 0; i < shiftDefinitions.length; i++) {
            var shift = shiftDefinitions[i];
            if (event.number === shift.cc) {
                var isShifted = event.value > shift.threshold;

                if (isShifted) {
                    shiftState |= (1 << shift.bit);
                } else {
                    shiftState &= ~(1 << shift.bit);
                }

                if (shiftDebug) {
                    Trace("*** SHIFT CC" + shift.cc + " (bit " + shift.bit + "): " + event.value + " -> State: 0x" + shiftState.toString(16));
                }

                //event.send();
                break;
            }
        }
        // Check for MIDI reset messages - sync all current values
        if (event.number === 121 || event.number === 123) {
            syncAllDestinations();
            if (shiftDebug) {
                Trace("*** MIDI RESET CC" + event.number + " - Syncing all destinations");
            }
        }

        // Check if this is a joystick CC
        for (var j = 0; j < joystickDefinitions.length; j++) {
            var joystick = joystickDefinitions[j];
            initJoystickState(j);
            var state = joystickStates[j];
            var isJoystickCC = false;
            var isNegative = false;

            if (event.number === joystick.negativeCC) {
                isJoystickCC = true;
                isNegative = true;
            } else if (event.number === joystick.positiveCC) {
                isJoystickCC = true;
                isNegative = false;
            }

            if (isJoystickCC) {
                var wasHeld = isNegative ? state.negativeHeld : state.positiveHeld;
                var isHeld = event.value > joystick.threshold;

                if (isNegative) {
                    state.negativeHeld = isHeld;
                } else {
                    state.positiveHeld = isHeld;
                }

                // Update "held" bits if defined
                if (isNegative && joystick.heldNegativeBit !== undefined) {
                    if (isHeld) {
                        shiftState |= (1 << joystick.heldNegativeBit);
                    } else {
                        shiftState &= ~(1 << joystick.heldNegativeBit);
                    }
                }

                if (!isNegative && joystick.heldPositiveBit !== undefined) {
                    if (isHeld) {
                        shiftState |= (1 << joystick.heldPositiveBit);
                    } else {
                        shiftState &= ~(1 << joystick.heldPositiveBit);
                    }
                }

                // Update latch on transition from not-held to held
                if (joystick.latchBit !== undefined && !wasHeld && isHeld) {
                    var newLatchState;
                    if (isNegative) {
                        newLatchState = joystick.latchNegativeOn;
                    } else {
                        newLatchState = !joystick.latchNegativeOn;
                    }

                    state.latchState = newLatchState;

                    if (newLatchState) {
                        shiftState |= (1 << joystick.latchBit);
                    } else {
                        shiftState &= ~(1 << joystick.latchBit);
                    }
                }

                if (shiftDebug) {
                    Trace("*** JOYSTICK " + j + " " + (isNegative ? "NEGATIVE" : "POSITIVE") + " CC" + event.number + ": " + event.value +
                          " -> Held Neg=" + state.negativeHeld + " Pos=" + state.positiveHeld + " Latch=" + state.latchState +
                          " -> State: 0x" + shiftState.toString(16));
                }

                //event.send();
                break;
            }
        }

        // Check if this CC has mappings
        if (event.number in ccMappings) {
            var actions = ccMappings[event.number];
            var matchedAny = false;

            // Execute ALL matching actions in sequence
            for (var j = 0; j < actions.length; j++) {
                var action = actions[j];
                var channelMatch = action.fromChannel === undefined || action.fromChannel === event.channel;

                // Check if this action matches current shift state
                var maskedState = shiftState & action.mask;
                if (maskedState === action.compare && channelMatch) {
                    matchedAny = true;
                    if (debugMode && targetCC && (targetCC != 14)) {
                        debugMsg += "Ch" + event.channel + "CC" + event.number + " matched action " + j + " (state=0x" + shiftState.toString(16) + ", mask=0x" + action.mask.toString(16) + ", compare=0x" + action.compare.toString(16) + ")";
                    }

                    // Determine target channel
                    var targetChannel = action.channel !== undefined ? action.channel : event.channel;

                    // Handle CC output if specified
                    if (action.cc !== undefined) {
                        var targetCC = action.cc;
                        var actionType = action.type !== undefined ? action.type : "absolute";
                        var defaultVal = action.default !== undefined ? action.default : (actionType === "relative" ? 64 : 0);
                        var minVal = action.min !== undefined ? action.min : 0;
                        var maxVal = action.max !== undefined ? action.max : 127;

                        initializeDestination(targetCC, targetChannel, defaultVal);

                        var outputValue;
                        if (actionType === "relative") {
                            var delta = decodeRelative(event.value);
                            var currentValue = getDestinationValue(targetCC, targetChannel);
                            outputValue = setDestinationValue(targetCC, targetChannel, currentValue + delta, minVal, maxVal);
                        } else {
                            outputValue = setDestinationValue(targetCC, targetChannel, event.value, minVal, maxVal);
                        }

                        if (debugMode) {
                            var targetName = "";
                            if (action.name !== undefined) {
                                targetName = action.name + ", ";
                            }
                            debugMsg += "\nShift State: 0b" + ("0000000000000000" + shiftState.toString(2)).slice(-16);
                            debugMsg += "\n  -> " + targetName + "CC" + targetCC + " = " + outputValue + " (ch " + targetChannel + ")";
                        }

                        var newEvent = new ControlChange();
                        newEvent.number = targetCC;
                        newEvent.value = outputValue;
                        newEvent.channel = targetChannel;
                        newEvent.send();
                    }

                    // Execute callback if specified
                    if (action.callback !== undefined && typeof action.callback === 'function') {
                        var currentDestValue = action.cc !== undefined ? getDestinationValue(action.cc, targetChannel) : undefined;
                        action.callback(event.number, event.value, currentDestValue);
                    }
                }
            }

            // If no actions matched, optionally log
            if (!matchedAny && debugMode) {
                Trace("Shift State: 0b" + ("0000000000000000" + shiftState.toString(2)).slice(-16));
                Trace("CC" + event.number + " had no matching actions for state 0x" + shiftState.toString(16));
            }
        }
    } else {
        if (debugMsg != "") {
            Trace(debugMsg);
        }
        debugMsg = "";
        // Pass through all non-CC, non-SysEx events
        Trace(event);
        event.send();
    }
    if (debugMsg != "") {
        Trace(debugMsg);
    }
    debugMsg = "";
}

function ParameterChanged(param, value) {
    // Parameter changes take effect immediately
}

function Reset() {
    shiftState = 0;
    destinationStates = {};
}
