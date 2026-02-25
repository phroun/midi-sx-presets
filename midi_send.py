#!/usr/bin/env python3
"""
Send a text command to MainStage via SysEx on the IAC return port.

The message is sent as:  F0 <mfr> <dev> 08 <ascii…> F7
The Scripter in MainStage can listen for command 0x08 and act on the text.

Uses the same config.yaml as the preset service to find port names and IDs.

Usage:
  python midi_send.py "hello from the terminal"
  python midi_send.py --config-dir ~/.midi-sx-presets "load bass patch"
"""

import argparse
import sys
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

DEFAULT_CONFIG_DIR = Path.home() / ".midi-sx-presets"
CONFIG_FILENAME = "config.yaml"

# Must match midi_preset_service.py
SYSEX_MANUFACTURER_ID = 0x7D
DEFAULT_DEVICE_ID = 0x01
CMD_REMOTE_CMD = 0x08


def load_config(config_dir):
    path = config_dir / CONFIG_FILENAME
    if path.exists():
        with open(path) as fh:
            return yaml.safe_load(fh) or {}
    print(f"Warning: config not found at {path}, using defaults.")
    return {}


def main():
    parser = argparse.ArgumentParser(
        description="Send a text command to MainStage via SysEx."
    )
    parser.add_argument(
        "text",
        help="The text string to send (received by the Scripter as a SysEx message).",
    )
    parser.add_argument(
        "--config-dir",
        type=str,
        default=None,
        help=f"Configuration directory (default: {DEFAULT_CONFIG_DIR})",
    )
    args = parser.parse_args()

    config_dir = Path(args.config_dir) if args.config_dir else DEFAULT_CONFIG_DIR
    config = load_config(config_dir)

    manufacturer_id = config.get("manufacturer_id", SYSEX_MANUFACTURER_ID)
    device_id = config.get("device_id", DEFAULT_DEVICE_ID)

    routing = config.get("routing")
    if routing:
        port_name = routing["iac_return"]
    else:
        port_name = config.get("port_name", "MIDI SX Presets")

    # Encode text as 7-bit-safe bytes (SysEx data bytes must be 0–127)
    text_bytes = [min(ord(c), 127) for c in args.text]
    sysex_data = [manufacturer_id, device_id, CMD_REMOTE_CMD] + text_bytes

    try:
        with mido.open_output(port_name) as port:
            port.send(mido.Message("sysex", data=sysex_data))
        print(f"Sent to {port_name}: {args.text}")
    except OSError as exc:
        print(f"Error: Could not open port '{port_name}': {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
