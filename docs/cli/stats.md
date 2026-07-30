# `klt stats`

Report bounding box, drawn area, density, and polygon/vertex counts of a
GDSII or OASIS layout stream, in total and optionally per layer.

```
klt stats <file> [--per-layer] [--format text|json]
```

- `<file>` — path to a GDSII (`.gds`) or OASIS (`.oas`) file. KLayout
  auto-detects the stream format on read; the extension is not authoritative.
- `--per-layer` — also report the same statistics broken down per layer.
  Without this flag, only the `total` figures are computed (`layers` is
  `null`).
- `--format` — `text` (default, a human-readable summary) or `json`.

The command runs fully headless via KLayout's batch database API
(`klayout.db`) — no GUI, no Qt — and is safe to run in CI.

## Determinism

The numbers this command reports are meant to be asserted against in test
fixtures, so **the same input always produces the same output**:

- Area and vertex counts are accumulated in database units as exact Python
  `int` arithmetic, then converted to micrometres with a single final
  multiplication — results never depend on shape iteration order or
  floating-point summation order.
- `layers[]` (when `--per-layer` is given) is sorted by `(layer, datatype)`
  ascending, matching `klt layers`.

## JSON schema (the contract)

**JSON is the API.** Human-readable text output is a courtesy; the JSON schema
below is the stable contract. Per the project's rules, **breaking (renaming,
removing, or retyping) a field is a breaking change**. New fields may be added
without breaking the contract, so consumers should ignore unknown fields. See
[`docs/json-contract.md`](../json-contract.md) for the envelope shared across
all `klt` commands (`schema_version`, error shape, exit codes).

```json
{
  "schema_version": 1,
  "file": "design.gds",
  "dbu_um": 0.001,
  "top_cell": "TOP",
  "bbox_um": {
    "left": 0.0,
    "bottom": 0.0,
    "right": 3.0,
    "top": 3.0,
    "width": 3.0,
    "height": 3.0
  },
  "total": {
    "area_um2": 2.125,
    "density": 0.2361111111111111,
    "polygon_count": 3,
    "vertex_count": 11
  },
  "layers": null
}
```

With `--per-layer`, `layers` is a list instead of `null`:

```json
"layers": [
  {
    "layer": 1,
    "datatype": 0,
    "name": "metal1",
    "area_um2": 2.0,
    "density": 0.2222222222222222,
    "polygon_count": 2,
    "vertex_count": 8
  },
  {
    "layer": 2,
    "datatype": 0,
    "name": null,
    "area_um2": 0.125,
    "density": 0.013888888888888888,
    "polygon_count": 1,
    "vertex_count": 3
  }
]
```

### Top-level fields

| Field            | Type                    | Description                                                                  |
| ---------------- | ----------------------- | ----------------------------------------------------------------------------- |
| `schema_version` | integer                 | Version of this command's JSON shape (starts at `1`; per-command).          |
| `file`           | string                  | The input path exactly as provided on the command line.                     |
| `dbu_um`         | number (float)          | Database unit in micrometres (e.g. `0.001` = 1 nm).                         |
| `top_cell`       | string \| null          | Name of the layout's single top cell, or `null` if the layout has no cells. |
| `bbox_um`        | object                  | The top cell's bounding box (hierarchy-inclusive), in micrometres. See below. |
| `total`          | object                  | Aggregate area/density/polygon/vertex figures across all layers. See below. |
| `layers`         | array\<object\> \| null | Per-layer breakdown; `null` unless `--per-layer` was given.                 |

### `bbox_um`

| Field    | Type           | Description                                  |
| -------- | -------------- | --------------------------------------------- |
| `left`   | number (float) | Left edge, in micrometres.                    |
| `bottom` | number (float) | Bottom edge, in micrometres.                  |
| `right`  | number (float) | Right edge, in micrometres.                   |
| `top`    | number (float) | Top edge, in micrometres.                     |
| `width`  | number (float) | `right - left`, in micrometres.               |
| `height` | number (float) | `top - bottom`, in micrometres.               |

All zero if the layout has no cells.

### `total` and `layers[]` entries

| Field           | Type             | Description                                                          |
| --------------- | ---------------- | ---------------------------------------------------------------------- |
| `layer`         | integer          | *(`layers[]` only)* Layer number.                                    |
| `datatype`      | integer          | *(`layers[]` only)* Datatype number.                                 |
| `name`          | string \| null   | *(`layers[]` only)* Layer name, or `null` for unnamed layers.        |
| `area_um2`      | number (float)   | Drawn area in square micrometres (see semantics below).              |
| `density`       | number (float)   | `area_um2 / bbox_um area`; `0.0` if the bounding box has zero area.  |
| `polygon_count` | integer          | Number of area-bearing shapes (boxes, polygons, paths).              |
| `vertex_count`  | integer          | Total vertex count across those shapes.                              |

### Semantics and guarantees

- **Single top cell required.** `klt stats` reports a single bounding-box
  reference frame shared by `total` and every `layers[]` entry, taken from
  the layout's one top cell. A layout with more than one top cell is
  ambiguous and raises an error (exit code `1`). A layout with zero cells
  reports `top_cell: null` and an all-zero `bbox_um`.
- **Bounding box is hierarchy-inclusive.** `bbox_um` covers the top cell and
  everything instantiated beneath it, not just shapes drawn directly in the
  top cell.
- **Area/vertex counts are per-cell-definition**, exactly like `klt layers`'
  shape counts: each shape is counted once where it is *defined*, summed
  over every cell in the layout — **not** multiplied by how many times its
  cell is instantiated.
- **Overlapping shapes are not merged.** `area_um2` is the sum of individual
  shape areas; overlapping geometry is double-counted. This keeps the
  computation cheap and exactly reproducible (no polygon-merge dependency on
  iteration order).
- **`polygon_count` counts area-bearing shapes only** — boxes, polygons, and
  paths. Text labels (and other non-area shape kinds) are excluded, since
  they carry no drawn area or meaningful vertex count.
- **Names.** OASIS supports named layers; plain GDSII typically does not, so
  `name` is usually `null` for GDSII inputs, matching `klt layers`.

## Text format

The default `text` format prints a summary followed by an aligned per-layer
table when `--per-layer` is given. It is intended for human eyes and its
exact layout is **not** part of the contract — parse the JSON instead.

```
$ klt stats design.gds --per-layer
file: design.gds
dbu_um: 0.001
top_cell: TOP
bbox_um: (0.0, 0.0) - (3.0, 3.0)  3.0 x 3.0
area_um2: 2.125
density: 0.2361111111111111
polygons: 3
vertices: 11

layer  datatype  name  area_um2  density              polygons  vertices
-----  --------  ----  --------  -------------------  --------  --------
    1         0  -          2.0  0.2222222222222222          2         8
    2         0  -        0.125  0.013888888888888888        1         3
```

## Exit codes and errors

| Exit code | Meaning                                                              |
| --------- | ------------------------------------------------------------------- |
| `0`       | Success — report written to stdout.                                 |
| `1`       | The file is missing, unreadable, not a recognisable layout, or the layout has more than one top cell. |
| `2`       | Usage error (missing argument, bad `--format` value) — from argparse.|

On error, a concise message is written to **stderr** and nothing is written to
stdout. No Python traceback is printed.

- `--format text` (default): a plain-text line prefixed `klt stats:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "stats", "message": "file not found: missing.gds" } }
  ```
