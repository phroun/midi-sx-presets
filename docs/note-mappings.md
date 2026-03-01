# Note Mapping & Polyphony

Defined in the `note_mappings` section of `cc_mappings.yaml`. Note mappings process note-on and note-off messages through range-based rules with optional polyphony management.

---

## Note Range Mapping

```yaml
note_mappings:
  - low: 0
    high: 127
    from_channel: 1
    channel: 5
    transpose: -36
    name: "Bass"
```

### Fields

| Field | Description |
|---|---|
| `low` / `high` | Inclusive note range (0–127). |
| `from_channel` | Only match this source channel (1-based). Omit for any. |
| `mask` / `compare` | Shift-state matching (same as CC mappings). Default: `mask=0, compare=0` (always match). |
| `channel` | Override output channel (1-based). Omit to keep source channel. |
| `transpose` | Semitones to shift note number (positive or negative, clamped to 0–127). |
| `name` | Label for logging. |

Notes that match at least one range are forwarded (possibly modified). Notes that match NO ranges are passed through unchanged. ALL matching ranges execute (not first-match).

Note-off routing uses stored origin info from the original note-on, ensuring releases reach the correct mapping even if shift state changes while a key is held.

---

## Polyphony Limiter

Adding polyphony fields to a note range enables voice allocation.

```yaml
note_mappings:
  - low: 0
    high: 127
    from_channel: 2
    max_polyphony: 4
    fallback_priority: most_recent
    replace_priority: highest
    channel: 2
    name: "Poly Synth"
```

### Polyphony Fields

| Field | Default | Description |
|---|---|---|
| `max_polyphony` | — | Max simultaneous voices (1–16). Enables the polyphony limiter. |
| `fallback_priority` | `most_recent` | When a note is released, which held-but-inactive note to reactivate. |
| `replace_priority` | `lowest` | When polyphony is full, which active note to steal. |
| `polyphony_instance` | — | Shared pool name. Multiple ranges can share one voice pool. |
| `monophonic` | — | Shorthand for `max_polyphony: 1`. Use `true` for anonymous, or a string to also set `polyphony_instance`. |
| `allocation_strategy` | `round_robin` | How distribution slots are assigned (see below). |

### Fallback Priority Options

When a held note is released and polyphony has room, a previously-held-but-stolen note can be reactivated:

| Value | Behavior |
|---|---|
| `most_recent` | Reactivate the most recently pressed held note. |
| `highest` | Reactivate the highest-pitched held note. |
| `lowest` | Reactivate the lowest-pitched held note. |

### Replace Priority Options

When polyphony is full and a new note arrives, an active note must be stolen:

| Value | Behavior |
|---|---|
| `lowest` | Steal the lowest-pitched active note. |
| `second_lowest` | Steal the second-lowest-pitched active note. |
| `highest` | Steal the highest-pitched active note. |
| `oldest` | Steal the earliest-pressed active note. |
| `second_oldest` | Steal the second-earliest-pressed active note. |
| `most_recent` | Steal the most recently pressed active note. |

---

## Polyphonic Distribution

Distributes voices across multiple output channels (e.g. for driving separate synth instances).

```yaml
note_mappings:
  - low: 0
    high: 127
    from_channel: 2
    polyphony_instance: chords
    max_polyphony: 4
    polyphonic_distribution: [2, 4, 6, 7]    # 1-based output channels
    allocation_strategy: first_available
    after_poly_to_chan: true
    channel: 2
    name: "Poly4"
```

### Allocation Strategies

All strategies prefer free slots. When all slots are occupied, falls back to round-robin for voice stealing.

| Strategy | Behavior |
|---|---|
| `round_robin` | Cycle through slots in order. |
| `first_available` | Always pick the lowest-index free slot. |
| `most_idle` | Pick the free slot whose last note was released longest ago. |
| `least_idle` | Pick the free slot whose last note was released most recently. |

### `after_poly_to_chan`

When `true`, polyphonic aftertouch messages are converted to channel aftertouch on the distributed channel. This is useful when each voice goes to a separate synth instance that listens on its own channel.

---

## Shared Polyphony Instances

Multiple note ranges can share a single voice pool by using the same `polyphony_instance` name.

```yaml
note_mappings:
  # Low range: mono bass
  - low: 0
    high: 48
    monophonic: bass
    channel: 1
    name: "Bass"

  # Mid range: also mono bass (same pool)
  - low: 0
    high: 59
    from_channel: 4
    monophonic: bass
    transpose: -48
    channel: 1
    name: "Bass from ch4"

  # High range: 4-voice poly chords
  - low: 49
    high: 127
    polyphony_instance: chords
    max_polyphony: 4
    polyphonic_distribution: [2, 4, 6, 7]
    channel: 2
    name: "Chords"
```

The FIRST range that sets `max_polyphony` for a given instance defines the canonical parameters. Later ranges join the pool — if they repeat parameters with different values, a warning is logged and the first definition wins.

---

## Polyphonic Aftertouch Routing

When a note has been routed through the polyphony engine, polyphonic aftertouch for that note follows the stored output channel. This ensures pressure data reaches the correct distributed voice even if the original input channel differs.
