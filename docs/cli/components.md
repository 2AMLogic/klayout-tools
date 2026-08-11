# `klt components`

Report **which shapes form one electrically connected geometric component**
across a caller-selected set of conductor and via layers -- no PDK deck, no
device recognition.

```
klt components <file> --conductors <json> [--vias <json>] [--label-layers <json>] [--region <json>] [--top <cell>] [--format text|json]
```

- `<file>` -- path to a GDSII (`.gds`) or OASIS (`.oas`) file. KLayout
  auto-detects the stream format on read; the extension is not authoritative.
- `--conductors` -- **required**. The conductor layer set, as a path to a
  JSON file or an inline JSON array of `{"name": str, "layer": [layer,
  datatype]}` objects, e.g. `'[{"name": "m1", "layer": [68, 20]}, {"name":
  "m2", "layer": [69, 20]}]'`. `name` must be unique. Same-layer touching
  shapes on one conductor always merge into one component, regardless of
  `--vias`. Not validated by argparse -- an empty/malformed value exits `1`
  with a clean error rather than argparse's usage-error exit `2`.
- `--vias` -- optional. A via mapping that electrically joins two named
  conductor levels, as a path to a JSON file or an inline JSON array of
  `{"name": str, "layer": [layer, datatype], "between": [conductor_name_a,
  conductor_name_b]}` objects, e.g. `'[{"name": "mcon", "layer": [67, 44],
  "between": ["li", "m1"]}]'`. `name` must be unique; `between` must name two
  *different* `--conductors` entries. A via joins two components only where
  its own shapes actually land on both -- see
  ["Overlap vs. connection"](#overlap-vs-connection-what-this-engine-actually-asserts)
  below. Omit for a purely same-layer connectivity report.
- `--label-layers` -- optional. GDS text layers to scan for names/labels/pins,
  same `{"name": str, "layer": [layer, datatype]}` shape as `--conductors`
  (no `between`). Any text whose location touches a component's geometry (on
  any of its conductor/via layers) is reported in that component's `labels`
  list. Omit to skip name/label/pin detection.
- `--region` -- optional. A crop window as an inline JSON array of four
  micrometre coordinates `[left, bottom, right, top]` (e.g. `'[0, 0, 100,
  100]'`). Every conductor/via/label-layer shape is clipped to this window
  before components are computed. Omit to report every shape on the given
  layers. `right > left` and `top > bottom` are enforced.
- `--top` -- optional. The top cell to report on when the stream has more
  than one; omit to report every top cell. A named cell that is absent exits
  `1`.
- `--format` -- `text` (default, a human-readable summary) or `json`.

## Why this exists

`klt extract` answers a device/netlist question using a curated,
PDK-specific `ExtractionDeck` -- it is not a convenient fit for inspecting
unnamed, device-free metal, annotation geometry, an incomplete layout, or a
deliberately limited subset of layers (issue #674). Today that requires a
hand-rolled KLayout `Region`/`LayoutToNetlist` script with hand-coded
conductor/via interaction semantics. `klt components` generalises the same
connectivity engine `klt extract`'s internal extraction already uses (`l2n.
connect(metal, via)` / `l2n.connect(via)` / `l2n.connect(via, next_metal)`)
behind a caller-supplied, ad-hoc layer/via mapping instead of a hardcoded
deck, so **no device recognition and no named PDK deck is ever required**.

## Overlap vs. connection: what this engine actually asserts

**Two shapes that merely overlap in XY on different layers are never treated
as connected.** `kdb.LayoutToNetlist`'s `connect()` graph is purely
declarative: a conductor pair is joined only when

- they are the **same conductor** and their shapes touch (a same-layer
  merge, declared automatically for every `--conductors` entry), or
- an explicit **via** is declared `between` them (via `--vias`) *and* that
  via's own shapes actually land on both conductors at the same location.

A via shape that only touches one side (or neither) contributes no join. Two
different conductor levels that happen to cross in the XY plane, with no via
declared between them, always stay separate components -- this is the
load-bearing distinction a plain `Region` union cannot make.

This engine never calls a device extractor and never purges "floating" nets:
an isolated, unnamed, device-free island with no via and no label still
survives as its own reported component.

## JSON schema (the contract)

**JSON is the API.** Human-readable text output is a courtesy; the JSON
schema below is the stable contract. Per the project's rules, **breaking**
(renaming, removing, or retyping) **a field is a breaking change**. New
fields may be added without breaking the contract, so consumers should
ignore unknown fields. See [`docs/json-contract.md`](../json-contract.md) for
the envelope shared across all `klt` commands (`schema_version`, error shape,
exit codes).

```json
{
  "schema_version": 1,
  "file": "stack.gds",
  "conductors": [
    { "name": "m1", "layer": [68, 20] },
    { "name": "m2", "layer": [69, 20] }
  ],
  "vias": [
    { "name": "via1", "layer": [70, 20], "between": ["m1", "m2"] }
  ],
  "label_layers": [],
  "region_um": null,
  "top": null,
  "dbu_um": 0.005,
  "component_count": 1,
  "components": [
    {
      "id": "TOP:0",
      "cell": "TOP",
      "bbox_um": { "left": 0.0, "bottom": 0.0, "right": 100.0, "top": 100.0 },
      "conductors": [
        { "name": "m1", "layer": [68, 20], "shape_count": 1, "area_um2": 20.0 },
        { "name": "m2", "layer": [69, 20], "shape_count": 1, "area_um2": 20.0 }
      ],
      "vias": [
        { "name": "via1", "layer": [70, 20], "shape_count": 1, "area_um2": 1.0 }
      ],
      "labels": [],
      "touches_crop_boundary": false
    }
  ]
}
```

### Top-level fields

| Field             | Type                     | Description                                                                                     |
| ----------------- | ------------------------ | ------------------------------------------------------------------------------------------------ |
| `schema_version`  | integer                  | Version of this command's JSON shape (starts at `1`).                                            |
| `file`            | string                   | The input layout path exactly as provided on the command line.                                  |
| `conductors`      | array\<object\>          | The `--conductors` set echoed back, as `{name, layer}`.                                          |
| `vias`            | array\<object\>          | The `--vias` set echoed back, as `{name, layer, between}`; `[]` when `--vias` was omitted.       |
| `label_layers`    | array\<object\>          | The `--label-layers` set echoed back, as `{name, layer}`; `[]` when omitted.                     |
| `region_um`       | array\<number\> \| null  | The `--region` crop window `[left, bottom, right, top]` in micrometres, or `null` when omitted.  |
| `top`             | string \| null           | The `--top` cell name, or `null` when every top cell was reported.                               |
| `dbu_um`          | number (float)           | The input layout's database unit in micrometres, same semantics as `klt layers`.                 |
| `component_count` | integer                  | `len(components)`.                                                                                |
| `components`      | array\<object\>          | One entry per connected component. Sorted by `(cell, bbox.left, bbox.bottom, bbox.right, bbox.top)` for deterministic output. |

### `components[]` entries

| Field                   | Type             | Description                                                                                              |
| ----------------------- | ----------------- | ---------------------------------------------------------------------------------------------------------- |
| `id`                    | string            | Stable identifier, `"<cell>:<index>"`, assigned after the deterministic sort -- stable across identical-input runs, not tied to the engine's internal cluster order. |
| `cell`                  | string            | The top cell this component was found in.                                                                |
| `bbox_um`               | object            | `{left, bottom, right, top}` in micrometres -- the component's physical bounding box.                    |
| `conductors`            | array\<object\>   | `{name, layer, shape_count, area_um2}` for every conductor that contributes at least one shape to this component. A conductor with zero shapes here never appears. |
| `vias`                  | array\<object\>   | Same shape as `conductors`, for every via present in this component. `[]` when no via landed on it.       |
| `labels`                | array\<string\>   | Sorted, de-duplicated text strings from any `--label-layers` entry whose text touches this component's geometry. `[]` when no `--label-layers` were given or none touched. |
| `touches_crop_boundary` | boolean           | `true` when this component's (clipped) bounding box touches the `--region` crop window's edge -- it may continue outside the cropped view. Always `false` when `--region` was omitted. |

## Exit codes

| Code | Meaning                                                                                                    |
| ---- | ------------------------------------------------------------------------------------------------------------ |
| `0`  | Ran successfully -- the components report was produced (even an empty one; this is a report, not a pass/fail check). |
| `1`  | Failed to run -- bad layout file, malformed `--conductors`/`--vias`/`--label-layers`, an unknown conductor name referenced by a via, unknown `--top` cell, or malformed `--region`. |
| `2`  | Usage error (missing argument, bad `--format` value) -- from argparse.                                        |

On error (exit `1`), a concise message is written to **stderr** and nothing is
written to stdout. No Python traceback is printed.

- `--format text` (default): a plain-text line prefixed `klt components:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "components", "message": "conductors must contain at least one entry" } }
  ```
