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
without breaking the contract, so consumers should ignore unknown fields.

```json
{
  "file": "design.gds",
  "dbu_um": 0.001,
  "layer_count": 3,
  "layers": [
    { "layer": 1,  "datatype": 0,  "name": "metal1", "shapes": 2 },
    { "layer": 5,  "datatype": 0,  "name": "empty",  "shapes": 0 },
    { "layer": 66, "datatype": 20, "name": null,     "shapes": 3 }
  ]
}
```

### Top-level fields

| Field         | Type            | Description                                                        |
| ------------- | --------------- | ------------------------------------------------------------------ |
| `file`        | string          | The input path exactly as provided on the command line.            |
| `dbu_um`      | number (float)  | Database unit in micrometres (e.g. `0.001` = 1 nm).                |
| `layer_count` | integer         | Number of entries in `layers` (i.e. `len(layers)`).                |
| `layers`      | array\<object\> | One entry per layer in the stream's layer table (see below).       |

### `layers[]` entries

| Field      | Type             | Description                                                                 |
| ---------- | ---------------- | --------------------------------------------------------------------------- |
| `layer`    | integer          | Layer number.                                                               |
| `datatype` | integer          | Datatype number.                                                            |
| `name`     | string \| null   | Layer name carried in the stream, or `null` for unnamed layers.             |
| `shapes`   | integer          | Shape count summed across all cell **definitions** (see semantics below).   |

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

## Text format

The default `text` format prints the file metadata followed by an aligned
table. It is intended for human eyes and its exact layout is **not** part of
the contract — parse the JSON instead.

```
$ klt layers design.oas
file: design.oas
dbu_um: 0.001
layers: 3

layer  datatype  name    shapes
-----  --------  ------  ------
    1         0  metal1       2
    5         0  empty        0
   66        20  -            3
```

Unnamed layers render as `-` in the table (and as `null` in JSON).

## Exit codes and errors

| Exit code | Meaning                                                              |
| --------- | ------------------------------------------------------------------- |
| `0`       | Success — report written to stdout.                                 |
| `1`       | The file is missing, unreadable, or not a recognisable layout.      |
| `2`       | Usage error (missing argument, bad `--format` value) — from argparse.|

On error, a concise message is written to **stderr** (prefixed `klt layers:`)
and nothing is written to stdout. No Python traceback is printed.
