// POLY SYNTH SCRIPT - PASS-THROUGH 3.0
// All CC remapping, shift tracking, and transport handling now live in
// the Python MIDI Preset Service (midi_preset_service.py).
// This Scripter just forwards all MIDI events unchanged.

function HandleMIDI(event) {
    event.send();
}
