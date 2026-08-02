# `klt gen-compose`

Place a set of already-generated [`klt gen`](gen.md) blocks into one composed
GDS/OASIS cell and route two-pin nets between their named ports — phases 1–2
of Epic #191, the build carried by the accepted spike,
[`docs/design/gen-composition-spike.md`](../design/gen-composition-spike.md)
(section 2 for the contract, section 3 for the build-native-not-wrap routing
decision, section 5 for the phased scope proposal). This document is the
shipped contract; where the two disagree, this document (and the code) win.

Phase 3 (#196) is canary bring-up: this contract, unchanged from phases 1–2,
was run end to end against the real sky130 5T OTA case #164 (Epic #153 phase
4, loop closure) needs — a differential pair, a current-mirror load, and a
tail current source, placed and wired with `connectivity[]` — through
`klt gen-compose` → [`klt extract`](extract.md) → [`klt lvs`](lvs.md) →
[`klt sim`](sim.md). See "Worked example" below for the exact request and
results, and "Known limitations (found during phase 3 bring-up)" for the
routing-geometry and loop-closure gaps the bring-up surfaced that phases 1–2
did not anticipate — filed as friction (#199, #200, #201), not folded into
this document's contract.

```
klt gen-compose <request.json> [--format text|json]
```

Like `klt lvs`/`klt sim`, `klt gen-compose` takes a **request document**, not
positional block file args — it binds an arbitrary number of blocks plus
placement/connectivity/routing/options, richer than a flag line carries
cleanly.

- `<request.json>` — path to a request document (see "Request" below).
- `--format` — `text` (default, a human-readable summary) or `json`.

## Scope (phases 1–2)

- **Placement** — two strategies:
  - `"row"` — a single horizontal row, left to right in caller-declared
    `placement.order`, at a uniform `placement.spacing_um` between adjacent
    blocks.
  - `"explicit"` (#321) — each block in `placement.order` is placed at a
    caller-declared `placement.origins_um[id]` `{x, y}` origin instead of a
    computed row offset, so a genuinely two-dimensional floorplan (arbitrary
    positions, per-pair separation) can be expressed directly rather than
    forced through a single row's uniform spacing. `placement.spacing_um` is
    not read under `"explicit"` — the declared origins are the whole
    placement. **Two things `"explicit"` does not do**: it supports no
    orientation/rotation (translation only, exactly like `"row"`), and it
    performs no overlap validation of its own — an overlapping or abutting
    pair of declared origins composes successfully; `klt drc` remains the
    rule-compliance authority on the composed output (see "Geometry is
    advisory" below).

  `"grid"` is spike-scoped for a later phase.
- **Routing** — two-pin, point-to-point Manhattan routing between the named
  ports listed in `connectivity[]`. Each 2-pin net is drawn as a native
  `pya.Path` (backbone → corner bends → straight fill) on the resolved
  `routing.layer_role` layer at `routing.width_um` width; `nets[]` reports
  `routed` and `route_length_um` per net. A net the router cannot connect is
  reported in `unrouted_nets[]` (a **partial success**, exit code `3`), not a
  hard failure. **Bundle (>2-pin) routing is out of scope this phase** — a net
  with more than two pins is left unrouted (reported in `unrouted_nets[]` with
  an explanatory `drc_hints.notes[]` entry), not rejected.
- **Net labels (#200, fixed)** — every routed 2-pin net also gets one
  `kdb.Text` label, named after its own `connectivity[].net` field, on the
  PDK-family label layer that pairs with the resolved routing layer (e.g.
  sky130 `li1.pin` `67/5` for the `"metal"` role's `li1.drawing`; gf180mcu
  `Metal1`'s pin/label purpose `34/10`) — the same label-recognition
  convention [`klt extract`](extract.md) already uses for hand-authored
  corpus cells (`ExtractionDeck.metals[]`/`metal_labels[]`). This is what lets
  a `connectivity[]` net survive `klt extract`'s pin-promotion
  (`Netlist.make_top_level_pins()`/`purge()`) as a **named** `.SUBCKT` pin
  instead of being demoted to an anonymous `$N` net — see "Worked example"
  below. A `routing.layer_role` with no PDK label-layer counterpart (e.g.
  `"poly"`, which pairs with no `ExtractionDeck.metals[]` entry) still gets
  its metal drawn, just without a label — a `drc_hints.notes[]` entry
  explains why.
- **Top-level pins without routing (`pins[]`, #210)** — a `connectivity[]`
  net needs at least two pins to route, so a node with exactly one pin (a
  bias/supply pad, an input, and — critically — every device **gate**) cannot
  be expressed there. `pins[]` fills that gap: each entry
  (`{net, block, port}`) names exactly one block port to promote to a labelled
  top-level pin by dropping one `kdb.Text` at that port's own composed-frame
  position — **no metal is routed**, the port's existing geometry is what the
  label attaches to. The label lands on the label layer that pairs with the
  port's **own** drawn layer (resolved per entry — each port can be on a
  different physical layer, unlike `connectivity[]`'s single shared
  `routing.layer_role`): a metal port on `metal_labels[]`, and a bare-poly
  **gate** port on the `poly_label` layer the extraction deck gained for this
  purpose (sky130 `poly.pin` `66/5`; gf180mcu `Poly2` label purpose `30/10`),
  so a gate survives `klt extract` as a **named, biasable** `.SUBCKT` pin
  instead of an anonymous `$N` net. A `(block, port)` also named in any
  `connectivity[]` entry is rejected (exit 1) — a shape the router already
  labels must not carry a second, possibly inconsistent `pins[]` label. A port
  whose layer has no label convention (e.g. a `bjt_array` collector-ring
  `COLL_*` tap on the diffusion layer) is a **partial success**: the pin is
  left unlabelled with a `drc_hints.notes[]` entry, never a hard failure.
- **`drc_hints`** — `matched_groups[]` reports every distinct
  `matched_group_id` seen among the input blocks (read-only echo of
  `generator_report.drc_hints.matched_group_id`, `placement_symmetric: null` —
  symmetry *verification* is out of scope this phase); `min_spacing_um` reports
  the tightest spacing actually used across placement and routing.
- **Geometry is advisory.** A routed net (`routed: true`) is *not* a DRC-clean
  guarantee — `klt drc` remains the rule-compliance authority on the composed
  output, exactly as it is on any single generator's output.

## Known limitations (found during phase 3 bring-up, #196)

Running the real 5T OTA case (below) surfaced gaps phases 1–2 did not
anticipate; #199, #200, and #201 (below) are all now fixed: the two
device-level shorts #196's bring-up hit are now caught at `klt gen-compose`
time (`unrouted_nets[]` plus a `drc_hints.notes[]` reason) rather than
silently drawn as `routed: true`, every routed `connectivity[]` net now
survives extraction as a named pin, and `klt lvs` no longer logs a spurious
`severity: "error"` mismatch for an unused device class — but the router
still cannot *route around* the two obstacle cases below; both remain
workarounds a caller must apply, exactly as the worked example below does.

- **The router detects, but does not avoid, two obstacle cases —
  same-facing port pairs and guard-ringed blocks (#199, fixed).** A routed
  net's Manhattan backbone is a straight line/single-jog between two ports'
  positions (see "Engine" below); before drawing it, `route_two_pin()` now
  checks the backbone against every placed block's own reported `bbox_um`
  and any `TAP_*`/`COLL_*` (guard/collector ring tap) port names, and
  reports the net **unroutable** (`unrouted_nets[]`, `routed: false`, a
  `drc_hints.notes[]` entry naming the crossed block or ring) instead of
  drawing it, for either of the two cases #196's bring-up hit: **(1)**
  connecting two ports that face the *same* absolute direction (e.g. two
  `_D` ports, both `direction_deg: 0`) would route straight through the
  *destination* device's nearer same-row pin (its `_S` port), shorting that
  device's own source and drain together; **(2)** routing to/from a
  non-tap port on a block with `add_guard_ring: true` (the default for
  `diff_pair`) would cross the guard ring's own local-metal loop, merging
  the signal net with the ring's tap net (checked symmetrically — a
  guard-ringed *source* block is caught the same as a guard-ringed
  *destination* block). Neither case is *routable* at this phase — the
  router reports the obstruction rather than routing around it — so the
  worked example below still applies the same workarounds as before (an
  `add_guard_ring: false` block parameter, and connectivity wired between
  *opposite*-facing port pairs only); the difference #199 makes is that
  skipping a workaround now fails visibly (partial success, exit `3`)
  instead of silently producing a shorted device. The underlying detection
  is a bbox/margin heuristic against each block's own already-reported
  geometry (not a general obstacle-avoiding router, e.g. `route_astar`,
  and not aware of a block's *internal* geometry beyond its `bbox_um` and
  `ports[]`) — full obstacle avoidance (needed once `"grid"` placement
  lands, per the spike's own open questions) remains its own follow-up.
- **The composed output now carries net labels (#200, fixed).** Previously,
  `klt gen-compose` drew routed metal with no `kdb.Text` label, so `klt
  extract`'s pin-promotion (`Netlist.make_top_level_pins()` + `purge()`) kept
  only the one *globally*-connected net every deck ties every device body to
  (`vsubs` in the sky130/gf180mcu curated decks) — every other net, including
  every `connectivity[]` net this command itself just wired, was
  extraction-visible only under an unstable, anonymous `$N` name, and was not
  addressable from a `klt sim` testbench (which can only source/probe a
  `.subckt`'s *declared* pins). `_write_composed_gds` now draws one label per
  routed net (see "Scope" above), so the 5T OTA's `.SUBCKT` below declares a
  pin for every `connectivity[]` net (`N1`, `TAIL_A|TAIL_B`, `VOUT`), not just
  `vsubs` — see the worked example's "Extraction and LVS" and "Simulation"
  steps below. The `connectivity[]` path only labels nets `klt gen-compose`
  itself routes; a *single* block port never passed through `connectivity[]`
  (a bias pad, an input, or a device **gate** — a one-pin node
  `connectivity[]` cannot even express) is named instead via the `pins[]`
  request field (#210, see "Scope" above). **Remaining gap:** `pins[]` can
  label any port whose drawn layer has a label convention — every MOS
  `mos_array`/`diff_pair` gate (poly), and every metal S/D, resistor, or
  guard-ring tap port — but a `bjt_array` collector-ring `COLL_*` tap sits on
  the diffusion/`active` layer, which has no label layer in either curated
  extraction deck, so promoting one is a partial success (unlabelled, with a
  `drc_hints.notes[]` entry). Giving that port a labelable layer is a
  `klt gen`-side follow-up, not part of #210.
- **`klt lvs`'s unused-device-class mismatch is now `severity: "warning"`
  (#201, fixed).** Previously, a device class (e.g. `pfet`) that `klt
  extract` always registers even when a layout has zero instances of it, if
  the paired reference netlist naturally omits that unused class, logged a
  spurious `severity: "error"` mismatch. `status` always correctly reported
  `"match"` regardless (it is `NetlistComparer.compare()`'s own verdict, not
  derived from `severity`), but a caller filtering `mismatches[]` on
  `severity: "error"` alone would have seen a false positive. Not specific
  to composed circuits, but first observed while LVS-checking the worked
  example below.

## CLI shape (a Builder decision, per the spike's own flag)

The spike's contract section names `klt gen compose` as a working name only
("not a commitment to that exact CLI shape"), and explicitly leaves the
nested-subcommand-vs-new-verb call to whoever implements it.
[`klt gen`](gen.md)'s own `gen_parser` (`src/klayout_tools/cli/parser.py`)
takes a flat positional `<generator>` argument, not subparsers — restructuring
it into nested subparsers (`klt gen <subcommand>`) so `compose` could sit
alongside `<generator>` names would require argparse to disambiguate a
literal subcommand token (`compose`) from an arbitrary caller-chosen
generator name in the same positional slot, which argparse's own subparser
mechanism doesn't support without a larger, backward-incompatible rewrite of
`klt gen`'s existing CLI surface (see `test_gen.py`'s `klt gen <generator>`
callers).

This phase therefore implements `klt gen-compose` as a **distinct top-level
verb** (`gen_compose_cmd.py`, registered next to `gen_cmd.py` in `parser.py`),
not a `gen` sub-subcommand — the same request-document CLI shape
`klt sim`/`klt lvs` already use, and reversible: nothing prevents a later
phase from also exposing `klt gen compose` as an alias if a real need
surfaces.

## Engine

Runs fully headless via KLayout's native `pya` (`klayout.db`) — no GUI, no
Qt. Each block's own GDS/OASIS stream (`generator_report.gds_path`) is read
into a scratch `kdb.Layout`, its reported top cell
(`generator_report.cell_name`) is duplicated (`kdb.Cell.copy_tree`) into a
fresh sub-cell of the composed layout, and that sub-cell is instantiated
into the composed top cell at the block's computed `offset_um` — geometry is
copied exactly once (never re-derived from the GDS a second time), and each
block stays its own cell in the output hierarchy (not flattened into the
composed top cell).

Routed metal is built **natively** against `pya.Path` (the spike's
build-not-wrap decision, section 3) — not via a runtime dependency on any
external router. For each 2-pin net, the two ports' positions are resolved
into the composed coordinate frame (each port's own reported `x_um`/`y_um`
translated by its block's `offset_um`), a Manhattan backbone is generated
(leave each port along its outward `direction_deg`, then join the stubs with
right-angle-only segments — a single jog for same-axis ports, a single corner
for mixed-axis ports), and the resulting waypoint list is drawn as one
`pya.Path` on the resolved routing layer, on the composed **top** cell (not
inside any block's sub-cell). A `pya.Path` renders each corner as a square
miter that fully fills the bend, so no separate bend-insertion pass is needed.

**A block's `bbox_um`/`ports[]` are consumed exactly as its own
`generator_report` reported them** — this command never re-derives a
block's placement math from its GDS stream (the spike's "one new guarantee
specific to composition," section 2). Every block referenced by
`blocks[].generator_report` must share the same `dbu` (design-rule grid
resolution) — every `klt gen` generator uses `0.001` (see
[`docs/cli/gen.md`](gen.md)), so this only matters for a hand-crafted
`generator_report` with a different `dbu`; a mismatch is an application
error (exit 1).

## JSON schema (the contract)

**JSON is the API.** Human-readable text output is a courtesy; the JSON
schema below is the stable contract. Per the project's rules, **breaking**
(renaming, removing, or retyping) a field is a breaking change. New fields
may be added without breaking the contract, so consumers should ignore
unknown fields. See [`docs/json-contract.md`](../json-contract.md) for the
envelope shared across all `klt` commands (`schema_version`, error shape,
exit codes).

### Request

```json
{
  "schema": "klt.gen_compose.request/1",
  "pdk": { "variant": "sky130A", "root": null },
  "blocks": [
    { "id": "diffpair", "generator_report": "diffpair.json" },
    { "id": "mirror", "generator_report": "mirror.json" },
    { "id": "tail", "generator_report": "tail.json" }
  ],
  "placement": {
    "strategy": "row",
    "order": ["diffpair", "mirror", "tail"],
    "spacing_um": 1.0
  },
  "connectivity": [
    {
      "net": "VOUT",
      "pins": [
        { "block": "diffpair", "port": "Q1_1_D" },
        { "block": "mirror", "port": "M1_1_D" }
      ]
    }
  ],
  "pins": [
    { "net": "VBIAS", "block": "tail", "port": "U0_G" }
  ],
  "routing": { "layer_role": "metal", "width_um": 0.17 },
  "options": { "cell_name": "ota_top_0", "output": "ota_top_0.gds" }
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema` | string | Contract identifier and major version. |
| `pdk.variant`/`pdk.root` | string \| null | The exact fields `klt pdk find --pdk`/`--pdk-root` accept ([`docs/cli/pdk.md`](pdk.md)) — resolved through that one resolver, never a private lookup. |
| `blocks[]` | array\<object\> | Each already-generated primitive to place — see below. |
| `blocks[].id` | string | Caller-chosen label used to address the block's ports elsewhere in this request (`placement.order`, `connectivity[].pins[].block`). Must be unique within `blocks[]`. |
| `blocks[].generator_report` | object \| string | The block's own [`klt gen`](gen.md) JSON response — either an inline object, or a path to a file holding one (mirrors `klt gen --params`'s own path-or-inline duality). This command's only input about a block's geometry is its already-reported `bbox_um`/`ports[]`/`cell_name`/`gds_path` — never a second, private inspection of the GDS stream at request-parse time. |
| `placement.strategy` | string | `"row"` (single horizontal row, left to right in `order`, spaced by `spacing_um`) or `"explicit"` (#321 — each block placed at its own declared `origins_um[id]`). Any other value (e.g. `"grid"`) is an application error (exit 1). |
| `placement.order` | array\<string\> | Block `id`s in placement order. Every `id` in `blocks[]` must appear exactly once — a missing or extra/unknown `id` is an application error. Response `blocks[]` ordering follows `order` under both strategies. |
| `placement.spacing_um` | number | Fixed gap between adjacent blocks' bounding boxes. Must be `>= 0`. **Only read under `strategy: "row"`** — ignored (not an error) when present alongside `strategy: "explicit"`. |
| `placement.origins_um` | object | **Required when `strategy: "explicit"`**, otherwise not read. Maps every `placement.order` block `id` to its own `{"x": number, "y": number}` origin — that block's `offset_um`, applied exactly like a `"row"` offset (added directly to the block's own reported `bbox_um`; see "`blocks[]` entries" below). The key set must equal `order` exactly — a missing, extra, or unknown `id` is an application error (exit 1), as is a non-numeric `x`/`y`. |
| `connectivity[]` | array\<object\> | One entry per net: a `net` label (caller-chosen, response traceability only) and `pins[]` (at least 2), each `{block, port}` addressing one named port from that block's own `generator_report.ports[]`. A **2-pin** net is routed point-to-point (see "Scope"); a **>2-pin** (bundle) net is left unrouted this phase. A `pins[].block`/`pins[].port` referencing a nonexistent block `id` or port name is an application error (exit 1). |
| `pins[]` | array\<object\> | Optional. One entry per single-pin top-level net to label **without routing** (#210) — e.g. a device gate, a bias/supply pad. Omitting it entirely changes nothing. Each entry names **exactly one** port (unlike `connectivity[]`'s 2+ `pins`). See fields below. |
| `pins[].net` | string | Caller-chosen net name written as the `kdb.Text` label on the port, and echoed in the response. Required and non-empty. |
| `pins[].block` | string | A `blocks[].id`. Referencing an unknown `id` is an application error (exit 1). |
| `pins[].port` | string | A port name from that block's own `generator_report.ports[]`. An unknown port is an application error (exit 1). A `(block, port)` that also appears in any `connectivity[]` entry is rejected (exit 1) — the router already labels that shape. The label lands at the port's own composed-frame position on the label layer paired with the port's own drawn layer; a port on a layer with no label convention is not labelled (a `drc_hints.notes[]` partial-success note, not an error). |
| `routing.layer_role` | string | A layer *role* (e.g. `"metal"`) resolved through the **same** per-PDK-family role→layer table every [`klt gen`](gen.md) generator uses — never a raw `{layer, datatype}` pair. **Required** (and must name a role the resolved PDK family actually has a layer for) when `connectivity[]` is non-empty; otherwise ignored. |
| `routing.width_um` | number | Route wire width. **Required and must be `> 0`** when `connectivity[]` is non-empty; otherwise ignored. |
| `options.cell_name`/`options.output` | string | Same semantics as `klt gen`'s own `options` fields — see [`docs/cli/gen.md`](gen.md). `cell_name` defaults to `"gen_compose_0"`; `output` defaults to `"<cell_name>.gds"`. |

### Response

```json
{
  "schema_version": 1,
  "cell_name": "ota_top_0",
  "gds_path": "ota_top_0.gds",
  "pdk": { "name": "sky130A", "variant": "sky130A", "version": "open_pdks 0fe599b" },
  "bbox_um": { "x0": -0.92, "y0": -0.92, "x1": 14.2, "y1": 2.16 },
  "blocks": [
    {
      "id": "diffpair",
      "generator": "diff_pair",
      "offset_um": { "x": 0.0, "y": 0.0 },
      "bbox_um": { "x0": -0.92, "y0": -0.92, "x1": 3.56, "y1": 2.16 }
    }
  ],
  "nets": [
    {
      "net": "VOUT",
      "pins": [
        { "block": "diffpair", "port": "Q1_1_D" },
        { "block": "mirror", "port": "M1_1_D" }
      ],
      "routed": true,
      "route_length_um": 3.2
    }
  ],
  "pins": [
    { "net": "VBIAS", "block": "tail", "port": "U0_G", "labelled": true }
  ],
  "unrouted_nets": [],
  "drc_hints": {
    "min_spacing_um": 1.0,
    "matched_groups": [
      {
        "matched_group_id": "diff_pair:pair:2",
        "blocks": ["diffpair"],
        "placement_symmetric": null
      }
    ],
    "notes": []
  },
  "warnings": []
}
```

#### Top-level fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema_version` | integer | Version of this command's JSON shape (starts at `1`). |
| `cell_name` | string | Name of the top cell written into `gds_path`, containing every placed block's cell as a translated sub-cell instance plus all routed metal. |
| `gds_path` | string | Resolved output path (echoes `options.output`, or the computed default). |
| `pdk` | object | The resolved PDK reference, echoing `klt pdk find`'s own `variant`/`version` fields. |
| `bbox_um` | object | Bounding box of the *composed* cell — the union of every placed block's own `bbox_um`, translated by its `offset_um` (computed arithmetically from each block's reported `bbox_um`, never re-derived from drawn geometry). |
| `blocks[]` | array\<object\> | Per-block placement result — see below. |
| `nets[]` | array\<object\> | One entry per `connectivity[]` net: an echo of `net`/`pins`, plus `routed` (boolean) and `route_length_um` (total routed wire length in um, or `null` when the net was not routed — for a caller doing a first-order parasitic estimate before extraction). Present for every net including bundle (>2-pin) and unroutable ones (with `routed: false`). |
| `pins[]` | array\<object\> | One entry per request `pins[]` item (#210), in request order: `net`, `block`, `port` (all echoed) plus `labelled` (boolean — `true` when a label was placed, `false` when the port's layer has no label convention, matching a `drc_hints.notes[]` entry). Always present; **empty when the request supplied no `pins[]`** (backward compatible). |
| `unrouted_nets[]` | array\<string\> | Net labels the router could not connect (an unroutable 2-pin net, or a >2-pin bundle net deferred this phase). Always present, empty when everything routed. **A non-empty array is a partial success** (exit code `3`), not silently dropped connectivity. |
| `drc_hints` | object | Advisory, same "not authoritative" semantics as `klt gen`'s own `drc_hints` — `klt drc` remains the actual authority on rule compliance. See fields below. |
| `warnings[]` | array\<string\> | Non-fatal notes. Always present, empty when there is nothing to report. |

#### `drc_hints` fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `min_spacing_um` | number \| null | The tightest spacing actually used across placement and routing (the placement gap when any net was routed) — **`"row"` placement only**. `null` when no `connectivity[]` was supplied (nothing was routed, so no routing/placement spacing was exercised as a clearance), and always `null` under `"explicit"` placement (#321) — there is no single shared spacing value to report; per-pair separation is exactly what a caller-declared origin expresses. |
| `matched_groups[]` | array\<object\> | One entry per distinct `matched_group_id` seen among the input blocks' own `generator_report.drc_hints.matched_group_id` (in first-seen order): `matched_group_id` (echoed), `blocks` (the request-level block `id`s carrying it), and `placement_symmetric` (always `null` this phase — symmetry *verification* against a declared symmetry axis is out of scope). Empty when no input block carries a `matched_group_id`. |
| `notes[]` | array\<string\> | Free-form composition notes — e.g. why a specific net was left unrouted (narrow channel, or a bundle net deferred this phase). Always present, empty when there is nothing to report. |

#### `blocks[]` entries

| Field | Type | Description |
| ----- | ---- | ----------- |
| `id` | string | Echo of the request's `blocks[].id`. |
| `generator` | string | Echoed from that block's own `generator_report.generator`. |
| `offset_um` | object | `{x, y}` — the translation applied to place this block. Under `"row"`, the first block always has `offset_um: {x: 0.0, y: 0.0}`; every subsequent block is translated along `x` only (row placement never translates `y`) so its bbox sits exactly `placement.spacing_um` past the previous (already translated) block's right edge — regardless of that block's own `bbox_um.x0` (which need not be `0`; a guard-ringed block's bbox can extend to negative coordinates). Under `"explicit"` (#321), `offset_um` is exactly the request's own `placement.origins_um[id]`, verbatim — a block's own `bbox_um` plays no role in computing it (an explicit origin translates a block's bbox by that amount; it does not force the bbox's own `(x0, y0)` corner to land exactly on the declared origin unless that block's own `bbox_um.x0`/`y0` is already `0`). |
| `bbox_um` | object | That block's own `generator_report.bbox_um`, translated by `offset_um`, in the composed cell's coordinate frame. |

### Semantics and guarantees

Same guarantees as `klt gen` itself and the spike's proposed contract
(section 2, "Semantics and guarantees"): the contract is engine-neutral
(nothing names `pya`/`klayout.db` in the JSON shape), routing-layer resolution
goes through the one per-PDK role-layer table every generator already uses (a
`routing.layer_role`, never a raw `{layer, datatype}`), `drc_hints` is advisory
not authoritative, PDK resolution goes through the one resolver, and the
envelope is additive — new fields may be added without a schema/`schema_version`
bump; renaming, removing, or retyping an existing field requires one.

**One new guarantee specific to composition:** a block's `bbox_um`/`ports[]`
are consumed exactly as its own `generator_report` reported them — this
command never re-derives a block's placement math from its GDS stream (see
"Engine" above).

## Text format

The default `text` format prints a short summary. It is intended for human
eyes and its exact layout is **not** part of the contract — parse the JSON
instead.

```
$ klt gen-compose request.json
cell_name: ota_top_0
gds_path: ota_top_0.gds
pdk: sky130A (open_pdks 0fe599b)
bbox_um: (-0.92, -0.92) - (14.2, 2.16)

blocks:
  diffpair (diff_pair)  offset=(0.0, 0.0)  bbox=(-0.92, -0.92) - (3.56, 2.16)
  mirror (diff_pair)  offset=(5.48, 0.0)  bbox=(4.56, -0.92) - (9.04, 2.16)
  tail (mos_array)  offset=(11.56, 0.0)  bbox=(10.04, 0.0) - (14.2, 0.42)

nets:
  VOUT  routed  length=3.2um

matched_groups:
  diff_pair:pair:2  (diffpair)
```

## Exit codes and errors

| Exit code | Meaning |
| --------- | ------- |
| `0` | Every block placed and every net routed; `gds_path` was written and the report above is on stdout. |
| `1` | Application error — unresolvable PDK, malformed request (missing/invalid `blocks[]`, `placement.order` not matching `blocks[]`, negative `spacing_um`, a missing/mismatched/non-numeric `placement.origins_um` when `strategy: "explicit"` (#321), or a missing/invalid `routing.layer_role`/`routing.width_um` when `connectivity[]` is non-empty), an unsupported `placement.strategy`, a `connectivity[]` or `pins[]` entry referencing a nonexistent block `id`/port, a `pins[]` entry naming a `(block, port)` already used by a `connectivity[]` net, a block's `generator_report`/GDS could not be read, or the `options.output` directory does not exist. |
| `2` | Usage error — missing `<request.json>` argument, or a bad `--format` value (from argparse). |
| `3` | **Partial success** — every block placed, but `unrouted_nets[]` is non-empty (a net could not be routed, or a >2-pin bundle net was deferred this phase). The full success payload above is still on stdout, mirroring `klt drc`'s own `3` for "ran clean but found violations" (spike section 2, "Proposed exit codes"). |

On error, a concise message is written to **stderr** and nothing is written
to stdout (and no GDS/OASIS file is written). No Python traceback is
printed.

- `--format text` (default): a plain-text line prefixed `klt gen-compose:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "gen-compose", "message": "request.connectivity[0] (net 'VOUT') references unknown port 'NOPE' on block 'diffpair' -- available: Q1_1_D, Q1_1_G, Q1_1_S, ..." } }
  ```

## Explicit placement (a two-dimensional floorplan, #321)

`"row"` can only express a single left-to-right strip at one uniform
`spacing_um`. `"explicit"` instead lets the caller declare each block's own
`(x, y)` origin, so an arrangement like an L-shape — or any other
two-dimensional floorplan with per-pair separation — can be composed and
DRC'd as one thing, with a usable `bbox_um` reflecting the actual arrangement
rather than a wide, mostly-empty row:

```json
{
  "pdk": { "variant": "sky130A" },
  "blocks": [
    { "id": "diffpair", "generator_report": "diffpair.json" },
    { "id": "mirror", "generator_report": "mirror.json" },
    { "id": "tail", "generator_report": "tail.json" }
  ],
  "placement": {
    "strategy": "explicit",
    "order": ["diffpair", "mirror", "tail"],
    "origins_um": {
      "diffpair": { "x": 0.0, "y": 0.0 },
      "mirror": { "x": 0.0, "y": 40.0 },
      "tail": { "x": 60.0, "y": 20.0 }
    }
  },
  "options": { "cell_name": "floorplan_0", "output": "floorplan_0.gds" }
}
```

`diffpair` and `mirror` share an `x` (stacked along `y`); `tail` sits to the
east at a third `y`. `connectivity[]` and `pins[]` work identically to
`"row"` — `route_two_pin`/`manhattan_backbone` resolve each port's own
composed-frame position (`x_um`/`y_um` plus its block's `offset_um`) and
route generically against `(x, y)`, with no assumption that ports differ
only along `x`; a net between two blocks placed at different `y` (not just
different `x`) routes through a **vertical** jog exactly the same way a
row-placed net routes through a horizontal one.

Two things `"explicit"` deliberately does not add (see "Scope" above):
placed blocks carry no orientation/rotation (an origin is a translation
only), and `gen-compose` performs no overlap check of its own — a caller
that declares two blocks at overlapping origins gets a composed GDS with
overlapping geometry and no error from this command; `klt drc` remains the
authority that would catch an actually-illegal result.

## Worked example

**Verified end to end (#196, phase 3 canary bring-up; re-verified after
#200)**: the real sky130 5T OTA case #164 needs — a differential pair, a
current-mirror load, and a single-device tail current source, composed and
wired with `connectivity[]` — taken through `klt gen-compose` -> `klt
extract` -> `klt lvs` -> `klt sim`, all the way through to a passing
simulation biasing the composed circuit's own declared net names. The exact
commands and results below are what #196 originally ran (sky130A; a
gf180mcuA run of the same request produces
byte-identical topology — see "gf180mcu bonus" below).

Placement order is **`tail` first**, not `diffpair`/`mirror`/`tail` as a
naive reading of the spike's illustrative request might suggest — every
phase-2 generator's drain-side ports face east and source-side ports face
west regardless of row position (see "Known limitations" above), so
`tail`'s `_D` port only faces a same-row neighbour correctly when that
neighbour is immediately to its *east*. `add_guard_ring: false` is passed
to both `diff_pair` blocks for the same reason (#199) -- an external route
into a guard-ringed block shorts against the ring's own metal.

```bash
# Generate three real blocks with `klt gen` (a tail current source, a
# differential pair, and a current-mirror-labelled load -- the #164 5T OTA
# case the composition spike's section 4 worked through). `splits: 1` keeps
# each device a single instance (no common-centroid interleaving) so each
# generator's own Q1/Q2 (or M1/M2) port pair is unambiguous; `add_guard_ring:
# false` avoids the guard-ring finding above (#199):
$ klt gen mos_array --params '{"rows": 1, "cols": 1, "dummy": 0}' --pdk sky130A \
    -o tail.gds --format json > tail.json
$ klt gen diff_pair --params '{"mirror": false, "splits": 1, "add_guard_ring": false}' \
    --pdk sky130A -o diffpair.gds --format json > diffpair.json
$ klt gen diff_pair --params '{"mirror": true, "splits": 1, "add_guard_ring": false}' \
    --pdk sky130A -o mirror.gds --format json > mirror.json

# Compose them into one row-placed cell. Connectivity: TAIL_A/TAIL_B tie the
# pair's two source nodes and the tail device's drain into one three-way tail
# node (decomposed into two 2-pin nets sharing the tail.U0_D endpoint, since
# bundle/>2-pin nets are out of scope this phase); N1/VOUT tie each input
# device's drain to the mirror's *source*-side port on the matching row --
# not literally "drain to drain" (#199 above; see the comment there for why),
# but the closest topologically-meaningful connection this phase's router can
# make cleanly between the pair and its load:
$ cat > compose_request.json <<'EOF'
{
  "pdk": { "variant": "sky130A" },
  "blocks": [
    { "id": "tail", "generator_report": "tail.json" },
    { "id": "diffpair", "generator_report": "diffpair.json" },
    { "id": "mirror", "generator_report": "mirror.json" }
  ],
  "placement": { "strategy": "row", "order": ["tail", "diffpair", "mirror"], "spacing_um": 1.0 },
  "connectivity": [
    { "net": "TAIL_A", "pins": [{ "block": "tail", "port": "U0_D" }, { "block": "diffpair", "port": "Q1_1_S" }] },
    { "net": "TAIL_B", "pins": [{ "block": "tail", "port": "U0_D" }, { "block": "diffpair", "port": "Q2_1_S" }] },
    { "net": "N1", "pins": [{ "block": "diffpair", "port": "Q1_1_D" }, { "block": "mirror", "port": "M1_1_S" }] },
    { "net": "VOUT", "pins": [{ "block": "diffpair", "port": "Q2_1_D" }, { "block": "mirror", "port": "M2_1_S" }] }
  ],
  "routing": { "layer_role": "metal", "width_um": 0.17 },
  "options": { "cell_name": "ota_top_0", "output": "ota_top_0.gds" }
}
EOF
# Exit 0 -- every block placed, every net routed (unrouted_nets: []).
$ klt gen-compose compose_request.json --format json
```

### Extraction and LVS

**Re-verified after #200** (previously, this step's `.SUBCKT` declared only
`vsubs`; it now declares a pin for every routed `connectivity[]` net too):

```bash
# Extract the composed GDS (5 devices: tail + 2 diff-pair + 2 mirror, all
# nfet -- diff_pair's "mirror" naming is a labelling convention only, see
# docs/cli/gen.md; it draws the same NMOS geometry either way):
$ klt extract ota_top_0.gds --deck sky130 --top ota_top_0 \
    -o ota_top_0.spice --format json
# device_count: 5, device_counts: {"nfet": 5}, pin_count: 4, exit 0.
# ota_top_0.spice now declares:
#   .SUBCKT ota_top_0 N1 TAIL_A|TAIL_B VOUT vsubs
# (TAIL_A and TAIL_B are the same physical node -- both routed to tail's
# single U0_D port -- so KLayout's netlist writer joins their two labels
# into one alias, "TAIL_A|TAIL_B"; N1/VOUT are each single-labelled.)

# Compare against a hand-written reference netlist with the same topology
# (a three-way tail node and two 2-terminal load nodes now match by name;
# five gate nets and three drain/source terminals remain isolated/floating
# -- those are `klt gen`'s own per-generator `ports[]`, never passed through
# `connectivity[]`, and are out of scope for #200, see "Known limitations"):
$ cat > ota_reference.spice <<'EOF'
.subckt ota_top_0 N1 TAIL_NODE VOUT vsubs
M1 TAIL_NODE g1 flt1 vsubs nfet L=0.28U W=0.42U
M2 TAIL_NODE g2 N1 vsubs nfet L=0.28U W=0.42U
M3 N1 g3 flt3 vsubs nfet L=0.28U W=0.42U
M4 TAIL_NODE g4 VOUT vsubs nfet L=0.28U W=0.42U
M5 VOUT g5 flt5 vsubs nfet L=0.28U W=0.42U
.ends
EOF
$ cat > lvs_request.json <<'EOF'
{
  "schema": "klt.lvs.request/1",
  "layout": { "file": "ota_top_0.gds", "deck": "sky130", "top": "ota_top_0" },
  "reference": { "netlist": "ota_reference.spice", "top": "ota_top_0" }
}
EOF
# status: "match", counts: nets 12/12/12, devices 5/5/5, pins 4/4/4 (was
# 1/1/1 before #200), exit 0. mismatch_count is 1 (the unused-device-class
# "warning" from #201 above -- unrelated to #200); it doesn't change
# `status`.
$ klt lvs lvs_request.json --format json
```

### Simulation

**Re-verified after #200** — the composed circuit's own `connectivity[]`
net names (not just `vsubs`) are now addressable from a `klt sim`
testbench, per `docs/cli/extract.md`'s documented pattern:

```bash
# A thin testbench `.include`s the extracted file unmodified and
# instantiates the `.subckt`, biasing it through its own declared pins
# (N1/TAIL_A|TAIL_B/VOUT/vsubs -- the caller picks its own local node names
# for the Xota instantiation; the .SUBCKT's own pin order, not the pin
# *text*, positionally binds them):
$ cat > testbench.spice <<'EOF'
.include "ota_top_0.spice"
.model nfet nmos level=1
.options rshunt=1e12
Vvsubs vsubs 0 DC 0
Vn1 n1_node 0 DC 1.0
Vtail tail_node 0 DC 0.5
Vvout vout_node 0 DC 1.0
Xota n1_node tail_node vout_node vsubs ota_top_0
EOF
$ cat > sim_request.json <<'EOF'
{
  "netlist": "testbench.spice",
  "analysis": { "kind": "tran", "args": "1n 1n" },
  "measurements": [
    { "name": "vout_meas", "spice": ".meas tran vout_meas find v(vout_node) at=1n" },
    { "name": "tail_meas", "spice": ".meas tran tail_meas find v(tail_node) at=1n" }
  ]
}
EOF
# status: "pass", exit 0 -- vout_meas/tail_meas read back the exact bias
# (1.0V/0.5V) applied through the composed circuit's own declared pins.
#
# Two notes on the testbench shape above, neither of them #200's concern:
# - `analysis.kind: "tran"` (a single-timestep transient), not `"op"`:
#   ngspice's `.MEASURE` statement does not recognise `"op"` as an analysis
#   type at all (`Error: unrecognized analysis type 'op'`) -- unrelated to
#   #200, and now a validated, rejected combination rather than a silent
#   ngspice parse failure (`klt sim` raises a clear error for a `.meas op`
#   card, see #205), that a prior revision of this example never actually
#   exercised (it always failed earlier, at the singular-matrix stage
#   below, masking it).
# - `.options rshunt=1e12` (a standard SPICE convergence aid -- a very
#   large global shunt resistor from every node to ground): this circuit's
#   five gate terminals are `klt gen`'s own per-generator `ports[]`, never
#   wired through `connectivity[]` in this request, so they stay genuinely
#   floating (out of scope for #200, see "Known limitations"). Without
#   `rshunt`, ngspice's DC solver logs a `singular matrix` warning while
#   still recovering a value via internal gmin/source stepping; `klt sim`
#   no longer misclassifies that recovery as fatal (#205 fixed the
#   `status: "error"` false positive this used to produce), but `rshunt`
#   remains worth keeping here anyway -- it gives every node a real (if
#   enormous) DC path, so the solver converges cleanly with zero
#   diagnostics instead of a recorded (non-fatal) `singular_matrix`
#   warning -- no hand-editing of `ota_top_0.spice`, and no need to
#   address any node by its anonymous `$N` name.
$ klt sim sim_request.json --format json
```

### gf180mcu bonus

The identical `compose_request.json`/`lvs_request.json` shape, with
`sky130A` -> `gf180mcuA` and `--deck sky130` -> `--deck gf180mcu`, produces
byte-identical device/net topology (`device_count: 5`,
`device_counts: {"nfet": 5}`, `pin_count: 4`) and an identical `klt lvs`
`"match"` verdict (`pins 4/4/4`) against the same reference netlist --
every phase-2 generator's layout shape is PDK-family-agnostic
(`docs/cli/gen.md`), so this composition and its connectivity carry over
unchanged.
