# `klt render`

Render one PNG image per non-empty layer of a GDSII or OASIS layout stream,
built on the same layer enumeration as [`klt layers`](layers.md).

```
klt render <file> [-o/--output DIR] [--width N] [--height N] [--background #rrggbb] [--top <cell>] [--format text|json]
```

- `<file>` — path to a GDSII (`.gds`) or OASIS (`.oas`) file. KLayout
  auto-detects the stream format on read; the extension is not authoritative.
- `-o`/`--output` — output directory for the rendered PNGs. Defaults to a
  `renders/` subdirectory next to `<file>` (see "Output path" below).
- `--width`, `--height` — image dimensions in pixels (default `1024x768`).
  Both must be positive.
- `--background` — canvas color as a `#rrggbb`/`#rgb` hex string (default
  `#ffffff`).
- `--top` — top cell to render when the stream has more than one; omit to
  render every top cell (today's default, unchanged). The underlying
  per-layer shape counts (from `klt layers`) and the rendered geometry
  itself are both scoped to that cell's own hierarchy. A named cell absent
  from the stream exits `1` with a clean error.
- `--format` — `text` (default, a human-readable table) or `json`.

The command runs fully headless via KLayout's `klayout.lay.LayoutView`, which
renders offscreen without a GUI, Qt display, or X server (no `xvfb` needed)
— safe to run in CI.

## Output path

Without `-o`/`--output`, PNGs are written to a `renders/` directory next to
the input file: `<file-dir>/renders/`. This is a deliberate, predictable
convention — a layout at `<block>/output/<name>.gds` (the path the gallery
content pipeline, [epic #13](https://github.com/2AMLogic/klayout-tools/issues/13),
is expected to write canary-block layouts to) renders to
`<block>/output/renders/` with no block-specific logic required downstream.

Each layer's PNG is named `<layer>_<datatype>.png` (e.g. `67_20.png`) —
deterministic and parseable without consulting the JSON output. An
all-layers composite is also written as `overview.png` — the "what does this
block look like" image (gallery thumbnails, agent quick-looks). Re-rendering
into the same directory removes any stale `<layer>_<datatype>.png` /
`overview.png` files this command previously wrote (e.g. from a layer that
no longer appears in the design) without touching unrelated files in that
directory.

## Which layers get rendered

Every layer `klt layers` reports is listed in the output, but only layers
with `shapes > 0` are actually rendered — an isolated render of a declared-
but-empty layer is a blank image, which carries no information worth the
render cost. Skipped layers report `"rendered": false, "path": null`.

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
  "output_dir": "design_dir/renders",
  "width": 1024,
  "height": 768,
  "background": "#ffffff",
  "overview": "design_dir/renders/overview.png",
  "layer_count": 3,
  "rendered_count": 2,
  "layers": [
    {
      "layer": 1,
      "datatype": 0,
      "name": "metal1",
      "shapes": 2,
      "annotation": false,
      "path": "design_dir/renders/1_0.png",
      "rendered": true
    },
    {
      "layer": 5,
      "datatype": 0,
      "name": "empty",
      "shapes": 0,
      "annotation": false,
      "path": null,
      "rendered": false
    },
    {
      "layer": 66,
      "datatype": 20,
      "name": null,
      "shapes": 3,
      "annotation": false,
      "path": "design_dir/renders/66_20.png",
      "rendered": true
    }
  ]
}
```

### Top-level fields

| Field             | Type            | Description                                                        |
| ----------------- | --------------- | -------------------------------------------------------------------- |
| `schema_version`  | integer         | Version of this command's JSON shape (starts at `1`; per-command). |
| `file`            | string          | The input path exactly as provided on the command line.            |
| `output_dir`      | string          | The resolved output directory PNGs were written into.              |
| `width`           | integer         | Image width in pixels, as requested (or the default).              |
| `background`      | string          | Canvas color, as requested (or the default `#ffffff`).             |
| `overview`        | string          | Path to the all-layers composite PNG (`overview.png`).             |
| `height`          | integer         | Image height in pixels, as requested (or the default).             |
| `layer_count`     | integer         | Number of entries in `layers` (matches `klt layers`' `layer_count`). |
| `rendered_count`  | integer         | Number of `layers[]` entries with `rendered: true`.                |
| `layers`          | array\<object\> | One entry per layer in the stream's layer table (see below).       |

### `layers[]` entries

| Field      | Type             | Description                                                                 |
| ---------- | ---------------- | --------------------------------------------------------------------------- |
| `layer`    | integer          | Layer number.                                                               |
| `datatype` | integer          | Datatype number.                                                            |
| `name`     | string \| null   | Layer name carried in the stream, or `null` for unnamed layers.             |
| `shapes`   | integer          | Shape count for this layer (same semantics as `klt layers`).                |
| `annotation` | boolean        | `true` when `(layer, datatype)` falls in the reserved annotation range (same semantics as `klt layers` — see [`docs/cli/layers.md`](layers.md) → "Semantics and guarantees"). |
| `path`     | string \| null   | Path to the rendered PNG, or `null` if this layer was not rendered.         |
| `rendered` | boolean          | `true` if a PNG was written for this layer (`shapes > 0`).                  |

## Text format

The default `text` format prints the file/output metadata followed by an
aligned table. It is intended for human eyes and its exact layout is **not**
part of the contract — parse the JSON instead.

```
$ klt render design.gds
file: design.gds
output_dir: design_dir/renders
size: 1024x768
layers: 3
rendered: 2

layer  datatype  name    shapes  annotation  path
-----  --------  ------  ------  ----------  ------------------------------
    1         0  metal1       2  -           design_dir/renders/1_0.png
    5         0  empty        0  -           -
   66        20  -            3  -           design_dir/renders/66_20.png
```

## Exit codes and errors

| Exit code | Meaning                                                              |
| --------- | --------------------------------------------------------------------- |
| `0`       | Success — report written to stdout, PNGs written to `output_dir`.     |
| `1`       | The file is missing, unreadable, not a recognisable layout, `--top` names a cell absent from the stream, `--width`/`--height` is not positive, or `--background` is not a valid hex color. |
| `2`       | Usage error (missing argument, bad `--format` value) — from argparse. |

On error, a concise message is written to **stderr** and nothing is written to
stdout (and no PNGs are written). No Python traceback is printed.

- `--format text` (default): a plain-text line prefixed `klt render:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "render", "message": "file not found: missing.gds" } }
  ```
