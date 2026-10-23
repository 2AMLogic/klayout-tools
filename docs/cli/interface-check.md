# `klt interface-check`

Check that two separately generated blocks **mate**: the labelled pins of
block A land on the pads of block B at a declared placement. It reads both
committed streams, translates B into A's frame in memory, and reports per pin
the offset between A's pad and B's pad. No assembled layout is written.

```
klt interface-check <a.gds> <b.gds> --place DX,DY --pins REGEX
                    [--pin-layer-a L/D] [--pin-layer-b L/D] [--tol-um UM]
                    [--top-a CELL] [--top-b CELL] [--format text|json]
```

- `<a.gds>`, `<b.gds>` -- GDSII or OASIS. The two streams may use different
  database units.
- `--place DX,DY` -- required. Translation of B into A's frame, micrometres
  (`"120.5,0"` or `"(120.5, 0)"`).
- `--pins REGEX` -- required. A label is checked when the regex **fully
  matches** its string (`OUT\[\d+\]` matches `OUT[3]`; `OUT` does not).
- `--pin-layer-a`, `--pin-layer-b` -- the layer each side's pads are drawn on.
  Default: each label's own layer (labels drawn on the pad layer). A and B
  may use different layers.
- `--tol-um` -- largest pad-to-pad gap still counted as mated (default `0`:
  the pads must overlap or touch).
- `--top-a`, `--top-b` -- top cell of each stream; required when a stream has
  more than one top cell. Hierarchy is flattened.

## Matching rules

- Labels are paired by identical string. A label present in only one stream is
  `no_partner`.
- A label's **pad** is the merged polygon on the pin layer that contains the
  label point (a point on the pad edge counts as contained). If none contains
  it, the nearest pad by bounding-box distance is used; ties go to the
  lowest, then left-most pad.
- Pads are compared by bounding box: `gap_um` is the Euclidean distance
  between the two boxes (`0` when they overlap or touch) and `offset_um` is B's
  box centre minus A's, in A's frame.
- A name that appears several times in one stream is paired by occurrence
  (bottom-to-top, then left-to-right) and reported as `name#1`, `name#2`, ...
- A label with no polygon on its pin layer is `no_partner` with a `reason`.

## Output

JSON (`schema_version` 1, shared envelope, see
[`docs/json-contract.md`](../json-contract.md)):

```json
{
  "schema_version": 1,
  "a": "strip.gds", "b": "array.gds",
  "place_um": [-10.0, 100.0],
  "pins_pattern": "OUT\\[\\d+\\]",
  "tolerance_um": 0.0,
  "dbu_um": {"a": 0.001, "b": 0.001},
  "status": "fail",
  "pin_count": 4, "ok_count": 3, "misaligned_count": 1, "no_partner_count": 0,
  "pins": [
    {"name": "OUT[2]", "status": "misaligned",
     "offset_um": [0.0, 3.0], "gap_um": 2.0,
     "a": {"layer": "10/0", "label_um": [1.0, 110.5],
           "pad_bbox_um": [0.0, 110.0, 2.0, 111.0]},
     "b": {"layer": "10/0", "label_um": [1.0, 113.5],
           "pad_bbox_um": [0.0, 113.0, 2.0, 114.0]},
     "reason": null}
  ]
}
```

`pins[]` is ordered by name (natural sort, so `OUT[2]` precedes `OUT[10]`).
`status` per pin is `ok`, `misaligned` or `no_partner`; `offset_um` and
`gap_um` are `null` for `no_partner`.

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | every matched pin is `ok` |
| `1` | failed to run: unreadable stream, bad `--place` / `--pins` / layer, or **no label matched `--pins` in either stream** (never a silent pass) |
| `2` | argparse usage error |
| `3` | ran, and at least one pin is `misaligned` or `no_partner` |

## Worked example: row-driver strip vs. array

A row-driver strip emits `OUT[k]` pads at `y0 + k*pitch + dy`; the array it
abuts has landing pads at `k*pitch + dy`. Each cell passes its own DRC/LVS, so
a pitch or pad-height change in either one is invisible until assembly.
Check the committed GDS of both instead:

```
klt interface-check strip.gds array.gds --place=-10,100 --pins 'OUT\[\d+\]' --format json
```

`--place` is where B's origin lands in A's frame (use `--place=-10,100`, with
the `=`, when the value starts with a minus sign). If the array's pitch drifts
from 5.0 to 5.5 um, rows 0.. fail progressively and the report names each
failing row (`OUT[3]`) with its offset, so the broken pitch is visible in one
run.

## Not covered (first increment)

- `mirror=` / `repeat=NxM@(px,py)` placement: repeat is expressed by
  per-index pin names (`OUT[k]` on both sides).
- Via-stack allowance / cross-layer mating: one layer per side.
- Rotation, name remapping between A and B, any PDK deck.

Related: [`klt socket-check`](socket-check.md) validates one stream against a
descriptor of its own outline and pins; `interface-check` compares two streams
directly, so there is no second descriptor to drift.
