# Routing pitch lower bound (library)

`klayout_tools.routing_pitch.routing_pitch_bound(deck, layer, *,
requested_width_um=None)` returns a **necessary lower bound** on the
centre-to-centre pitch of two parallel tracks on one layer: the deck's ordinary
minimum `width` plus ordinary minimum `space`, in micrometres. It is a library
function only (no CLI verb, no MCP tool), and reuses the curated `DrcRule`
tables; it is not a second rule engine.

## Contract

| Field | Meaning |
|---|---|
| `status` | `partial` (width and space both found) or `unsupported` (either missing/conditional). Never complete. |
| `min_pitch_um` | `width + space`, `None` when unsupported. With `requested_width_um`, uses that width. |
| `min_width_um`, `min_space_um` | The deck minima, in um (deck nominal dbu rescaled). |
| `rule_ids`, `provenance` | The two `DrcRule` ids and their DRM/runset citations. |
| `unmodeled` | The deck's declared-unmodelled rule ids for that layer (`get_unmodeled_rules`). |
| `reason` | Why the result is only a bound. |

Supported rule classes: ordinary width and space. **Not representable** (so a
result is always `partial`): wide-metal spacing (sky130 `m1.3ab`, `m2.3ab`,
`m3.3ab`, `m4.5ab`; gf180mcu `Mn.2b`) and end-of-line rules (gf180mcu `CO.6a/b`,
`Vn.3c/d`, `Vn.4b/c`). They are listed in `unmodeled`; extending `DrcRule` to
model them is a separate deck-vocabulary change.

Errors (`RoutingPitchError`): unknown deck, unknown layer, or a
`requested_width_um` below the deck minimum (compared in deck dbu).

## Worked example (generic two-track layer)

```python
from klayout_tools.routing_pitch import routing_pitch_bound

r = routing_pitch_bound("sky130", "met1", requested_width_um=0.14)
pitch = r.min_pitch_um          # 0.28 -- must not be smaller than this
# Place two met1 tracks at `pitch` (or larger), write the GDS, then:
#   klt drc layout.gds --deck sky130 --format json
```

The helper does **not** replace DRC: a pitch at the bound can still fail
wide-metal or end-of-line rules listed in `r.unmodeled`, so always run
`klt drc` on the actual layout and read its `coverage.unmodeled_rules`.
