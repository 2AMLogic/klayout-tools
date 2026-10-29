# `klt tap-distance`

Assert that **every point of a subject region lies within a Euclidean
distance of a qualifying tap** -- the latch-up / well-tie-spacing rule shape
("something must be at least this close") -- and report where it is not.

`klt drc` has only width/spacing/enclosure style checks and `klt erc`
`ties[]` answers only "does this well island have a tap wired to net X", with
no distance bound. This verb is the single-purpose geometric assertion for
the distance question. Purely geometric apart from the optional net filter,
which reuses `klt erc`'s connectivity-extraction helpers.

```
klt tap-distance <file> --subject <expr> --taps <expr> --max-distance-um <um>
                 [--island <expr>] [--tap-net <net>]... [--net-spec <json>]
                 [--region <json>] [--top <cell>] [--format text|json]
```

- `--subject` / `--taps` / `--island` -- layer expressions: `<layer>/<datatype>`
  terms joined by `+` (union), `&` (intersection), `-` (difference), applied
  left to right (`65/20 & 125/44`). Hierarchy is flattened.
- `--max-distance-um` -- the allowed Euclidean subject-to-tap distance.
- `--island` -- optional partition region (e.g. a well layer). Each merged
  polygon is an island: the subject is restricted to it and only taps that
  touch or overlap it count (taps are not clipped, so a tap straddling an
  island edge counts for each island it touches). An island with subject
  geometry and **no** qualifying tap **fails** (`no_tap`); an island with no
  subject is reported `no_subject` and ignored. Subject outside every island
  is not checked.
- `--tap-net` (repeatable) / `--net-spec` -- count only taps electrically
  wired to a named net. `--net-spec` is a JSON file with the same shape as
  `klt erc`'s connectivity spec minus ties:
  `{"stackup": [{"name", "layer", "label_layer"}], "vias": [{"name", "layer",
  "between": [a, b]}], "tap_connect_to": "<stackup name>"}`; the tap layer is
  connected to that stackup role (default: `stackup[0]`) and nets are matched
  by label like `klt erc` does.
- `--region` -- optional micrometre clip window `[left, bottom, right, top]`
  applied to the subject.
- `--top` -- top cell when the stream has several.

## Exit codes

| Code | Meaning |
| ---- | ------- |
| `0` | `status: "pass"` -- every subject point is within the bound. |
| `1` | Failed to run (bad file, malformed expression/spec, unknown `--top`). Error envelope on stderr. |
| `3` | `status: "fail"` -- some subject area is too far from every tap (or an island has no tap). |
| `4` | `status: "unsupported"` -- the rule could not be evaluated; **never a pass**. |

`2` is argparse's usage error. A report is on stdout for `0`, `3` and `4`.

## JSON output

```
{
  "schema_version": 1,
  "file", "input_hash": "sha256:...", "cell",
  "subject", "taps", "island", "tap_nets", "net_spec",
  "max_distance_um", "region_um", "dbu_um",
  "status": "pass" | "fail" | "unsupported",
  "reason": null | "subject_empty" | "taps_empty" | "island_empty"
          | "subject_outside_islands" | "net_not_found" | "no_extraction",
  "reason_detail": null | str,
  "measurement": null | {
    "method": "minkowski_inscribed_polygon", "polygon_sides": 64,
    "resolution_um",
    "measured_max_distance_um",   // proven upper bound on the true worst distance
    "lower_bound_um",             // proven lower bound
    "error_bound_um"              // upper - lower
  },
  "violation_count": int,
  "violations": [{ "rule": "tap_distance.too_far" | "tap_distance.no_tap",
                   "check": "tap_distance", "kind": "too_far" | "no_tap",
                   "description", "cell", "island", "bbox": {...},
                   "definite": bool }],
  "islands": [{ "index", "bbox", "subject_area_um2", "tap_count",
                "status": "pass" | "fail" | "no_subject",
                "measured_max_distance_um", "lower_bound_um",
                "worst_locations": [bbox, ...] }]
}
```

The `violations[]` entries follow the `rule`/`check`/`description`/`cell`/
`bbox` shape of `klt ring-check` and `klt drc`, so `klt report` can aggregate
them. `unsupported` reuses the "no verdict reached" meaning of exit `4` from
`docs/json-contract.md`; it is never graded as a pass.

## Measurement and error bound

The worst distance is found by bisecting a radius `R` (database units): `R`
covers the subject iff `subject - minkowski(taps, P(R))` is empty, with
`P(R)` a 64-gon **inscribed** in the radius-`R` circle. An inscribed polygon
lies inside the disc, so it can only under-cover: the check errs on the safe,
over-reporting side (a circumscribed polygon would be unsafe and is never
used).

`P(R)` contains the disc of radius `R*cos(pi/64) - 1 dbu` and lies inside the
disc of radius `R + 1 dbu`, so the reported `measured_max_distance_um` and
`lower_bound_um` provably bracket the true worst Euclidean distance. The
verdict is taken at `floor(max_distance_um / dbu) - 1`, so `pass` proves
`true <= max_distance_um`. A `fail` whose `lower_bound_um` is still
`<= max_distance_um` carries `definite: false`: it fails only inside the
stated error bound (about 0.12% of the distance plus two grid steps).

The distance is Euclidean, not Manhattan: a subject corner offset by
`(dx, dy)` from a tap corner is `sqrt(dx^2 + dy^2)` away.

## Out of scope

Resistive/electrical latch-up analysis; per-PDK rule values (a follow-up once
the primitive exists); well-conduction net merging (see
`docs/design/erc-well-tap-connectivity-spike.md`).
