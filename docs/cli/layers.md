# `klt layers`

Enumerate the layers of a GDSII or OASIS layout stream.

```
klt layers <file> [--top <cell>] [--flattened] [--include-text] [--format text|json]
```

- `<file>` — path to a GDSII (`.gds`) or OASIS (`.oas`) file. KLayout
  auto-detects the stream format on read; the extension is not authoritative.
- `--top` — top cell to report on when the stream has more than one; omit to
  report shape counts summed across every top cell (today's default,
  unchanged). When given, `shapes` is restricted to that cell's own
  hierarchy — itself plus every cell it calls, directly or indirectly — not
  the whole stream. A named cell absent from the stream exits `1` with a
  clean error. Also selects the flattening root for `--flattened` (see
  below).
- `--flattened` — opt-in (default off; omitting it leaves the report
  byte-identical to before this flag existed). Additionally reports, per
  layer, the *instantiated* layout content: `flattened_shapes` (every
  physical placement of a shape on this layer, hierarchy- and
  transform-flattened), `bbox_um` (the union of every placement's
  transformed physical extents), and `contributors` (every cell definition
  that owns at least one reached shape, with an instance-weighted count).
  See "Semantics and guarantees" below.
- `--include-text` — opt-in, requires `--flattened`. Additionally reports,
  per layer, `text_count` (flattened text-label occurrence count) and
  `texts` (the distinct text strings that occur, each with its own
  flattened occurrence count).
- `--format` — `text` (default, a human-readable aligned table) or `json`.

The command runs fully headless via KLayout's batch database API
(`klayout.db`) — no GUI, no Qt — and is safe to run in CI.

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
  "layer_count": 3,
  "layers": [
    { "layer": 1,  "datatype": 0,  "name": "metal1", "shapes": 2, "annotation": false },
    { "layer": 5,  "datatype": 0,  "name": "empty",  "shapes": 0, "annotation": false },
    { "layer": 66, "datatype": 20, "name": null,     "shapes": 3, "annotation": false },
    { "layer": 994, "datatype": 0, "name": null,     "shapes": 1, "annotation": true }
  ]
}
```

With `--flattened --include-text`, each `layers[]` entry gains five more
fields (purely additive — `schema_version` stays `1`):

```json
{
  "layer": 1, "datatype": 0, "name": "metal1", "shapes": 2, "annotation": false,
  "flattened_shapes": 16,
  "bbox_um": { "left": 0.0, "bottom": 0.0, "right": 0.36, "top": 0.11, "width": 0.36, "height": 0.11 },
  "contributors": [ { "cell": "CHILD", "shapes": 16 } ],
  "text_count": 8,
  "texts": [ { "text": "PIN", "count": 8 } ]
}
```

### Top-level fields

| Field            | Type            | Description                                                        |
| ---------------- | --------------- | ------------------------------------------------------------------ |
| `schema_version` | integer         | Version of this command's JSON shape (starts at `1`; per-command). |
| `file`           | string          | The input path exactly as provided on the command line.            |
| `dbu_um`         | number (float)  | Database unit in micrometres (e.g. `0.001` = 1 nm).                |
| `layer_count`    | integer         | Number of entries in `layers` (i.e. `len(layers)`).                |
| `layers`         | array\<object\> | One entry per layer in the stream's layer table (see below).       |

### `layers[]` entries

| Field      | Type             | Description                                                                 |
| ---------- | ---------------- | --------------------------------------------------------------------------- |
| `layer`    | integer          | Layer number.                                                               |
| `datatype` | integer          | Datatype number.                                                            |
| `name`     | string \| null   | Layer name carried in the stream, or `null` for unnamed layers.             |
| `shapes`   | integer          | Shape count summed across all cell **definitions** (see semantics below).   |
| `annotation` | boolean        | `true` when `(layer, datatype)` falls in the reserved annotation range (see below). |
| `flattened_shapes` | integer   | *(`--flattened` only)* Every physical placement of a shape on this layer, hierarchy- and transform-flattened. |
| `bbox_um`  | object           | *(`--flattened` only)* Physical bounding box of every flattened shape on this layer, in micrometres — see "Semantics and guarantees". |
| `contributors` | array\<object\> | *(`--flattened` only)* `[{"cell": str, "shapes": int}, ...]`, every cell definition that owns at least one reached shape, instance-weighted, sorted by `cell`. |
| `text_count` | integer        | *(`--include-text` only)* Total flattened text-label occurrences on this layer. |
| `texts`    | array\<object\>  | *(`--include-text` only)* `[{"text": str, "count": int}, ...]`, distinct strings with their flattened occurrence counts, sorted by `text`. |

### Semantics and guarantees

- **Sort order.** `layers` is sorted by `(layer, datatype)` ascending, so
  output is deterministic across runs and platforms.
- **Shape counts are per-cell-definition.** Each shape is counted once where it
  is *defined*. Shapes are **not** multiplied by how many times their cell is
  instantiated. Instance-flattened shape/text census data is a separate,
  opt-in concern — see `--flattened`/`--include-text` below. Area-based
  statistics remain a separate command (`klt stats`).
- **`--top` scopes the summation, not just which cells are "checked."**
  Without `--top`, counts are summed over every cell in the stream (today's
  default, unchanged). With `--top <cell>`, counts are summed only over
  `<cell>`'s own hierarchy — itself plus every cell it calls, directly or
  indirectly — so a library stream with several unrelated top cells reports
  one cell's own shape usage, not the whole library's. This also selects the
  flattening root when `--flattened` is given (see below).
- **`--flattened` reports instantiated layout content, not library
  complexity.** `flattened_shapes`, `bbox_um`, and `contributors` are
  computed by walking every physical placement of a shape on each layer via
  KLayout's hierarchy- and transform-aware recursive shape iterator
  (`Cell.begin_shapes_rec`) — a cell drawn once but instantiated `N` times
  with a shape on a layer contributes `N` to that layer's `flattened_shapes`
  and to that cell's own entry in `contributors`. `bbox_um` is the union of
  every placement's transformed extents (rotation, mirroring, displacement,
  and array placement all applied); it uses the same all-zero-box convention
  as `klt stats`' `bbox_um` when a layer has no flattened shapes.
  `contributors` is sorted by `cell` name for deterministic output. Without
  `--top`, the flattening root is every top cell in the stream (so the
  reported counts sum the whole library's actual instantiated content); with
  `--top <cell>`, it is just that cell's own sub-hierarchy. Omitting
  `--flattened` leaves the report byte-identical to before this flag
  existed — every new field is additive and opt-in.
- **`--include-text` reports flattened text occurrences, gated behind its own
  flag.** `text_count` and `texts` require `--flattened` (an error otherwise)
  and are only computed when `--include-text` is also given, since collecting
  per-string census data is extra work callers who only want geometry counts
  should not pay for. `texts` is sorted by `text` for deterministic output.
- **Empty layers.** A layer present in the stream's layer table but carrying no
  shapes is still listed, with `shapes: 0`. (Note: plain GDSII does not persist
  empty layers on write, so they typically appear only in OASIS inputs.)
- **Names.** OASIS supports named layers; plain GDSII typically does not, so
  `name` is usually `null` for GDSII inputs. An empty layer name is normalised
  to `null` (never an empty string).
- **Reserved annotation layers.** `annotation` is `true` when the layer
  number falls in **990-999 (any datatype)**, the range reserved for
  floorplan reservations, out-of-scope regions, and black-box sub-cell
  placeholders — see [`docs/cli/drc.md`](drc.md) → "Reserved annotation
  layer" for the full rationale and cross-check (`(994, 0)` is the documented
  single canonical pair when one value is wanted; the same reservation
  applies to `klt drc`, `klt extract`, `klt stats`, and this command). This
  lets a reader distinguish "this is a floorplan reservation" from "this is
  unrecognised real geometry" without opening the source that generated the
  GDS. `annotation` reflects only the `(layer, datatype)` pair — it says
  nothing about whether the layer actually carries shapes (`shapes` may be
  `0` for a declared-but-empty annotation layer, same as any other layer).

## Text format

The default `text` format prints the file metadata followed by an aligned
table. It is intended for human eyes and its exact layout is **not** part of
the contract — parse the JSON instead.

```
$ klt layers design.oas
file: design.oas
dbu_um: 0.001
layers: 3

layer  datatype  name    shapes  annotation
-----  --------  ------  ------  ----------
    1         0  metal1       2  -
    5         0  empty        0  -
   66        20  -            3  -
```

Unnamed layers render as `-` in the table (and as `null` in JSON). A layer
whose `annotation` field is `true` renders as `yes` in the table.

With `--flattened` (and `--include-text`), the table gains extra columns for
the new fields; `contributors`/`texts` render as comma-separated
`name:count` pairs, and `bbox_um` as `(left, bottom) - (right, top)`:

```
$ klt layers hier.gds --flattened --include-text
file: hier.gds
dbu_um: 0.001
layers: 1

layer  datatype  name  shapes  annotation  flattened_shapes  bbox_um                contributors  text_count  texts
-----  --------  ----  ------  ----------  ----------------  ---------------------  ------------  ----------  -----
    1         0  -          2           -                16  (0, 0) - (0.36, 0.11)  CHILD:16               8  PIN:8
```

## Exit codes and errors

| Exit code | Meaning                                                              |
| --------- | ------------------------------------------------------------------- |
| `0`       | Success — report written to stdout.                                 |
| `1`       | The file is missing, unreadable, not a recognisable layout, `--top` names a cell absent from the stream, or `--include-text` is given without `--flattened`. |
| `2`       | Usage error (missing argument, bad `--format` value) — from argparse.|

On error, a concise message is written to **stderr** and nothing is written to
stdout. No Python traceback is printed.

- `--format text` (default): a plain-text line prefixed `klt layers:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "layers", "message": "file not found: missing.gds" } }
  ```
