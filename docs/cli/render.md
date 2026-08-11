# `klt render`

Render one PNG image per non-empty layer of a GDSII or OASIS layout stream,
built on the same layer enumeration as [`klt layers`](layers.md).

```
klt render <file> [-o/--output DIR] [--width N] [--height N] [--background #rrggbb] [--top <cell>] [--layers <json>] [--bbox xmin,ymin,xmax,ymax] [--format text|json]
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
- `--layers` — restrict rendering to a caller-supplied layer set: a path to
  a JSON file, or an inline JSON array, of `[layer, datatype]` pairs (e.g.
  `'[[67, 20], [66, 44]]'`) — the same convention `klt ring-check --layers`
  / `klt precheck --allowed-layers` use. Omit to render every non-empty
  layer (today's default, unchanged). Layers outside the set still appear
  in `layers[]` with `"rendered": false, "path": null`, exactly like a
  declared-but-empty layer; the all-layers `overview.png` composite is
  restricted to the selection too, so it doesn't leak unrequested geometry.
- `--bbox` — crop the render to a physical window: four comma-separated
  micrometre coordinates `xmin,ymin,xmax,ymax` (e.g. `'0,0,50,50'`). Omit to
  fit the whole layout (today's default, unchanged). The requested window's
  physical aspect ratio is preserved — the viewport is padded on the
  shorter axis to match `--width`/`--height` rather than stretching the
  image — so pixels outside the requested extent are simply absent, not
  distorted. See `actual_extent` below for the extent actually rendered
  after that padding.
- `--format` — `text` (default, a human-readable table) or `json`.

`--layers` and `--bbox` may be used independently or together, and compose
with `--top` (each narrows what gets drawn/considered, in any combination).

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
render cost. Skipped layers report `"rendered": false, "path": null`. When
`--layers` is given, a layer outside the requested set is skipped the same
way, even if it has shapes.

## Physical extent (`--bbox`)

Coordinates are in micrometres, matching every other `klt` flag that takes a
physical position (e.g. `klt ring-check --region`). Both `requested_bbox`
(the window as given) and `actual_extent` (the window actually rendered,
after aspect-ratio padding) use the same `{left, bottom, right, top}` shape
as `klt cells`' `bbox_um` — see "JSON schema" below.

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
  "requested_layers": null,
  "requested_bbox": null,
  "actual_extent": {"left": 0.0, "bottom": 0.0, "right": 0.03, "top": 0.03},
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
| `requested_layers` | array\<[int,int]\> \| null | The `--layers` selection as `[layer, datatype]` pairs, or `null` when `--layers` was omitted. |
| `requested_bbox`  | object \| null  | The `--bbox` window as given (micrometres, `{left, bottom, right, top}`), or `null` when `--bbox` was omitted. |
| `actual_extent`   | object          | The viewport actually rendered (micrometres, `{left, bottom, right, top}`) — the whole-layout fit when `--bbox` is omitted, or the `--bbox` window padded to `--width`/`--height`'s aspect ratio when given. |
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
actual_extent: (0.0,0.0)-(0.03,0.03)

layer  datatype  name    shapes  annotation  path
-----  --------  ------  ------  ----------  ------------------------------
    1         0  metal1       2  -           design_dir/renders/1_0.png
    5         0  empty        0  -           -
   66        20  -            3  -           design_dir/renders/66_20.png
```

`requested_layers`/`requested_bbox` lines are only printed when the
corresponding flag was given, e.g.:

```
$ klt render design.gds --layers '[[1, 0]]' --bbox 0,0,5,5
...
requested_layers: 1/0
requested_bbox: (0.0,0.0)-(5.0,5.0)
actual_extent: (0.0,0.0)-(5.0,5.0)
```

## Exit codes and errors

| Exit code | Meaning                                                              |
| --------- | --------------------------------------------------------------------- |
| `0`       | Success — report written to stdout, PNGs written to `output_dir`.     |
| `1`       | The file is missing, unreadable, not a recognisable layout, `--top` names a cell absent from the stream, `--width`/`--height` is not positive, `--background` is not a valid hex color, `--layers` is malformed/empty, or `--bbox` is malformed or has `xmax <= xmin`/`ymax <= ymin`. |
| `2`       | Usage error (missing argument, bad `--format` value) — from argparse. |

On error, a concise message is written to **stderr** and nothing is written to
stdout (and no PNGs are written). No Python traceback is printed.

- `--format text` (default): a plain-text line prefixed `klt render:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "render", "message": "file not found: missing.gds" } }
  ```
