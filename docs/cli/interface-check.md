# `klt interface-check`

Check that two separately generated blocks **mate**: the labelled pins of
block A land on the pads of block B at a declared placement. It reads both
committed streams, translates B into A's frame in memory, and reports per pin
the offset between A's pad and B's pad. No assembled layout is written.

```
klt interface-check <a.gds> <b.gds> --place DX,DY --pins REGEX
                    [--pins-a REGEX] [--pins-b REGEX]
                    [--pin-layer-a L/D] [--pin-layer-b L/D] [--tol-um UM]
                    [--pair A_REGEX=B_TEMPLATE ...]
                    [--top-a CELL] [--top-b CELL] [--format text|json]
```

- `<a.gds>`, `<b.gds>` -- GDSII or OASIS. The two streams may use different
  database units.
- `--place DX,DY` -- required. Translation of B into A's frame, micrometres
  (`"120.5,0"` or `"(120.5, 0)"`).
- `--pins REGEX` -- required. A label is checked when the regex **fully
  matches** its string (`OUT\[\d+\]` matches `OUT[3]`; `OUT` does not).
- `--pins-a REGEX`, `--pins-b REGEX` -- optional per-side narrowing. A label
  is checked on a side only when it fully matches `--pins` **and**, when that
  side's flag is given, also fully matches it. `--pins` keeps its meaning
  (the shared universe of pin labels) and stays required; a per-side flag can
  only shrink its own side. A per-side regex that matches no label on its own
  side exits 1 (never a silent pass).
- `--pin-layer-a`, `--pin-layer-b` -- the layer each side's pads are drawn on.
  Default: each label's own layer (labels drawn on the pad layer). A and B
  may use different layers.
- `--tol-um` -- largest pad-to-pad gap still counted as mated (default `0`:
  the pads must overlap or touch).
- `--pair A_REGEX=B_TEMPLATE` -- repeatable. Mate complementary names: an A
  label that fully matches `A_REGEX` pairs with the B label named by the
  `re.Match.expand` template (`\1`, `\g<n>`), e.g.
  `--pair 'n_out\[(\d+)\]=s_in[\1]'`. Split on the last `=`; the first
  matching pair wins. Both names must match `--pins`, each within its own
  side's scope (`--pins-a` / `--pins-b` narrow one side). Invalid
  regex/template exits 1.
- `--top-a`, `--top-b` -- top cell of each stream; required when a stream has
  more than one top cell. Hierarchy is flattened.

## Matching rules

- `--pins-a` / `--pins-b` narrow one side within `--pins`: a label is in a
  side's scope iff it fully matches `--pins` and, when that side's flag is
  given, also fully matches it. The report echoes the effective patterns as
  `pins_pattern_a` / `pins_pattern_b` (`pins_pattern` is unchanged), so the
  report alone tells which seam was checked.
- Labels are paired by identical string, unless `--pair` maps A's name to a
  different B name (reported under A's name, with B's name in `b_name`;
  unmapped pins have `b_name == name`). A label present in only one stream is
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
  "pins_pattern_a": null,
  "pins_pattern_b": null,
  "tolerance_um": 0.0,
  "dbu_um": {"a": 0.001, "b": 0.001},
  "status": "fail",
  "pin_count": 4, "ok_count": 3, "misaligned_count": 1, "no_partner_count": 0,
  "pairs": [],
  "pins": [
    {"name": "OUT[2]", "b_name": "OUT[2]", "status": "misaligned",
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
| `1` | failed to run: unreadable stream, bad `--place` / `--pins` / `--pins-a` / `--pins-b` / layer, or **no label matched `--pins` in either stream** (or a per-side filter emptied its side -- never a silent pass) |
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

## Worked example: one seam of a repeated block

A block `C` whose top edge (`t_out[k]`, `t_in[k]`) mates with the bottom edge
(`b_in[k]`, `b_out[k]`) of the copy placed one pitch above. Checking that one
seam passes the same stream as both inputs (`a == b`) -- which needs the
per-side flags: with `--pins` alone A's own bottom-edge labels are in scope
too, take `--pair`'s same-name fallback, and collide with the declared rules
(exit 1), while declaring the reverse rules instead pulls B's far edge, a
full placement pitch away, into the check.

```
klt interface-check c.gds c.gds --place 0,10 --format json \
    --pins 't_(in|out)\[\d\]|b_(in|out)\[\d\]' \
    --pins-a 't_(in|out)\[\d\]' --pins-b 'b_(in|out)\[\d\]' \
    --pair 't_out\[(\d+)\]=b_in[\1]' --pair 't_in\[(\d+)\]=b_out[\1]'
```

A's scope is its top edge and B's its bottom edge; every A label matches a
rule, so the fallback never fires, and B's far edge is out of scope. `status`
is `pass` with `pin_count` 4 when the seam mates; a facing pad shifted far
enough that the mating pads no longer overlap gives exactly one `misaligned`
row (exit 3).

## Not covered (first increment)

- `mirror=` / `repeat=NxM@(px,py)` placement: repeat is expressed by
  per-index pin names (`OUT[k]` on both sides).
- Via-stack allowance / cross-layer mating: one layer per side.
- Rotation, name-pair files (`--map FILE`), any PDK deck.

Related: [`klt socket-check`](socket-check.md) validates one stream against a
descriptor of its own outline and pins; `interface-check` compares two streams
directly, so there is no second descriptor to drift.
