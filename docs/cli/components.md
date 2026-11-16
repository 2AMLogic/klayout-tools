# `klt components`

Report **which shapes form one electrically connected geometric component**
across a caller-selected set of conductor and via layers -- no PDK deck, no
device recognition.

```
klt components <file> --conductors <json> [--vias <json>] [--label-layers <json>] [--region <json>] [--top <cell>] [--probe <spec>] [--format text|json]
```

- `<file>` -- path to a GDSII (`.gds`) or OASIS (`.oas`) file. KLayout
  auto-detects the stream format on read; the extension is not authoritative.
- `--conductors` -- **required**. The conductor layer set, as a path to a
  JSON file or an inline JSON array of `{"name": str, "layer": [layer,
  datatype]}` objects, e.g. `'[{"name": "m1", "layer": [68, 20]}, {"name":
  "m2", "layer": [69, 20]}]'`. `name` must be unique. Same-layer touching
  shapes on one conductor always merge into one component, regardless of
  `--vias`. Not validated by argparse -- an empty/malformed value exits `1`
  with a clean error rather than argparse's usage-error exit `2`. Each entry
  (and each `--vias` entry) may also carry the boolean qualifiers described
  under ["Boolean qualifiers"](#boolean-qualifiers-and--not).
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
  as `{"name": str, "layer": [layer, datatype], "conductor": str}` objects.
  `conductor` is optional and, when given, must name one of the
  `--conductors` entries: that label layer's texts are then matched *only*
  against that conductor's own shapes in a component, so a text sitting on a
  lower conductor's pin/label layer is never attributed to a different,
  upper-layer component whose geometry merely crosses over it in XY with no
  via joining them (issue #1579). Omitting `conductor` keeps the original
  any-layer behaviour for that label layer: its texts are matched against
  every conductor/via layer present in a component -- this is the
  backward-compatible default and is unaffected by this scoping. A matched
  text is reported in that component's `labels` list. Omit `--label-layers`
  entirely to skip name/label/pin detection.
- `--region` -- optional. A crop window as an inline JSON array of four
  micrometre coordinates `[left, bottom, right, top]` (e.g. `'[0, 0, 100,
  100]'`). Every conductor/via/label-layer shape is clipped to this window
  before components are computed. Omit to report every shape on the given
  layers. `right > left` and `top > bottom` are enforced.
- `--top` -- optional. The top cell to report on when the stream has more
  than one; omit to report every top cell. A named cell that is absent exits
  `1`.
- `--probe` -- optional, repeatable. A point-to-component lookup as
  `'name=<id>,layer=<conductor>,x=<um>,y=<um>'` -- see
  ["Point-to-component probes"](#point-to-component-probes---probe-issue-5552)
  below.
- `--format` -- `text` (default, a human-readable summary) or `json`.

## Boolean qualifiers (`and` / `not`)

A raw layer cannot say "this via only inside marker X" or "poly minus the
resistor-body marker". Every `--conductors` and `--vias` entry therefore
accepts two optional keys, each a non-empty list of `[layer, datatype]` pairs:

- `"and": [[l, d], ...]` -- keep only the part of the entry's layer that
  overlaps the merged union of the listed layers.
- `"not": [[l, d], ...]` -- remove the merged union of the listed layers from it.

With both present, `and` is applied first, then `not`. The boolean runs before
the `--region` clip and before connectivity is computed, so components,
`shape_count` and `area_um2` all report the qualified geometry. A qualifier
layer absent from the layout is empty: `and` then empties the entry, `not` is
a no-op. Malformed qualifiers (non-list, empty, bad pair, boolean entries)
exit `1`.

Example -- a MIM capacitor whose top-plate vias (marker `75/0`) must not
short the bottom plate:

```json
[
  {"name": "via2_routing",  "layer": [38, 0], "not": [[75, 0]], "between": ["m2", "m3"]},
  {"name": "via2_topplate", "layer": [38, 0], "and": [[75, 0]], "between": ["fusetop", "m3"]}
]
```

Resistor body: `{"name": "poly", "layer": [66, 20], "not": [[110, 0]]}` splits a
marked body into its two terminal ends.

**Sliver caveat:** these are plain booleans, not "interacting" selections. A
shape straddling a qualifier layer's edge is cut at that edge, leaving
slivers on each side; only fully-inside or fully-outside shapes behave as
whole shapes.

The `conductors[]`/`vias[]` echo includes `and`/`not` only for entries that set
them; requests without qualifiers produce byte-identical output.

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

## Point-to-component probes (`--probe`, issue #5552)

Ask which component a point lands on, answered on the very connectivity graph
this report is built from:

```bash
klt components assembled.gds \
  --conductors '[{"name": "li", "layer": [67, 20]}, {"name": "m1", "layer": [68, 20]}]' \
  --vias '[{"name": "mcon", "layer": [67, 44], "between": ["li", "m1"]}]' \
  --probe 'name=vdd_pad_a,layer=m1,x=12.4,y=-4.0' \
  --probe 'name=vdd_pad_b,layer=m1,x=40.0,y=-4.0' \
  --format json
```

`--probe` is repeatable; every key (`name`, `layer`, `x`, `y`) is required and
`name` must be unique. `x`/`y` are micrometres in the top cell's frame and may
be negative. This is the **same spec grammar** `klt extract --probe` takes --
with one difference: `layer` here is the `name` of one `--conductors` entry,
not a deck layer role, because this verb has no deck.

**Why `bbox_um` is not enough.** Two interleaved nets running the length of a
cell have almost identical bounding boxes, and `labels` only helps where the
point of interest carries its own unique text (a sub-block pad usually does
not). Clipping with `--region` around the point loses the component's
whole-layout identity. A probe returns the id of the component the report
itself lists, so **two pads prove they are one island when they share
`component_id`** -- which is what catches a pad that is geometrically placed
but not electrically joined. Extraction + LVS reports that only as a net
mismatch; a probe names the pad.

**Not found is not an error.** `found: false` with `component_id: null` is the
answer for a point on empty space, a point on a *different* conductor only
(the named conductor is what is probed, so an unrelated net crossing overhead
on another layer never matches), and a point outside the `--region` crop. The
command still exits `0`, so a caller can assert on `probes[]` directly.

**On a polygon edge.** The underlying hit test is *inclusive*: a point
exactly on a shape's edge resolves to that shape's component, and so does a
point one database unit outside it (two database units out is `found:
false`). A probe answers "which island is here", not "is this coordinate
inside that polygon" -- use `klt drc` for geometry assertions at that
precision.

**Hierarchy.** Every layer is flattened into the top cell's frame before
connectivity is computed (the same `begin_shapes_rec` flattening the rest of
this report uses), so a conductor shape drawn inside an instanced child cell
is part of a top-cell component and a probe at that point returns that
component's id. There are no subcircuits in this graph to disagree with.

**One top cell.** `component_id` is `"<cell>:<index>"`, scoped to one cell's
component numbering, so `--probe` needs a single top cell in scope: on a
multi-top stream, pass `--top` (the error says so).

**JSON.**

```json
{
  "probes": [
    { "name": "vdd_pad_a", "layer": "m1", "x_um": 12.4, "y_um": -4.0,
      "found": true, "component_id": "TOP:3" },
    { "name": "vdd_pad_b", "layer": "m1", "x_um": 40.0, "y_um": -4.0,
      "found": false, "component_id": null }
  ]
}
```

**Compared with `klt extract --probe`.** That flag answers point-to-*net* on a
device-extraction deck (`--deck sky130|gf180mcu`; see
[`docs/cli/extract.md`](extract.md#point-to-net-probes---probe-issue-2986)). Where
a deck exists and purge semantics are acceptable, it is the richer answer --
it names the net. This flag is the deck-free equivalent: it takes an arbitrary
conductor/via stack with `and`/`not` qualifiers, and because this engine never
purges, a floating island still has a `component_id` (the extractor would have
removed that net and reported `found: false`).

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
  "label_layers": [
    { "name": "m1pin", "layer": [68, 5], "conductor": "m1" }
  ],
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
  ],
  "probes": []
}
```

### Top-level fields

| Field             | Type                     | Description                                                                                     |
| ----------------- | ------------------------ | ------------------------------------------------------------------------------------------------ |
| `schema_version`  | integer                  | Version of this command's JSON shape (starts at `1`).                                            |
| `file`            | string                   | The input layout path exactly as provided on the command line.                                  |
| `conductors`      | array\<object\>          | The `--conductors` set echoed back, as `{name, layer}` (plus `and`/`not` when set).                                          |
| `vias`            | array\<object\>          | The `--vias` set echoed back, as `{name, layer, between}` (plus `and`/`not` when set); `[]` when `--vias` was omitted.       |
| `label_layers`    | array\<object\>          | The `--label-layers` set echoed back, as `{name, layer, conductor}` (`conductor` is `null` when that entry didn't declare one); `[]` when omitted.  |
| `region_um`       | array\<number\> \| null  | The `--region` crop window `[left, bottom, right, top]` in micrometres, or `null` when omitted.  |
| `top`             | string \| null           | The `--top` cell name, or `null` when every top cell was reported.                               |
| `dbu_um`          | number (float)           | The input layout's database unit in micrometres, same semantics as `klt layers`.                 |
| `component_count` | integer                  | `len(components)`.                                                                                |
| `components`      | array\<object\>          | One entry per connected component. Sorted by `(cell, bbox.left, bbox.bottom, bbox.right, bbox.top)` for deterministic output. |
| `probes`          | array\<object\>          | Additive field (issue #5552). One entry per `--probe`, in request order; always present, `[]` when none was given. See ["`probes[]` entries"](#probes-entries) below. |

### `components[]` entries

| Field                   | Type             | Description                                                                                              |
| ----------------------- | ----------------- | ---------------------------------------------------------------------------------------------------------- |
| `id`                    | string            | Stable identifier, `"<cell>:<index>"`, assigned after the deterministic sort -- stable across identical-input runs, not tied to the engine's internal cluster order. |
| `cell`                  | string            | The top cell this component was found in.                                                                |
| `bbox_um`               | object            | `{left, bottom, right, top}` in micrometres -- the component's physical bounding box.                    |
| `conductors`            | array\<object\>   | `{name, layer, shape_count, area_um2}` for every conductor that contributes at least one shape to this component. A conductor with zero shapes here never appears. |
| `vias`                  | array\<object\>   | Same shape as `conductors`, for every via present in this component. `[]` when no via landed on it.       |
| `labels`                | array\<string\>   | Sorted, de-duplicated text strings from any `--label-layers` entry whose text touches this component's geometry. When a `--label-layers` entry declares `conductor`, only that conductor's own shapes in this component are considered (a text on a different conductor's layer that merely overlaps in XY, with no via joining them, is never attributed here); a `--label-layers` entry with no `conductor` matches against every conductor/via layer present in the component. `[]` when no `--label-layers` were given or none touched. |
| `touches_crop_boundary` | boolean           | `true` when this component's (clipped) bounding box touches the `--region` crop window's edge -- it may continue outside the cropped view. Always `false` when `--region` was omitted. |

### `probes[]` entries

| Field          | Type             | Description                                                                                      |
| -------------- | ---------------- | ------------------------------------------------------------------------------------------------ |
| `name`         | string           | The probe's `name` as given, echoed back. Unique within one run.                                 |
| `layer`        | string           | The probed conductor's `name` as given -- one of `conductors[].name`.                            |
| `x_um`         | number (float)   | The probe's `x` as given, in micrometres in the top cell's frame.                                |
| `y_um`         | number (float)   | The probe's `y` as given, in micrometres in the top cell's frame.                                |
| `found`        | boolean          | `true` exactly when some `components[]` entry owns a shape of the named conductor at that point. `false` for empty space, for a point on a different conductor only, and for a point outside the `--region` crop -- never an error. |
| `component_id` | string \| null   | The `components[].id` of the component owning that shape, or `null` when `found` is `false`. Always resolvable in this same report's `components[]`. |

## Exit codes

| Code | Meaning                                                                                                    |
| ---- | ------------------------------------------------------------------------------------------------------------ |
| `0`  | Ran successfully -- the components report was produced (even an empty one; this is a report, not a pass/fail check). |
| `1`  | Failed to run -- bad layout file, malformed `--conductors`/`--vias`/`--label-layers`, an unknown conductor name referenced by a via, by a `--label-layers` entry's `conductor` field or by a `--probe`'s `layer`, unknown `--top` cell, malformed `--region`, a malformed `--probe` spec (missing/unknown/repeated key, non-numeric coordinate, duplicate `name`), or `--probe` with more than one top cell in scope. |
| `2`  | Usage error (missing argument, bad `--format` value) -- from argparse.                                        |

Per [`docs/json-contract.md`](../json-contract.md#exit-codes)'s additive
contract, a future release may still add a code above `2`; a consumer
should gate on the payload's own fields rather than assume the codes above
are exhaustive forever.

On error (exit `1`), a concise message is written to **stderr** and nothing is
written to stdout. No Python traceback is printed.

- `--format text` (default): a plain-text line prefixed `klt components:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "components", "message": "conductors must contain at least one entry" } }
  ```
