// Multi-Shift Broadcaster
// Receives shift/joystick CCs and rebroadcasts them to designated channels
// Preserves original values so receivers can do their own threshold detection
var PluginParameters = [
    {
        name: "Debug Mode",
        type: "checkbox",
        defaultValue: 0
    }
];
// Define broadcast mappings
// Each input CC can be rebroadcast to multiple output CC/channel combinations
var broadcastMappings = [
    { inputCC: 2, outputs:  [ { cc: 2, channels: "14" } ] },
    { inputCC: 3, outputs:  [ { cc: 3, channels: "14" } ] },
    { inputCC: 12, outputs: [ { cc: 12, channels: "14" } ] },
    { inputCC: 13, outputs: [ { cc: 13, channels: "14" } ] },
    { inputCC: 80, outputs: [ { cc: 80, channels: "14" } ] },
    { inputCC: 81, outputs: [ { cc: 81, channels: "14" } ] },
    { inputCC: 121, outputs: [ { cc: 121, channels: "14" } ] }, // reset
    { inputCC: 123, outputs: [ { cc: 123, channels: "14" } ] }  // reset
];
// Build a lookup table for faster processing
var inputCCLookup = {};
function buildLookupTable() {
    inputCCLookup = {};
    for (var i = 0; i < broadcastMappings.length; i++) {
        var mapping = broadcastMappings[i];
        inputCCLookup[mapping.inputCC] = mapping.outputs;
    }
}
// Initialize lookup table
buildLookupTable();
function HandleMIDI(event) {
    if (event instanceof ControlChange) {
        var debugMode = GetParameter("Debug Mode");

        // Check if this CC should be broadcast
        if (event.number in inputCCLookup) {
            var outputs = inputCCLookup[event.number];

            if (debugMode) {
                Trace("Broadcasting CC" + event.number + " = " + event.value + " to " + outputs.length + " destination(s)");
            }

            // Send to all defined outputs
            for (var i = 0; i < outputs.length; i++) {
                var output = outputs[i];
                var targetCC = output.cc;
                var targetChannels = output.channels;
                // Handle "all" channels shorthand
                if (targetChannels === "all") {
                    for (var ch = 1; ch <= 16; ch++) {  // Changed: 1 to 16, not 0 to 15
                        var broadcast = new ControlChange();
                        broadcast.number = targetCC;
                        broadcast.value = event.value;
                        broadcast.channel = ch;  // Now correctly 1-16
                        broadcast.send();
                    }

                    if (debugMode) {
                        Trace("  -> CC" + targetCC + " on all channels");
                    }
                } else {
                    // Send to specific channels
                    for (var j = 0; j < targetChannels.length; j++) {
                        var ch = targetChannels[j];  // Changed: removed the -1 conversion
                        var broadcast = new ControlChange();
                        broadcast.number = targetCC;
                        broadcast.value = event.value;
                        broadcast.channel = ch;  // Already 1-16 from config
                        broadcast.send();
                    }

                    if (debugMode) {
                        if (typeof targetChannels == "string") {
                          Trace("  -> CC" + targetCC + " on channels " + targetChannels);
                        } else {
                          Trace("  -> CC" + targetCC + " on channels " + targetChannels.join(","));
                        }
                    }
                }
            }

            // Pass through the original event too (optional - comment out if not desired)
            // event.send();
            return;
        }
    }

    // Pass through all other events unchanged
    // event.send();
}
function ParameterChanged(param, value) {
    // Rebuild lookup if needed
}
function Reset() {
    // Reset state if needed
}
