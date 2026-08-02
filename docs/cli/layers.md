# `klt layers`

Enumerate the layers of a GDSII or OASIS layout stream.

```
klt layers <file> [--format text|json]
```

- `<file>` — path to a GDSII (`.gds`) or OASIS (`.oas`) file. KLayout
  auto-detects the stream format on read; the extension is not authoritative.
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

### Semantics and guarantees

- **Sort order.** `layers` is sorted by `(layer, datatype)` ascending, so
  output is deterministic across runs and platforms.
- **Shape counts are per-cell-definition.** Each shape is counted once where it
  is *defined*, summed over every cell in the layout. Shapes are **not**
  multiplied by how many times their cell is instantiated. Instance-flattened
  and area-based statistics are a separate concern (`klt stats`).
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

## Exit codes and errors

| Exit code | Meaning                                                              |
| --------- | ------------------------------------------------------------------- |
| `0`       | Success — report written to stdout.                                 |
| `1`       | The file is missing, unreadable, or not a recognisable layout.      |
| `2`       | Usage error (missing argument, bad `--format` value) — from argparse.|

On error, a concise message is written to **stderr** and nothing is written to
stdout. No Python traceback is printed.

- `--format text` (default): a plain-text line prefixed `klt layers:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "layers", "message": "file not found: missing.gds" } }
  ```
