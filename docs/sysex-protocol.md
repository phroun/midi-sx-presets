# SysEx Protocol

All messages use manufacturer ID `0x7D` (non-commercial/educational) and a configurable device ID (default `0x01`).

## Message Format

```
F0 <manufacturer_id> <device_id> <command> [<data…>] F7
```

## Commands

| Cmd | Hex | Direction | Description |
|---|---|---|---|
| Load Preset | `0x03` | Device → Service | Enter load mode; next note-on recalls that preset. |
| Preset Name | `0x04` | Service → Device | ASCII preset name sent back on recall (max 64 chars). |
| Debug Text | `0x07` | Device → Service | Arbitrary 7-bit ASCII printed to console (supports ANSI). |
| Remote Cmd | `0x08` | Service → Device | Text command sent back to device via return port. |
| Device Cmd | `0x09` | Device → Service | Machine-readable command from device to service. |

## Device Commands (0x09)

Text-based commands sent inside a `0x09` SysEx. The service parses the first word as the command name.

| Command | Args | Description |
|---|---|---|
| `ping` | — | Service responds with `pong` via remote cmd. |
| `status` | — | Reports mode, preset count, transport mode. |
| `list` | — | Returns comma-separated list of all preset slots. |
| `mode` | `intercept` / `passthrough` / (none=toggle) | Set or toggle transport mode. |

## Text Encoding

All text in SysEx data bytes is 7-bit safe: each character is clamped to 0–127 (`min(ord(c), 127)`).

## `midi_send.py`

CLI tool to send a remote command (0x08) to the service:

```bash
python midi_send.py "hello from the terminal"
python midi_send.py --config-dir ~/.midi-sx-presets "load bass patch"
```

Reads `config.yaml` to find the correct port name and IDs. Sends on the IAC return port in proxy mode, or the virtual port in standalone mode.
