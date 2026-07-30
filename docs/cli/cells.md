# `klt cells`

Report the cell hierarchy of a GDSII or OASIS layout stream.

```
klt cells <file> [--top] [--format text|json]
```

- `<file>` — path to a GDSII (`.gds`) or OASIS (`.oas`) file. KLayout
  auto-detects the stream format on read; the extension is not authoritative.
- `--top` — narrow the `cells` array to top cells only (cells with no parent
  instances). The record shape is unchanged, so code parsing one path also
  parses the other.
- `--format` — `text` (default, a human-readable aligned table) or `json`.

The command runs fully headless via KLayout's batch database API
(`klayout.db`) — no GUI, no Qt — and is safe to run in CI.

## JSON schema (the contract)

**JSON is the API.** Human-readable text output is a courtesy; the JSON schema
below is the stable contract. Per the project's rules, **breaking (renaming,
removing, or retyping) a field is a breaking change**. New fields may be added
without breaking the contract, so consumers should ignore unknown fields.

```json
{
  "file": "design.gds",
  "dbu_um": 0.001,
  "cell_count": 2,
  "top_cell_count": 1,
  "cells": [
    {
      "name": "TOP",
      "index": 0,
      "is_top": true,
      "shapes": 2,
      "instances": 1,
      "children": ["SUB"],
      "parents": [],
      "bbox_um": { "left": 0.0, "bottom": 0.0, "right": 0.03, "top": 0.03 }
    },
    {
      "name": "SUB",
      "index": 1,
      "is_top": false,
      "shapes": 1,
      "instances": 0,
      "children": [],
      "parents": ["TOP"],
      "bbox_um": { "left": 0.02, "bottom": 0.02, "right": 0.03, "top": 0.03 }
    }
  ]
}
```

### Top-level fields

| Field            | Type            | Description                                                                          |
| ---------------- | --------------- | ------------------------------------------------------------------------------------- |
| `file`           | string          | The input path exactly as provided on the command line.                               |
| `dbu_um`         | number (float)  | Database unit in micrometres (e.g. `0.001` = 1 nm).                                   |
| `cell_count`     | integer         | Total cells in the **whole layout**, regardless of `--top` filtering.                  |
| `top_cell_count` | integer         | Total top cells in the **whole layout**, regardless of `--top` filtering.              |
| `cells`          | array\<object\> | One entry per cell — the full set, unless `--top` narrows it to top cells only.        |

### `cells[]` entries

| Field       | Type              | Description                                                                                                                    |
| ----------- | ----------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `name`      | string            | Cell name.                                                                                                                      |
| `index`     | integer           | KLayout cell index (`Cell.cell_index()`), stable within one read.                                                               |
| `is_top`    | boolean           | `true` if the cell has no parent instances (`Layout.top_cells()` membership).                                                   |
| `shapes`    | integer           | Shape count owned by this cell's own definition, summed across all layers — same per-definition rule as `klt layers`' `shapes`. |
| `instances` | integer           | Count of child-instance placement records (`Cell.each_inst()`). An arrayed instance counts as **one** record, not its expanded row×column total. |
| `children`  | array\<string\>   | Deduplicated names of cells this cell instantiates directly (one level down only, not transitive).                              |
| `parents`   | array\<string\>   | Deduplicated names of cells that instantiate this cell directly (empty exactly when `is_top` is `true`).                        |
| `bbox_um`   | object \| null    | `{ "left", "bottom", "right", "top" }` in micrometres (from `Cell.dbbox()`, which folds in child geometry). `null` when the cell (including children) has no geometry at all. |

### Semantics and guarantees

- **Sort order.** `cells` is sorted by `index` ascending, so output is
  deterministic across runs and platforms.
- **Whole-layout counts.** `cell_count`/`top_cell_count` always describe the
  whole layout, whether or not `--top` is passed.
- **`--top` filtering.** Narrows `cells` to `is_top: true` entries only; the
  record shape is identical to the unfiltered output.
- **Shape counts are per-cell-definition.** Each shape is counted once where
  it is *defined* in that cell, not multiplied by how many times the cell is
  instantiated — same convention as `klt layers`.
- **Instance counts are placement records, not expanded arrays.** A
  `CellInstArray` with row/column repetition counts as one entry in
  `instances`, not its expanded copy count.
- **Direct hierarchy only.** `children`/`parents` are one level of
  instantiation away; walk the graph from any top cell (deduping on name) to
  reconstruct the full hierarchy.
- **Bounding boxes.** `null` for a cell with no geometry anywhere in its
  subtree, never an empty/inverted box object.

## Text format

The default `text` format prints the file metadata followed by an aligned
table. It is intended for human eyes and its exact layout is **not** part of
the contract — parse the JSON instead.

```
$ klt cells design.gds
file: design.gds
dbu_um: 0.001
cells: 2
top_cells: 1

index  name  is_top  shapes  instances  children  parents  bbox_um
-----  ----  ------  ------  ---------  --------  -------  -------
    0  TOP   yes          2          1  SUB       -        (0, 0, 0.03, 0.03)
    1  SUB   no           1          0  -         TOP      (0.02, 0.02, 0.03, 0.03)
```

## Exit codes and errors

| Exit code | Meaning                                                              |
| --------- | --------------------------------------------------------------------- |
| `0`       | Success — report written to stdout.                                   |
| `1`       | The file is missing, unreadable, or not a recognisable layout.        |
| `2`       | Usage error (missing argument, bad `--format` value) — from argparse. |

On error, a concise message is written to **stderr** (prefixed `klt cells:`)
and nothing is written to stdout. No Python traceback is printed.
