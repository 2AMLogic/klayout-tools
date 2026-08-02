# `klt draw`

Write a GDSII/OASIS stream **verbatim** from a JSON description of polygons and
labels on explicit `(layer, datatype)` pairs. `klt draw` is the deliberately
dumb write-side counterpart to the read-side verbs (`layers`/`stats`/`cells`/
`drc`/…): it has **no** PDK awareness and does **no** rule checking. That is the
point — unlike [`klt gen`](gen.md), which validates generator params against the
resolved PDK's minimums and refuses out-of-range values, `klt draw` will happily
place a rule-violating shape, so a DRC flow's *negative* case — a known-bad
fixture that must come back flagged with specific rule ids — can be produced
with `klt` alone (see [issue #230](https://github.com/2AMLogic/klayout-tools/issues/230)).

Useful well beyond DRC fixtures: minimal reproducers for a suspected deck bug,
layer-map sanity checks, and the negative fixtures a deck's own test suite
wants.

```
klt draw --params <path-or-inline> [--cell-name <name>] [-o/--output <path>]
                                   [--format text|json]
```

- `--params` — the shape/label description, either a path to a JSON file or an
  inline JSON object (e.g. `--params '{"shapes": [{"layer": [66, 20],
  "rect_um": [0, 0, 0.1, 2.0]}]}'`). A value that resolves to an existing file
  path is read as a file; otherwise it is parsed as inline JSON. **Required** —
  unlike `klt gen`, there is no useful default request (an empty layout is never
  what the caller wants).
- `--cell-name` — name for the written top cell (default: `TOP`).
- `-o`/`--output` — output GDS/OASIS path (default: `<cell_name>.gds`, written
  to the current directory). Format (`.gds`/`.oas`) is inferred from the
  extension, matching `klt render`/`klt gen`. The containing directory must
  already exist.
- `--format` — `text` (default, a human-readable summary) or `json`.

The command runs fully headless via `klayout.db` — no GUI, no Qt — so it is safe
in CI.

## `params` schema

```json
{
  "dbu_um": 0.001,
  "shapes": [
    { "layer": [66, 20], "name": "poly.drawing", "rect_um": [0, 0, 0.1, 2.0] },
    { "layer": [65, 20], "polygon_um": [[0, 0], [1.0, 0], [1.0, 1.0], [0, 1.0]] }
  ],
  "labels": [
    { "layer": [66, 20], "text": "IN", "at_um": [0.05, 1.0] }
  ]
}
```

- `dbu_um` (number, optional, default `0.001`) — the database unit in microns.
  Every coordinate below is given in microns and converted to integer database
  units by rounding to the nearest `dbu_um`.
- `shapes` (array, **required, non-empty**) — an empty or missing `shapes` is an
  error; `klt draw` will not write an empty layout.
- `labels` (array, optional) — text labels.

### Shape entry

- `layer` (**required**) — a `[layer, datatype]` pair of non-negative integers.
  Not validated against any PDK layer map — an arbitrary pair writes
  successfully (a layer-map sanity check is one of the intended uses).
- `name` (string, optional) — a layer name stamped as the stream's `LayerInfo`
  for that pair. If several shapes share a pair, the last non-null `name` wins.
- **Exactly one** geometry key:
  - `rect_um`: `[x0, y0, x1, y1]` in microns. Corners are **normalised**, so a
    reversed `[1, 1, 0, 0]` produces the same box as `[0, 0, 1, 1]` rather than
    an empty one.
  - `polygon_um`: a list of at least three `[x, y]` points in microns.

### Label entry

- `layer` (**required**) — a `[layer, datatype]` pair, as above.
- `text` (string, **required**) — the label string.
- `at_um` (**required**) — an `[x, y]` anchor point in microns.

## Response

`--format json` emits the documented envelope
([`docs/json-contract.md`](../json-contract.md)):

```json
{
  "schema_version": 1,
  "gds_path": "bad.gds",
  "cell_name": "TOP",
  "dbu_um": 0.001,
  "shape_count": 1,
  "label_count": 0,
  "layers": [
    { "layer": 66, "datatype": 20, "name": "poly.drawing", "shapes": 1 }
  ],
  "bbox_um": { "x0": 0.0, "y0": 0.0, "x1": 0.1, "y1": 2.0 },
  "warnings": [
    "written verbatim by `klt draw`: no PDK awareness and no rule checking were applied; this cell is not guaranteed to be design-legal"
  ]
}
```

- `layers` — one entry per `(layer, datatype)` pair written, in first-seen
  order, with the resolved `name` (or `null`) and the count of **shapes** placed
  on it (labels are not counted here).
- `bbox_um` — the top cell's bounding box in microns, or `null` if nothing was
  drawn (impossible in practice, since `shapes` must be non-empty).
- `warnings` — always carries the loud not-design-legal marker, so a caller can
  never mistake a `draw` output for a rule-checked, PDK-legal cell.

**Byte-reproducible output.** The written stream is deterministic: the GDSII
`BGNLIB`/`BGNSTR` timestamp records are zeroed rather than stamped with the
wall clock, so drawing the same description twice yields byte-identical files
(see `docs/cli/gen.md`).

## Exit codes

| Code | Meaning |
| ---- | ------- |
| `0`  | The stream was written; the success payload is on stdout. |
| `1`  | Application error: invalid/missing `--params`, a malformed shape/label, empty `shapes`, a nonexistent output directory, or a write failure. Documented error envelope on stderr. |
| `2`  | Usage error (bad `--format` value) — from argparse. |

## Example: a DRC negative fixture

Draw a poly bar 0.1 µm wide — narrower than sky130's `poly.width.1` minimum of
0.15 µm — then confirm the deck flags it:

```bash
klt draw --params '{"shapes": [{"layer": [66, 20], "name": "poly.drawing", "rect_um": [0, 0, 0.1, 2.0]}]}' -o bad.gds
klt drc bad.gds --deck sky130 --format json   # -> status "violations", poly.width.1, exit 3
```

This is the missing write-side half of the read/write pair the toolchain
otherwise has: the one thing that proves a DRC flow is honest is now something
`klt` itself can produce.
