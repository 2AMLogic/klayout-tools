# Spike: `klt route` — routing between placed cells' labelled pins

**Status:** design spike for issue #5200 (split from #5198). Docs only; no
`src/` change. Per `docs/ARCHITECTURE.md` § "How capabilities arrive", a new
routing capability gets a survey, a proposed JSON contract and a build/wrap
decision before implementation. Implementation is filed as child issues.

**Recommendation (one line):** add a thin `klt route` verb that reads an
*already-placed* layout, derives endpoints from text labels, and drives the
existing `gen_compose_routing` routers (`route_two_pin`, later `route_bundle`)
unchanged. Add one new, report-only foreign-geometry overlap pass. Do **not**
write a new routing engine, and do **not** grow `gen-compose`'s request schema.

## The gap, stated precisely

`klt gen-compose` already covers most of "route between labelled pins":
`blocks[].cell` places a library cell, `ports_from_labels` (#2927) derives
ports from in-cell text labels (label to net via `LayoutToNetlist` over the
deck's metal/via stack), `connectivity[]` routes them, and `placement.explicit`
positions cells. What it cannot do is:

1. **Start from an existing layout.** `gen-compose` *places and writes a new
   composed cell*. The caller who already has a top cell with instances placed
   (by a hand script, `klt place-and-route`, a prior `gen-compose`) cannot say
   "route these two labels in place".
2. **Report overlap with foreign geometry.** The routers' checks are
   diagnostic heuristics against the composition's own known geometry: bbox
   obstacles (check 5), and *drawn* shapes only for a self-net's own block
   (`read_block_layer_geometry`, checks 3/4, `_leg_block_spacing_violation_um`).
   No pass answers "which shapes, belonging to any other net or cell, does the
   drawn route touch?" — the hand-written collision check #5198 describes.

## Question 1 — reuse vs new verb

**Answer: thin verb over the existing routers.** The routers are already
decoupled from `compose()`. `route_two_pin(pin_a, pin_b, blocks, offsets_um,
placed_bboxes_um, width_um, route_layer, extraction_deck, block_geometry, …)`
and `route_bundle(pins, blocks, offsets_um, placed_bboxes_um, …)` take plain
dicts: block `ports[]` (`x_um`, `y_um`, `layer`, `direction_deg`), a per-block
offset and bbox, and callbacks (`block_geometry_for`, `leg_conflict`). Nothing
in them reads a request document or writes a GDS; `_write_composed_gds` is the
only drawing step and is separable.

Front end responsibilities (new code, no routing logic):

- Load the layout, select the top cell, and enumerate **direct child
  instances** of the named cells. Each instance becomes one synthetic block:
  `gds_path`/`cell_name` of the child, `offset_um` from the instance
  transform, `bbox_um` from the placed instance bbox. Reuse
  `_load_block_cell` and `read_block_layer_geometry` for geometry.
- Derive endpoints with `derive_label_ports` (`gen_compose_port_access.py`),
  the same label-to-net and optional `access` landing search `gen-compose`
  uses, so a label means the same thing in both verbs. Label layers and names
  come from the request (`pins[].label`, `pins[].layers`).
- Resolve the route layer with `_resolve_route_layer`
  (`gen_compose_routing.py`) and the extraction deck via `get_extraction_deck`,
  exactly as `gen-compose` does.
- Call `route_two_pin`; if it returns a route, draw it with the same
  `kdb.Path`/via-drop construction (`_drawn_leg_footprint_region` already
  defines the footprint, so drawing and checking cannot disagree).

**Why not extend `gen-compose`:** its request is organised around
*constructing* a composition (blocks, placement, connectivity, pins, output
cell). "Operate on this existing layout, leave its cells untouched" is a
different contract (input layout in, same layout plus metal out), and bolting a
mode onto the placement-centred request would make an already ~5.5k-line
routing module's front end ambiguous. A separate verb keeps
`docs/cli/gen-compose.md` stable and mirrors how `klt ring-check` /
`klt keepout-check` are separate verbs over shared geometry code.

**Known limits of reuse (to be verified in slice 1, not assumed):**

- Block orientation is limited to `_ORIENTATION_KDB_ARGS` (`none`, `mirror_x`,
  `mirror_y`, `rotate_180`). Instances placed at R90/R270 must be rejected with
  a structured error in slice 1, not silently mis-transformed.
- Same-block legs rely on the self-net checks (3/4) written for one block's
  pads; when both labels sit in the *same* instance the front end uses that
  path as-is.
- Nested hierarchy: slice 1 considers direct children of the top cell only;
  labels deeper in a child's own hierarchy are found by
  `derive_label_ports` (it extracts the macro), but intermediate transforms
  are out of scope.

## Question 2 — proposed JSON contract

Verb: `klt route <request.json> [--format text|json]`, a request document like
`gen-compose`/`lvs`, emitting through the shared envelope
(`docs/json-contract.md`).

Request:

```json
{
  "layout": { "gds_path": "top.gds", "top_cell": "chip_top" },
  "pdk": { "variant": "sky130A" },
  "routing": { "layer_role": "metal4", "width_um": 0.6 },
  "nets": [
    {
      "net": "VREF_LINK",
      "pins": [
        { "cell": "adc_0",  "label": "VREF",
          "layers": [{ "layer": 71, "datatype": 5 }] },
        { "cell": "ldo_0",  "label": "VOUT",
          "layers": [{ "layer": 71, "datatype": 5 }] }
      ]
    }
  ],
  "options": { "overlap_check": "report", "output": "top_routed.gds" }
}
```

- `layout.top_cell` is optional if the layout has one top cell.
- `pins[].cell` names an instance (not a cell master) so two instances of the
  same master are distinguishable; slice 1 requires exactly two pins per net.
- `options.overlap_check`: `"report"` (default; draw and report),
  `"reject"` (do not draw a net whose route overlaps foreign geometry),
  `"off"`. Slice 1 ships `"report"` only.
- `options.output` is optional; omitted means a dry run that routes and
  reports without writing a layout.

Report:

```json
{
  "schema_version": 1,
  "status": "overlaps",
  "layout": { "gds_path": "top.gds", "top_cell": "chip_top" },
  "output": { "gds_path": "top_routed.gds", "cell_name": "chip_top" },
  "routes": [
    {
      "net": "VREF_LINK",
      "status": "routed",
      "layer": { "layer": 71, "datatype": 20 },
      "width_um": 0.6,
      "length_um": 184.2,
      "points_um": [[12.0, 40.0], [12.0, 90.0], [196.2, 90.0]],
      "endpoints": [
        { "cell": "adc_0", "label": "VREF", "x_um": 12.0, "y_um": 40.0 },
        { "cell": "ldo_0", "label": "VOUT", "x_um": 196.2, "y_um": 90.0 }
      ]
    }
  ],
  "unrouted_nets": [],
  "overlaps": [
    {
      "net": "VREF_LINK",
      "layer": { "layer": 71, "datatype": 20 },
      "foreign_owner": { "cell": "adc_0", "net": "AVDD" },
      "overlap_bbox_um": { "x0": 90.0, "y0": 89.7, "x1": 95.4, "y1": 90.3 },
      "overlap_area_um2": 3.24
    }
  ]
}
```

**Exit codes** (additive, per `docs/json-contract.md` § "Exit codes" and
mirroring `gen-compose`/`klt drc`): `0` every net routed with no overlaps; `1`
application error (no report, error envelope: unreadable layout, unknown
instance/label, unsupported orientation); `2` usage error; `3` a report was
emitted but `unrouted_nets[]` or `overlaps[]` is non-empty. `status` is the
verdict of record (`"clean"`, `"unrouted"`, `"overlaps"`), consumers gate on it.

**Foreign-geometry definition.** For each routed net, build the drawn
footprint with `_drawn_leg_footprint_region` (backbone, via-drop pads,
stub-widen boxes) and intersect it, per layer the leg draws on, with the
flattened layout region on that layer minus the routed net's own shapes. The
net's own shapes are those in the connected component of each endpoint label,
identified with the same `LayoutToNetlist` extraction `derive_label_ports`
already runs (not a second connectivity model). Positive-area intersection is
an overlap; touching is not.

- **Pin on the same cell as the shape (own-cell):** a shape in an endpoint's
  own instance counts as foreign unless it belongs to the endpoint's net. This
  is what catches the route running across the pin's own cell's other metal,
  which bbox-level check 5 deliberately exempts within `own_pin_allowance`.
- **Shape in another instance, or top-level geometry:** always foreign unless
  its net is the routed net (a route legitimately merging into a same-net
  shape is not an overlap).
- Reported per overlapping shape with `foreign_owner` (`cell` for an instance
  shape, `null` for top-level; `net` when the extractor names it, else `null`).

This is a *diagnostic* like the routers' own checks: `klt drc` remains
authoritative for spacing and rule compliance. Slice 1 reports overlap
(positive-area) only; spacing-violation reporting reuses
`_leg_block_spacing_violation_um` in a later slice.

## Question 3 — minimal first slice

**Slice 1:** two-pin nets, a single routing layer (`routing.layer_role`), pins
on direct child instances, `route_two_pin` as the router, report-only overlap
check (`overlap_check: "report"`), JSON + text output, `docs/cli/route.md`,
tests with a small hand-built two-cell fixture (clean route; route crossing a
foreign shape; unknown label; R90 instance rejected).

**Size estimate:** roughly 500–700 lines of new `src` plus about 400 lines of
tests and one CLI doc. Breakdown: request parsing and validation about 150
lines; instance-to-block adapter about 120; overlap pass about 120; drawing
and report assembly about 150. No router changes. Via-drops and multi-layer
routes work only to the extent `route_two_pin` already supports them
(`extraction_deck` supplied); a fixture covering one is a stretch goal.

**Later slices (not filed yet):** N-pin nets via `route_bundle`; `"reject"`
overlap mode; spacing-violation reporting; R90/R270 and nested instance
transforms; a keepout allow-window (tracked in #5198, out of scope here).

## Rejected alternatives

- **A new routing engine** (maze/A*): excluded up front. The routers' value is
  their accumulated checks (rings, self-net shorts, via-drop ladders, track
  assignment); a second engine would have to re-earn all of them.
- **Extending `gen-compose`'s request schema:** see Question 1.
- **Wrapping an external router (e.g. OpenROAD) for cross-block top routing:**
  it routes digital standard-cell netlists, not named analog pin pairs on a
  fixed macro layout, and would not give the foreign-overlap report.

## Open risks for the implementer

- Whether `route_two_pin`'s obstacle check (bbox-level) rejects routes that a
  real-geometry check would accept for large hand-drawn macros; if so slice 1
  may need to pass `navigable_regions`-style exemptions derived from the
  layout rather than bboxes.
- Cost of flattening a large top cell for the overlap pass: restrict to the
  route's inflated bbox via `Region` clipping before the boolean.
