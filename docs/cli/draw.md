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

Unrecognised keys are **rejected**, with one reserved escape hatch for
annotations — see ["Unrecognised keys"](#unrecognised-keys) below.

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
- `array` (object, optional) — steps the shape's geometry into a regular grid
  of instances instead of drawing it once, so a via/contact farm on a uniform
  pitch does not need one JSON shape entry per cut:

  ```json
  { "layer": [41, 0],
    "rect_um": [7.0, 1.5, 7.26, 1.76],
    "array": { "pitch_um": [0.86, 0.86], "count": [71, 71] } }
  ```

  - `pitch_um` (**required**) — `[dx, dy]` step between adjacent instances, in
    microns.
  - `count` (**required**) — `[nx, ny]` number of *instances* per axis (not
    gaps), each a positive integer. `[1, 1]` is equivalent to omitting `array`
    entirely — output is unchanged.
  - Instance `(i, j)` is placed at `unit_geometry + (i * pitch_x, j *
    pitch_y)`, computed in **integer database units after the unit shape (and
    the pitch) are snapped to `dbu_um`** — not by accumulating
    `origin_um + i * pitch_um` in floating point before conversion — so every
    instance lands exactly on pitch, by construction, regardless of whether
    `pitch_um` is exactly representable in binary float.
  - The response's `shape_count` and the matching `layers[].shapes` entry
    count every expanded instance (`count: [71, 71]` contributes 5041), not 1
    per `shapes` array entry — see "Response" below.
  - Out of scope for this key: a `bounds_um` ("fill this window") alternative
    to `count`, and cell/instancing (`AREF`) — an array of shapes only, no
    hierarchy.

### Label entry

- `layer` (**required**) — a `[layer, datatype]` pair, as above.
- `text` (string, **required**) — the label string.
- `at_um` (**required**) — an `[x, y]` anchor point in microns.

## Unrecognised keys

**A key that is not documented above is an application error (exit 1) naming the
offending key — except a key beginning with an underscore (`_`), which is
reserved for caller annotations and is always accepted and ignored.**

This is a promise in both directions, at *every* level of the request — the
top-level request object, `params`, `options`, each `shapes[]` entry, each
`labels[]` entry, and a shape's `array` object all enforce their own key list:

| Key | Behavior |
| --- | -------- |
| A documented key | Read as documented. |
| `_`-prefixed (`_purpose`, `_rule`, `_expected_rules`, …) | Accepted and **ignored**. Reserved forever — no future version of `klt draw` will give an underscore-prefixed key meaning, so an annotation can never collide with a real key. |
| Anything else | **Rejected**: exit 1 with an error naming the unknown key(s) and listing the allowed set for that object. Nothing is written. |

Rejecting is what makes a typo in a *real* key visible. `{"rect_nm": [...]}`
(or `count` misspelled `counts` inside `array`) is far more likely to be a
mistake than an intentional annotation, and silently dropping it would produce a
successfully-written stream missing the geometry the caller asked for. The same
posture is already applied to [`klt gen-compose`](gen-compose.md)'s
`request.pdk`.

The `_` prefix exists because JSON has no comment syntax, and the fixtures
`klt draw` is built to produce are exactly the ones that most need a comment: a
known-bad DRC negative control whose dimensions are *deliberately* illegal. A
reviewer opening such a file must be able to tell which rule each shape is meant
to trip, and that "cleaning up" a dimension would silently turn the fixture legal
and the negative control into a no-op. Annotate it in place:

```json
{
  "_purpose": "negative control: must come back flagged by sky130 poly.width.1",
  "_expected_rules": ["poly.width.1"],
  "shapes": [
    {
      "layer": [66, 20],
      "name": "poly.drawing",
      "rect_um": [0, 0, 0.1, 2.0],
      "_rule": "poly.width.1 — 0.1 um is below the 0.15 um minimum, on purpose"
    }
  ]
}
```

Errors are reported per object, e.g.:

```
shape[0] has unknown key(s): rect_nm -- allowed: array, layer, name, polygon_um, rect_um (keys beginning with '_' are reserved for caller annotations and ignored)
```

This policy applies to `klt draw` only. Other request-JSON verbs document (or
do not yet document) their own; do not infer this one from them, or theirs from
this one.

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

- `shape_count` — the total number of shapes actually written, i.e. every
  `array`-expanded instance counted individually, not the number of entries in
  `params.shapes` (a single entry with `array.count: [71, 71]` contributes
  5041, matching what the same command with no `array` key would have needed
  5041 entries to produce).
- `layers` — one entry per `(layer, datatype)` pair written, in first-seen
  order, with the resolved `name` (or `null`) and the count of **shapes** placed
  on it, likewise expanded per-instance (labels are not counted here).
- `bbox_um` — the top cell's bounding box in microns, or `null` if nothing was
  drawn (impossible in practice, since `shapes` must be non-empty).
- `warnings` — always carries the loud not-design-legal marker, so a caller can
  never mistake a `draw` output for a rule-checked, PDK-legal cell.

## Exit codes

| Code | Meaning |
| ---- | ------- |
| `0`  | The stream was written; the success payload is on stdout. |
| `1`  | Application error: invalid/missing `--params`, a malformed shape/label, an [unrecognised key](#unrecognised-keys), empty `shapes`, a nonexistent output directory, or a write failure. Documented error envelope on stderr. |
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

## Example: an array of via cuts

A 71x71 via farm on a 0.86 um pitch, as one shape entry instead of 5041:

```bash
klt draw --params '{"shapes": [{"layer": [41, 0], "rect_um": [7.0, 1.5, 7.26, 1.76], "array": {"pitch_um": [0.86, 0.86], "count": [71, 71]}}]}' -o vias.gds --format json
# -> "shape_count": 5041, "layers": [{"layer": 41, "datatype": 0, "name": null, "shapes": 5041}]
```

See [issue #553](https://github.com/2AMLogic/klayout-tools/issues/553) for the
motivating real-world fixture (17134 shape entries / 1.4 MB of JSON for a small
cell of via/contact arrays) that `array` was added to compress.
