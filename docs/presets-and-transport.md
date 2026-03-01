# Presets & Transport

## Preset Storage

Presets are YAML files in the `presets/` subdirectory, named `preset-ch{N}-{note}.yaml` (1-based channel, zero-padded 3-digit note). Legacy `preset-{note}.yaml` files are loaded into channel 0.

### Preset File Format

```yaml
name: Preset 49
read_only: false

# Top-level named parameters (resolved via destinations)
poly_vcf_cutoff: 127
poly_to_duck_out: 127
perf_to_duck_out: 127
mono_pan: 63
bass_to_duck_out: 110
loop_a_to_duck_out: 0

# Seq counter positions
seq_states:
  MonoVoice: 2

# Fallback: unnamed CCs grouped by channel
channels:
  12:
    cc_values:
      46: 94
```

**Saving**: The service snapshots all `destination_states` values. Parameters with a resolved destination name are stored as top-level keys (e.g. `poly_vcf_cutoff: 127`). Parameters without a name fall into `channels/{ch}/cc_values`. Sequential counter positions are saved under `seq_states`.

**Tag filtering**: Only destination parameters whose tags intersect the bank's `bank_tags` are included. Banks not configured default to `["default"]`.

**Write protection**: Presets with `read_only: true` cannot be overwritten by a save. Protection can be toggled via transport commands.

---

## Transport Controls

Transport CCs (115–119) drive preset management. The service has two transport modes:

### Intercept Mode (default)

Transport CCs are consumed — they do NOT reach hardware.

| Action | Effect |
|---|---|
| **Play** | Enter load mode — next note-on recalls that preset. |
| **Rec x1 + note-on** | Save current state to that note's slot (bank = note's channel). |
| **Rec x1 + Play** | Save to the most recently loaded/saved preset slot. |
| **Stop** | Cancel any pending mode (load mode, rec counter). |
| **Rewind** | Navigate to previous preset in current bank and recall it. |
| **Forward** | Navigate to next preset in current bank and recall it. |
| **Rec x3 + Stop/Play** | Switch to pass-through mode. |
| **Rec x6 + note-on** | Toggle write-protection on that preset. |
| **Rec x6 + Play** | Disable write-protection on last preset. |
| **Rec x6 + Stop** | Enable write-protection on last preset. |

### Pass-Through Mode

Transport CCs are forwarded to hardware. Only one escape hatch:

| Action | Effect |
|---|---|
| **Rec x3 + Stop/Play** | Switch back to intercept mode. |

---

## Preset Recall Flow

When a preset is recalled:

1. **Named parameters** — top-level keys matching destination names are resolved to (channel, CC) and sent. Filtered by bank tags and `recall_ignore`.
2. **Channeled CC values** — `channels/{ch}/cc_values` entries are sent.
3. **Sequential counters** — `seq_states` positions are restored by pulsing reset + next CCs.
4. **Preset name** — sent back to the device as a `CMD_PRESET_NAME` SysEx (for display on controller).

Each recalled CC updates both the hardware output and the internal `destination_states`.

---

## Preset Navigation

Rewind/Forward navigate through the preset bank for the current channel. The cursor wraps around. The per-channel cursor position is persisted in `state.yaml`.

---

## Boot Sync

On startup, the service:
1. Restores `destination_states` from `state.yaml`.
2. Fills any missing destinations from `dest_defaults` (from cc_sets).
3. Sends ALL current destination values to hardware (so hardware matches immediately).
4. Syncs sequential counter positions by pulsing reset + next CCs.
