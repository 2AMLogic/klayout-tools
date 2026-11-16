# `klt route`

Route between **labelled pins of an already-placed layout**. Given a layout
whose top cell instantiates cells, `klt route` derives each endpoint from a text
label inside a direct child instance, routes the net with the `klt gen-compose`
router (unchanged), optionally writes the routed layout, and reports every
positive-area overlap between a drawn route and foreign metal.

Slice 1 of the design in
[`docs/design/route-labelled-pins-spike.md`](../design/route-labelled-pins-spike.md):
two-pin nets, one routing layer, pins on direct child instances of the top cell.

```
klt route <request.json> [--format text|json]
```

Like `klt gen-compose` and `klt lvs`, the input is a request document. Relative
paths resolve against the request file's directory.

## Request

```json
{
  "layout": { "gds_path": "top.gds", "top_cell": "chip_top" },
  "pdk": { "variant": "sky130A" },
  "routing": { "layer_role": "metal2", "width_um": 0.6 },
  "nets": [
    {
      "net": "VREF_LINK",
      "pins": [
        { "cell": "adc_0", "label": "VREF", "layers": [{ "layer": 68, "datatype": 5 }] },
        { "cell": "ldo_0", "label": "VOUT", "layers": [{ "layer": 68, "datatype": 5 }],
          "direction_deg": 180 }
      ]
    }
  ],
  "options": { "overlap_check": "report", "output": "top_routed.gds" }
}
```

- `layout.top_cell` is optional when the layout has exactly one top cell.
- `routing.layer_role` is a `klt gen` layer role (the same names
  `gen-compose`'s `routing.layer_role` accepts); `width_um` must clear the
  deck's minimum width.
- `nets[].pins` has **exactly two** entries. Each is a label resolved with the
  same label-to-net search as `gen-compose`'s `ports_from_labels`; `layers`
  lists the label layers, and the optional `access` object is the
  `ports_from_labels` landing-search block. `direction_deg` (0/90/180/270,
  optional, default 0) is the direction the route leaves the pin.
- `pins[].cell` names a **direct child instance of the top cell**. GDS stores no
  instance names, so an instance is addressed by its master cell name when only
  one direct instance uses that master, and always as `<master>#<k>` (`k` = 0-based
  position among that master's instances). A bare name shared by several
  instances fails with `error.code: "ambiguous_instance"`.
- `options.overlap_check`: only `"report"` (the default) is supported in this
  slice. `options.output` is optional; omitted means a dry run that routes and
  reports without writing a layout. The output is the input layout plus the
  routed metal on the top cell; instances are untouched.

Instances placed at R90/R270 (or mirrored+rotated), magnified, or as arrays are
rejected when a pin names them (`error.code: "unsupported_orientation"`); the
supported transforms are none, mirror-x, mirror-y and rotate-180.

## Report

Emitted through the shared envelope (`docs/json-contract.md`).

```json
{
  "schema_version": 1,
  "status": "clean | unrouted | overlaps",
  "layout": { "gds_path": "top.gds", "top_cell": "chip_top" },
  "output": { "gds_path": "top_routed.gds", "cell_name": "chip_top" },
  "dbu_um": 0.001,
  "routes": [
    { "net": "VREF_LINK", "status": "routed",
      "layer": { "layer": 68, "datatype": 20 }, "width_um": 0.6, "length_um": 184.2,
      "points_um": [[12.0, 40.0], [12.0, 90.0], [196.2, 90.0]],
      "endpoints": [ { "cell": "adc_0", "label": "VREF", "x_um": 12.0, "y_um": 40.0 },
                     { "cell": "ldo_0", "label": "VOUT", "x_um": 196.2, "y_um": 90.0 } ] }
  ],
  "unrouted_nets": [ { "net": "X", "reason": "..." } ],
  "overlaps": [
    { "net": "VREF_LINK", "layer": { "layer": 68, "datatype": 20 },
      "foreign_owner": { "cell": "adc_0#0", "net": null },
      "overlap_bbox_um": { "x0": 90.0, "y0": 89.7, "x1": 95.4, "y1": 90.3 },
      "overlap_area_um2": 3.24 }
  ]
}
```

`output` is `null` on a dry run. `status` is the verdict of record: `overlaps`
when `overlaps[]` is non-empty (even if some other net is also unrouted),
else `unrouted` when `unrouted_nets[]` is non-empty, else `clean`.

### Foreign geometry

For each routed net, the drawn footprint (backbone, via-drop pads on the route
layer, stub-widen boxes -- the primitives `gen-compose` draws) is intersected,
on the route layer, with the flattened layout **minus the routed net's own
connected component** (the `LayoutToNetlist` extraction `ports_from_labels`
uses). Touching is not an overlap; positive area is. Footprints of nets already
routed earlier in the same request count as foreign too (`foreign_owner.cell:
null`, `net` = that net).

`foreign_owner.cell` is the instance id containing the shape, or `null` for
top-level geometry; `net` is the extractor's net name, or `null` when it has none.

This is a diagnostic like the router's own checks; `klt drc` remains
authoritative for spacing and rule compliance. Spacing violations are not
reported in this slice. Only the route layer is checked (landing pads on other
layers are not).

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | every net routed, no overlaps (`status: "clean"`) |
| 1 | application error: unreadable request/layout, unknown instance or label, ambiguous instance, unsupported orientation, bad request (error envelope) |
| 2 | usage error (argparse) |
| 3 | report emitted, but `unrouted_nets[]` or `overlaps[]` is non-empty |

## Known limits

- The router's obstacle check is **bbox-level**: a third instance whose bounding
  box sits on the straight path is detoured around (or the net is reported
  unrouted), even where its real metal is elsewhere on that layer. Overlaps
  therefore mostly come from top-level geometry, from shapes of the endpoint
  instances themselves, and from earlier nets.
- Connectivity for the overlap pass runs on a flattened scratch copy of the
  layout, so cost scales with the flattened size of the top cell.
- Two-pin nets only; nested instance transforms and N-pin nets are later slices.
