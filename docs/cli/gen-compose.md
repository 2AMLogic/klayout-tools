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

- **Placement** — three strategies:
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
  - `"array"` (#1053) — the **one** `blocks[]` entry named in
    `placement.order` is repeated on a regular `rows` x `cols` grid
    (`placement.rows`/`cols`/`row_pitch_um`/`col_pitch_um`, plus an optional
    `placement.origin_um` for the base — row 0, col 0 — tile, defaulting to
    `{0, 0}`), emitted as a **single** hierarchical `kdb.CellInstArray`
    instance rather than `rows * cols` individual placements — see "Array
    placement (a repeated-block regular tiling, #1053)" below. Takes exactly
    one `blocks[]` entry (an `"array"` request with more than one is an
    application error) and, like `"explicit"`, supports no orientation
    (translation only).

  `"grid"` is a *different*, still-unimplemented feature reserved by the
  accepted spike for a later phase (a row-wrap layout of *distinct* blocks
  into `placement.cols` columns) — deliberately not the name used for
  `"array"` above, to avoid colliding with that reservation.
- **Routing** — two-pin, point-to-point Manhattan routing between the named
  ports listed in `connectivity[]`. Each 2-pin net is drawn as a native
  `pya.Path` (backbone → corner bends → straight fill) on the resolved
  `routing.layer_role` layer at `routing.width_um` width; `nets[]` reports
  `routed` and `route_length_um` per net. A net the router cannot connect is
  reported in `unrouted_nets[]` (a **partial success**, exit code `3`), not a
  hard failure. **Bundle (>2-pin) routing is out of scope this phase** — a net
  with more than two pins is left unrouted (reported in `unrouted_nets[]` with
  an explanatory `drc_hints.notes[]` entry), not rejected.
- **Via-drop routing (#454)** — a family whose curated extraction deck
  declares a second routing-metal level exposes it as a second
  `routing.layer_role` (sky130's `"metal2"`, resolving to met1 `68/20`;
  gf180mcu's `"metal2"`, resolving to Metal2 `36/0`), alongside the
  connecting via role (`"via1"`, sky130's mcon `67/44` / gf180mcu's Via1
  `35/0`) — sourced directly from each curated `ExtractionDeck`'s own
  `metals`/`vias` stack, never a second, private layer map. Selecting
  `"metal2"` runs the whole backbone on that second metal and drops back
  down to *each* target pin's own base-`"metal"`-role pad only via an
  enclosed via at that pin's own position — the backbone itself never runs
  across another pad on the base metal layer, so a same-block bus (e.g.
  chaining a matched array's unit terminals) is routable without the
  same-layer short #433 made visible instead of fixing. See "Known
  limitations" below for the exact before/after against #433's own
  reproduction, and ["Via-drop routing (metal2/via,
  #454)"](#via-drop-routing-metal2via-454) for a worked request/response
  pair. sky130's curated deck exposes one level further still (#508):
  `"metal3"` (met2 `69/20`) plus its own connecting via role (`"via2"`, the
  met1↔met2 via, `68/44`) — usable the same way, but only from a pin already
  on `"metal2"` (met1, one via hop away); a pin still on the base `"metal"`
  role (li1) is two hops from `"metal3"` and the via-drop's single-hop limit
  (below) rejects it. gf180mcu's own deck still exposes only `"metal2"`.
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
(#434 adds one way *through* rather than around: a ring generated with a
declared opening — see "Routing through a ring opening" below — so a matched
group no longer has to choose between keeping its guard ring and being wired
into the circuit.)

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
  non-tap port on a block whose ring is **closed** (`add_guard_ring: true`,
  the default for `diff_pair`, with no ring opening declared) would cross
  the guard ring's own local-metal loop, merging the signal net with the
  ring's tap net (checked symmetrically — a guard-ringed *source* block is
  caught the same as a guard-ringed *destination* block). Neither case is
  *routable* at this phase — the router reports the obstruction rather than
  routing around it — so the worked example below still applies the same
  workarounds as before (an `add_guard_ring: false` block parameter, and
  connectivity wired between *opposite*-facing port pairs only — case
  **(1)** now also has a remedy that keeps a same-facing pair: see
  "Routing same-facing port pairs with waypoints_um" below); the
  difference #199 makes is that skipping a workaround now fails visibly
  (partial success, exit `3`) instead of silently producing a shorted
  device. The underlying detection
  is a bbox/margin heuristic against each block's own already-reported
  geometry (not a general obstacle-avoiding router, e.g. `route_astar`,
  and not aware of a block's *internal* geometry beyond its `bbox_um` and
  `ports[]`) — full obstacle avoidance (needed once `"grid"` placement
  lands, per the spike's own open questions) remains its own follow-up.
  Case **(2)** now also has a remedy that keeps the ring: see "Routing
  through a ring opening" below.
- **The obstacle-overlap check above is `routing.width_um`-aware, not just a
  zero-width centerline test (#999, fixed).** The bbox/margin heuristic
  described above used to test only the backbone's zero-width *centerline*
  against every placed block's `bbox_um` -- not the width of the metal
  actually drawn. A same-facing port pair whose default backbone's
  connecting jog clears a block's bbox edge by less than
  `routing.width_um / 2` used to pass this check (`routed: true`, DRC-clean,
  since a routed short leaves no gap for `klt drc` to measure -- the same
  class of gap this document's "explicit placement" worked example describes
  for flush-placed blocks, below) even though the conductor actually
  drawn there -- which extends `routing.width_um / 2` past the centerline
  on every side -- still overlapped that block's metal, silently merging
  two nets that should stay independent (visible only via `klt extract`'s
  net count, several steps downstream of where the short was introduced).
  `route_two_pin()` now inflates every bbox this check tests against by
  `routing.width_um / 2` on every side first, mirroring the inflation the
  self-net pad-crossing check (#433, above) and the ring-opening check
  (#434) already apply; each own-pin's edge-margin allowance is bumped by
  the same amount so a normal approach into that pin's own block is not
  penalized by the inflation. As with #199, this widens what the router
  **detects** -- it still reports the net unroutable rather than routing
  around the obstacle, so the same workarounds (an `add_guard_ring: false`
  parameter, opposite-facing port pairs, or `waypoints_um` with several
  microns of clearance from every block) still apply whenever a near-miss
  like this is rejected.
- **Routing same-facing port pairs with `waypoints_um` (#634, fixed).** Case
  **(1)** above has no remedy when the caller cannot choose which ports get
  wired — e.g. a hand-drawn cell that legitimately puts its input and output
  on the same edge, so *every* link between two such cells is a same-facing
  pair, not just some. A `connectivity[]` entry now accepts an optional
  `"waypoints_um": [[x, y], ...]` field (um, in the composed coordinate
  frame): an ordered list of points the backbone is forced through, between
  port `a`'s own stub and port `b`'s own stub, in place of
  `manhattan_backbone()`'s fixed one-jog/corner shape. This is deliberately
  *not* a general obstacle-avoiding router — the caller supplies the routing
  knowledge the fixed shape lacks (e.g. a point above the row's shared bbox
  top, clearing both blocks entirely), and every one of #199/#433/#453's
  existing routability checks, including the obstacle-overlap check, still
  runs against the resulting path: a waypoint that still crosses another
  block's bbox interior is rejected (`routed: false`) exactly like any other
  backbone, not silently drawn as a short. Omitting `waypoints_um` changes
  nothing — the fixed-shape backbone above is exactly what still runs.

  ```json
  {
    "net": "n1",
    "pins": [
      { "block": "a", "port": "Y" },
      { "block": "b", "port": "A" }
    ],
    "waypoints_um": [[-0.17, 1.0], [11.09, 1.0]]
  }
  ```
- **Routing through a ring opening (#434, fixed).** A closed ring left
  `add_guard_ring: false` as the only way to wire a matched group into the
  rest of a circuit — i.e. a block could have its ring or its connectivity,
  not both. A block generated with
  [`klt gen`](gen.md)'s `ring_gap_side`/`ring_gap_um`/`ring_gap_offset_um`
  params reports its ring's one routing opening as a `GAP_<side>` port, and
  `route_two_pin()` then admits a route to that block's non-tap ports —
  **only if the drawn backbone actually goes through the opening**. Every
  segment of the backbone is tested against all four of the ring's own side
  centre lines (located from the ring's own `TAP_*`/`COLL_*`/`GAP_*` ports)
  inside the block's placed bbox, and the net is still reported unroutable
  when the backbone: crosses a side that declares no opening; crosses the
  gapped side outside the opening, or closer to either cut end than half the
  route width plus the block's own reported `drc_hints.min_spacing_um`; or
  runs *along* a ring side (metal laid on the ring is a short however wide
  the opening is). A ring that does not report where all four of its sides
  run is rejected rather than assumed clear. So the ring check is *widened*,
  never relaxed: with no opening declared, the behavior is exactly #199's.
  A `GAP_*` port itself is a marker for the absence of metal, not a
  conductor — naming one in `connectivity[]` or `pins[]` is an application
  error (exit `1`).

  ```bash
  # A guard-ringed pair whose ring is opened on the east side, on the row
  # its M1_1_D port sits on (0.41um below the ring's own mid-height), so a
  # route east out of that port passes through the opening:
  klt gen diff_pair --pdk sky130A -o a.gds --format json \
    --params '{"mirror": true, "splits": 2, "ring_gap_side": "E",
               "ring_gap_um": 1.0, "ring_gap_offset_um": -0.41}' > a.json
  # ...and its neighbour, opened on the west side it is approached from:
  klt gen diff_pair --pdk sky130A -o b.gds --format json \
    --params '{"splits": 2, "ring_gap_side": "W",
               "ring_gap_um": 1.0, "ring_gap_offset_um": -0.41}' > b.json
  # connectivity[] between a.M1_1_D and b.Q1_1_S now routes (exit 0) with
  # both guard rings intact, instead of exit 3 + unrouted_nets[].
  klt gen-compose request.json --format json
  ```
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
- **A self-net that crosses another pad on its own block is no longer a
  silent short (#433, fixed).** #199's obstacle-overlap check above exempts
  a **self-net** (both pins on the *same* block) from the whole-block bbox
  check entirely -- a same-block net's backbone is, by construction, always
  inside its own block's bbox, so without the exemption every self-net would
  be rejected. But the exemption also meant nothing checked whether that
  backbone ran straight over one of the block's *other* pads on the way --
  exactly what happens bussing a matched array's unit devices into one node
  (e.g. chaining three of an 8-unit `bjt_array`'s emitters with two 2-pin
  self-nets: each backbone jogs directly over the base pad sitting between
  the two emitters it connects). `route_two_pin()` now compares the backbone
  against every *other* same-layer port on that block (each approximated as
  a square pad footprint, its reported `width_um` on a side, inflated by the
  route's own trace half-width so a wire narrower than the gap between pad
  and centerline still counts) and reports the net **unroutable** instead of
  drawing it. A port on a different physical layer than `routing.layer_role`
  is not treated as an obstacle (it cannot short on that layer).

  A second, **conservative** check (#453) closes a gap the square-footprint
  model above misses. A port's reported `width_um` is roughly its contact
  size, not the full extent of its drawn pad — an array unit's base-tie tap,
  for instance, draws metal several times taller than its reported `width_um`
  in its facing direction. So when a self-net joins two ports that face the
  **same** direction and share the coordinate along that facing axis (same
  row for a north/south-facing pair, same column for an east/west-facing
  pair), `manhattan_backbone()` collapses to a single straight jog lifted just
  one stub width to the ports' outward side, and a route *wider* than the
  intervening pad's under-sized reported square still plows through that pad's
  real drawn metal. `route_two_pin()` therefore rejects the net whenever any
  other same-layer port that faces the **same** direction sits strictly
  between the two pins along the perpendicular axis (on the same row/column) —
  regardless of that port's reported `width_um`. This is the exact 8-unit
  `common_centroid bjt_array` case where bussing two same-row north-facing
  emitters across the intervening unit's base-tie pad previously composed
  `routed: true` and DRC-clean while extraction showed the whole array's
  shared base node absorbed into the emitter net.

  Both rejections above are **same-layer** checks: each skips any port drawn
  on a different physical layer than `routing.layer_role` (the
  `other_layer != route_layer: continue` guard in `route_two_pin()`), so each
  fires only against a pad sitting on the *same* metal the backbone runs on.
  That was metal-only bussing's fundamental limit at the time, not a fix for
  it: the router had no `metal2`/via role to hop over a crossed pad, so a
  genuinely necessary intra-block bus (as opposed to a route that happens to
  cross one because its two ports were picked at either end of a row) had no
  routable path -- it failed visibly (`unrouted_nets[]`, exit `3`) instead of
  drawing a short. **#454 (fixed) closes that gap**: `routing.layer_role:
  "metal2"` routes the same bus on the second routing-metal level with a
  via-drop back to each pin's own pad. Because no `klt gen` generator draws
  any pad on `"metal2"`, the backbone runs on a layer no pad occupies, so both
  #433's original same-layer check and #453's same-row/same-direction check
  are structurally bypassed on a `"metal2"` route (each `continue`s past every
  pad, none being on `route_layer`) rather than having to be crossed at all --
  see "Via-drop routing (metal2/via, #454)" below. `"metal"` (the base role)
  is unaffected: a caller who does not opt into `"metal2"` still gets exactly
  #433's and #453's fail-visibly behavior for a same-layer crossing.

## Via-drop routing (metal2/via, #454)

Re-raising #433's Ask options 1/2 (the merged #433 fix implemented only
option 3, "fail visibly"): a family whose curated `ExtractionDeck` already
declares a second routing-metal level and the via that lands on it
(sky130's `EXTRACTION_DECK.metals=((67,20),(68,20),(69,20))` /
`.vias=((67,44),(68,44))`; gf180mcu's Metal1→Metal5 stack) now exposes that
second level as a second `routing.layer_role` (`"metal2"`) plus the via role
that connects it back to the base `"metal"` role (`"via1"`) — sourced
directly from the deck's own `metals`/`vias` tuples in
`klayout_tools.gen._PDK_ROLE_LAYERS`, never a second, private layer map.
sky130's deck now declares a third level too (met2, issue #508): `"metal3"`
plus `"via2"` (the met1↔met2 via) work the same way one level up, but only
between `"metal2"` and `"metal3"` themselves — see the single-hop limit
below for why `"metal3"` cannot via-drop straight down to a pin still on the
base `"metal"` role.

Selecting `"metal2"` changes what `route_two_pin()` draws, not the
request/response shape: the Manhattan backbone still runs between the same
two composed-frame port positions (`manhattan_backbone`'s own geometry is
unchanged), but now on the second metal (sky130 met1) instead of the base
metal (li1). At each endpoint whose own reported layer differs from the
resolved `routing.layer_role` — every currently-generated block's pads, since
no `klt gen` generator draws on `"metal2"` itself — the router drops a via
(sky130 mcon) plus a landing-pad square on *both* the backbone's own layer
and the pin's own layer, centered exactly on that pin's own composed-frame
position (`gen_compose._resolve_via_drop_layer`). The backbone itself never
touches another pad's base-metal layer, so a same-block bus that would
otherwise cross #433's own pad-crossing rejection is now routable instead of
`unrouted_nets[]`.

```json
{
  "pdk": { "variant": "sky130A" },
  "blocks": [{ "id": "arr", "generator_report": "arr.json" }],
  "placement": { "strategy": "row", "order": ["arr"], "spacing_um": 1.0 },
  "connectivity": [
    {
      "net": "EBUS1",
      "pins": [
        { "block": "arr", "port": "Q0_E" },
        { "block": "arr", "port": "Q1_E" }
      ]
    },
    {
      "net": "EBUS2",
      "pins": [
        { "block": "arr", "port": "Q1_E" },
        { "block": "arr", "port": "Q2_E" }
      ]
    }
  ],
  "routing": { "layer_role": "metal2", "width_um": 0.17 },
  "options": { "cell_name": "bjt_bus", "output": "bjt_bus.gds" }
}
```

Against an 8-unit `bjt_array` (the exact repro #433's own tests use), both
`EBUS1`/`EBUS2` now come back `routed: true` — where the same request with
`routing.layer_role: "metal"` reports both in `unrouted_nets[]` with #433's
own rejection reason. The composed output is DRC-clean against `klt drc
--deck sky130`, and `klt extract --deck sky130` merges exactly the three
targeted emitters (`Q0_E`/`Q1_E`/`Q2_E`) into one node — every other emitter
and every base tie stays its own distinct node, matching the hand-drawn `klt
draw` workaround #454 was filed to replace.

A pin whose own layer is not a member of the resolved family's
`ExtractionDeck.metals` stack, but which a `routing.layer_role` shape already
covers at that position (e.g. a guard ring's `TAP_*` port on the tap layer,
under the ring's own metal), is left exactly as before #454 — drawn directly
on `routing.layer_role`, no via-drop attempted, since via-drop only ever
applies between two declared routing-metal levels.

Two cases are rejected instead, reporting the net unroutable rather than
drawing something that does not connect:

- A pin whose layer is a *different* metals-stack level than
  `routing.layer_role`, more than one via hop away — sky130's/gf180mcu's
  `"metal2"`/`"via1"` pair is always exactly one hop from `"metal"`, so this
  case does not arise for either family's second level. sky130's third
  level (`"metal3"`/`"via2"`, issue #508) is where it first becomes real: a
  `"metal3"` route to a pin still on the base `"metal"` role (li1) is two
  hops away and hits this rejection — only a pin already on `"metal2"`
  (met1, one hop from met2) resolves. gf180mcu's Metal3-5 levels remain
  unexposed as `routing.layer_role` roles at all (its curated
  `EXTRACTION_DECK` declares the full Metal1-Metal5 stack for extraction,
  but `klayout_tools.gen._PDK_ROLE_LAYERS["gf180mcu"]` stops at `"metal2"`),
  so this case stays unreachable on that family.
- A pin on the deck's bare **`poly`** layer — a `mos_array`/`diff_pair` gate
  drawn *without* [`params.gate_contact`](gen.md) (issue #492). No via in the
  metals stack lands on poly, so the backbone would end as an uncontacted
  metal stub sitting *over* the gate: before #492 that was drawn anyway
  (`"routed": true`, no note), leaving an open net that only a later `klt
  drc`/`klt extract`/`klt lvs` run would surface, with nothing pointing back
  at the cause. The rejection's `reason` names both the port's actual layer
  and the two ways forward — re-run the block's generator with
  `params.gate_contact: true` so the gate reports a contacted `"metal"`-role
  pad, or name the gate with `pins[]` (below) instead of routing to it.

  A third check (#469) generalizes both checks above from *reported*
  `ports[]` geometry to the block's **actual drawn** shapes. The two checks
  above only fire for a same-row/same-column, same-direction pin pair —
  exactly the degenerate single-jog backbone their reasoning models — so
  they still miss a same-facing pair on *different* rows/columns, or a route
  wide enough to reach an adjacent row's pad while clearing every modelled
  reported-`width_um` square. For a **self-net only**, `compose()` now reads
  the block's own GDS stream once per block (`read_block_layer_geometry()`,
  lazily, cached — every other net is already covered by the whole-block
  bbox check and never needs it) for its merged shapes on `routing.
  layer_role`, translated into the composed frame. `route_two_pin()` then
  intersects the route's actual drawn metal (the same `kdb.Path` construction
  the composed GDS gets) against those shapes: overlapping any merged shape
  other than the two the endpoints land on (a positive-area overlap only —
  an edge touch is a `klt drc` spacing question, not a short) is a silent
  short, reported `unrouted_nets[]` rather than drawn. This is the *only*
  place this command reads a block's shapes rather than its
  `generator_report` — placement math is still never re-derived from a
  block's GDS stream (see below); this reads obstacle geometry only, for a
  self-net's own block. The two checks compose rather than replace each
  other: either can catch a short the other's information set cannot see.

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
block's *placement math* from its GDS stream (the spike's "one new guarantee
specific to composition," section 2). Every block referenced by
`blocks[].generator_report` must share the same `dbu` (design-rule grid
resolution) — every `klt gen` generator uses `0.001` (see
[`docs/cli/gen.md`](gen.md)), so this only matters for a hand-crafted
`generator_report` with a different `dbu`; a mismatch is an application
error (exit 1).

The one exception (#453/#469): for a **self-net**, `route_two_pin()`'s
drawn-metal short check (above) reads the two ports' own block's GDS stream a
second time — not to re-derive placement, but to read its *drawn* shapes on
the route layer as routing obstacles, since a port's reported `width_um` can
under-state the real pad it draws. That read happens lazily and is cached
once per block, only when `connectivity[]` contains a self-net.

**A north/south-facing port's stub is widened past a wider pad (#496,
fixed).** The Manhattan backbone's stub — the segment leaving a port along
its own `direction_deg` before the perpendicular jog — used to be drawn no
wider than `routing.width_um`, even when the port's own reported `width_um`
(its drawn pad's extent) was larger. For a port that faces north or south,
that left a slit between the pad's far edge and the jog's underside, outside
the stub's own narrow footprint but inside the pad's — narrower than the
target deck's same-layer spacing rule whenever `routing.width_um` is
narrower than the pad (a `mos_array`/`diff_pair` gate contact's landing
pad, a `guard_ring`/`bjt_array` ring tap, an S/D pad reached from above —
nothing about the shape is specific to any one generator). `route_two_pin()`
now widens just that stub segment — from the port out to wherever the
un-widened stub already ended — to `max(routing.width_um, that port's own
reported width_um)`, mirroring the via-drop landing pad's own precedent
(sized independent of the route's own trace width, for the same enclosure
reason). Purely geometric, not port-name special-cased: it fires for any
port whose own reported `width_um` exceeds `routing.width_um`. Scoped to
north/south-facing ports only — an east/west-facing port's horizontal stub
is unaffected — and to an endpoint whose own reported layer *is*
`routing.layer_role`: a port that instead needs a via-drop (its real pad
lives on a different layer) is unaffected too, since the widened metal is
drawn on `routing.layer_role`, which is not that pad's own layer there.

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
| `pdk.variant`/`pdk.root` | string \| null | The exact fields `klt pdk find --pdk`/`--pdk-root` accept ([`docs/cli/pdk.md`](pdk.md)) — resolved through that one resolver, never a private lookup. `pdk` accepts **only** these two keys — an unrecognised key (e.g. `name`, a plausible typo for `variant`) is an application error (exit 1) naming the offending key(s), not a silent fallback to `$PDK`/the default search order. |
| `blocks[]` | array\<object\> | Each already-generated primitive to place — see below. |
| `blocks[].id` | string | Caller-chosen label used to address the block's ports elsewhere in this request (`placement.order`, `connectivity[].pins[].block`). Must be unique within `blocks[]`. |
| `blocks[].generator_report` | object \| string | The block's own [`klt gen`](gen.md) JSON response — either an inline object, or a path to a file holding one (mirrors `klt gen --params`'s own path-or-inline duality). A relative path string resolves against **the request file's own directory** (not the process's current working directory), matching `klt lvs`'s request-relative path convention — an absolute path is unaffected. When `compose()` is called directly as a library (no request file at all), relative paths resolve against the process's current working directory instead. This command's only input about a block's geometry is its already-reported `bbox_um`/`ports[]`/`cell_name`/`gds_path` — never a second, private inspection of the GDS stream at request-parse time. |
| `placement.strategy` | string | `"row"` (single horizontal row, left to right in `order`, spaced by `spacing_um`), `"explicit"` (#321 — each block placed at its own declared `origins_um[id]`), or `"array"` (#1053 — the one `blocks[]` entry named in `order` repeated on a `rows` x `cols` grid). Any other value (e.g. `"grid"`, reserved by the spike for a different, still-unimplemented feature) is an application error (exit 1). |
| `placement.order` | array\<string\> | Block `id`s in placement order. Every `id` in `blocks[]` must appear exactly once — a missing or extra/unknown `id` is an application error. **Under `strategy: "array"`, `blocks[]`/`order` must contain exactly one entry** — the single block repeated at every tile; more than one is an application error. Response `blocks[]` ordering follows `order` under every strategy. |
| `placement.spacing_um` | number | Fixed gap between adjacent blocks' bounding boxes. Must be `>= 0`. **Only read under `strategy: "row"`** — ignored (not an error) when present alongside `strategy: "explicit"` or `"array"`. |
| `placement.origins_um` | object | **Required when `strategy: "explicit"`**, otherwise not read. Maps every `placement.order` block `id` to its own `{"x": number, "y": number}` origin — that block's `offset_um`, applied exactly like a `"row"` offset (added directly to the block's own reported `bbox_um`; see "`blocks[]` entries" below). The key set must equal `order` exactly — a missing, extra, or unknown `id` is an application error (exit 1), as is a non-numeric `x`/`y`. |
| `placement.rows`/`placement.cols` | integer | **Required when `strategy: "array"`** (#1053), otherwise not read. The grid's row/column counts — each must be a positive integer (`>= 1`); a non-integer, zero, or negative value is an application error (exit 1). |
| `placement.row_pitch_um`/`placement.col_pitch_um` | number | **Required when `strategy: "array"`**, otherwise not read. The fixed spacing between adjacent tile origins along each axis — each must be `> 0` (a zero or negative pitch is an application error, exit 1, even for a degenerate `rows: 1` or `cols: 1` array, where the corresponding pitch is otherwise unused geometrically). |
| `placement.origin_um` | object | Optional, **`strategy: "array"` only** — the base (row 0, col 0) tile's own `{"x": number, "y": number}` origin, i.e. that block's `offset_um`. Defaults to `{"x": 0.0, "y": 0.0}` when omitted, mirroring `"row"` placement's own implicit first-block origin. A non-numeric `x`/`y` is an application error (exit 1). |
| `connectivity[]` | array\<object\> | One entry per net: a `net` label (caller-chosen, response traceability only) and `pins[]` (at least 2), each `{block, port}` addressing one named port from that block's own `generator_report.ports[]`. A **2-pin** net is routed point-to-point (see "Scope"); a **>2-pin** (bundle) net is left unrouted this phase. A `pins[].block`/`pins[].port` referencing a nonexistent block `id` or port name is an application error (exit 1). |
| `connectivity[].waypoints_um` | array\<array\<number\>\> | Optional, **2-pin nets only**. An ordered, non-empty list of `[x_um, y_um]` points (composed-frame coordinates) the backbone is forced through, between port `a`'s own stub and port `b`'s own stub — see "Routing same-facing port pairs with `waypoints_um`" below. A malformed entry (not an array, not length-2, a non-numeric coordinate) is an application error (exit 1). Omitting it changes nothing (today's fixed one-jog/corner shape). |
| `pins[]` | array\<object\> | Optional. One entry per single-pin top-level net to label **without routing** (#210) — e.g. a device gate, a bias/supply pad. Omitting it entirely changes nothing. Each entry names **exactly one** port (unlike `connectivity[]`'s 2+ `pins`). See fields below. |
| `pins[].net` | string | Caller-chosen net name written as the `kdb.Text` label on the port, and echoed in the response. Required and non-empty. |
| `pins[].block` | string | A `blocks[].id`. Referencing an unknown `id` is an application error (exit 1). |
| `pins[].port` | string | A port name from that block's own `generator_report.ports[]`. An unknown port is an application error (exit 1). A `(block, port)` that also appears in any `connectivity[]` entry is rejected (exit 1) — the router already labels that shape. The label lands at the port's own composed-frame position on the label layer paired with the port's own drawn layer; a port on a layer with no label convention is not labelled (a `drc_hints.notes[]` partial-success note, not an error). |
| `routing.layer_role` | string | A layer *role* (e.g. `"metal"`) resolved through the **same** per-PDK-family role→layer table every [`klt gen`](gen.md) generator uses — never a raw `{layer, datatype}` pair. **Required** (and must name a role the resolved PDK family actually has a layer for) when `connectivity[]` is non-empty; otherwise ignored. `"metal2"` (#454) runs the backbone on the family's second routing-metal level instead, via-dropping back to each pin's own `"metal"`-role pad through the connecting `"via1"` role — see "Via-drop routing (metal2/via, #454)" below. |
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
| `min_spacing_um` | number \| null | The tightest spacing actually used across placement and routing (the placement gap when any net was routed) — **`"row"` placement only**. `null` when no `connectivity[]` was supplied (nothing was routed, so no routing/placement spacing was exercised as a clearance), and always `null` under `"explicit"` (#321) or `"array"` (#1053) placement — neither has a single shared spacing value to report (`"explicit"`'s per-pair separation is exactly what a caller-declared origin expresses; `"array"` has two independent pitches, `row_pitch_um`/`col_pitch_um`, not one). |
| `matched_groups[]` | array\<object\> | One entry per distinct `matched_group_id` seen among the input blocks' own `generator_report.drc_hints.matched_group_id` (in first-seen order): `matched_group_id` (echoed), `blocks` (the request-level block `id`s carrying it), and `placement_symmetric` (always `null` this phase — symmetry *verification* against a declared symmetry axis is out of scope). Empty when no input block carries a `matched_group_id`. |
| `notes[]` | array\<string\> | Free-form composition notes — e.g. why a specific net was left unrouted (narrow channel, or a bundle net deferred this phase). Always present, empty when there is nothing to report. |

#### `blocks[]` entries

| Field | Type | Description |
| ----- | ---- | ----------- |
| `id` | string | Echo of the request's `blocks[].id`. |
| `generator` | string | Echoed from that block's own `generator_report.generator`. |
| `offset_um` | object | `{x, y}` — the translation applied to place this block. Under `"row"`, the first block always has `offset_um: {x: 0.0, y: 0.0}`; every subsequent block is translated along `x` only (row placement never translates `y`) so its bbox sits exactly `placement.spacing_um` past the previous (already translated) block's right edge — regardless of that block's own `bbox_um.x0` (which need not be `0`; a guard-ringed block's bbox can extend to negative coordinates). Under `"explicit"` (#321), `offset_um` is exactly the request's own `placement.origins_um[id]`, verbatim — a block's own `bbox_um` plays no role in computing it (an explicit origin translates a block's bbox by that amount; it does not force the bbox's own `(x0, y0)` corner to land exactly on the declared origin unless that block's own `bbox_um.x0`/`y0` is already `0`). Under `"array"` (#1053), `offset_um` is exactly `placement.origin_um` (the base, row-0/col-0 tile) — every *other* tile's own position is implied by `rows`/`cols`/`row_pitch_um`/`col_pitch_um` rather than reported as a separate `blocks[]` entry (there is still exactly one `blocks[]` entry for an `"array"`-placed block, echoing this base tile). |
| `bbox_um` | object | That block's own `generator_report.bbox_um`, translated by `offset_um`, in the composed cell's coordinate frame — **except under `"array"`** (#1053), where `bbox_um` is instead the union bounding box of *every* placed tile (all `rows * cols` instances), matching the top-level `bbox_um` field above when this is the only block in the request. |

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
| `1` | Application error — unresolvable PDK, an unrecognised `pdk` key (anything other than `variant`/`root`), malformed request (missing/invalid `blocks[]`, `placement.order` not matching `blocks[]`, negative `spacing_um`, a missing/mismatched/non-numeric `placement.origins_um` when `strategy: "explicit"` (#321), more than one `blocks[]` entry or a missing/non-positive `rows`/`cols`/`row_pitch_um`/`col_pitch_um`/non-numeric `origin_um` when `strategy: "array"` (#1053), or a missing/invalid `routing.layer_role`/`routing.width_um` when `connectivity[]` is non-empty), an unsupported `placement.strategy`, a `connectivity[]` or `pins[]` entry referencing a nonexistent block `id`/port, a `pins[]` entry naming a `(block, port)` already used by a `connectivity[]` net, a block's `generator_report`/GDS could not be read, or the `options.output` directory does not exist. |
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
overlapping geometry and no error from this command. `klt drc` is the
authority for illegal *shapes* on the composed output, but it is not a
complete backstop for placement mistakes: two same-layer shapes placed with
zero clearance merge into one polygon, which is not a spacing violation by
any rule (there is no gap left to measure once they're unioned), so `klt
drc` reports 0 violations on exactly the case that matters most — a block
placed flush against a neighbour's edge. That kind of short only otherwise
surfaces later, downstream, via `klt extract`'s `merged_net_labels`
diagnostic.

To close that gap without turning `"explicit"` placement into a hard
validator, `gen-compose` adds one advisory check (#692): for every ordered
pair of distinct blocks `(A, B)` where `A`'s own `generator_report`
(`drc_hints.min_spacing_um`) declares a positive minimum spacing, the actual
placed clearance between `A` and `B` is compared against it. If the
clearance is smaller, the response's top-level `warnings[]` gets one entry
naming both blocks, the declared minimum, and the clearance actually found —
composition still succeeds (this never raises, and never blocks output), so
existing overlapping-origin requests keep working exactly as before:

```json
{
  "warnings": [
    "block 'core' is placed 0.00um from block 'ring' (strategy: explicit), closer than ring's own declared drc_hints.min_spacing_um of 1.00um"
  ]
}
```

This warning is scoped to `strategy: "explicit"` only — `"row"` placement's
own uniform `spacing_um` does not have the same "silently flush against a
declared-hint neighbour" trap, so it gains no new warnings. It is also only
as good as the input: a block whose `generator_report` doesn't report a
`drc_hints.min_spacing_um` (or reports `0`) triggers nothing, so this is a
courtesy for generators (like `guard_ring`) that do report one, not a
general-purpose spacing check.

## Array placement (a repeated-block regular tiling, #1053)

`"row"` and `"explicit"` both place a distinct block once each. Neither
expresses a **two-dimensional regular array of one repeated block** — the
placement pattern behind any row/column-tiled structure (a matched-device
array, a memory bitcell array, a pad ring, anything built from one cell
repeated on a uniform X/Y pitch). Composing an `R` rows x `C` columns tiling
of one block via `"explicit"` would mean emitting `R*C` individual placement
entries, each carrying its own duplicated origin, even though the whole
placement is fully described by four numbers (a base origin, a row pitch, a
column pitch, and the row/column counts) plus the one block reference
repeated at every position.

`"array"` takes exactly that shape — mirroring `klayout.db.CellInstArray`'s
own row-vector/column-vector/row-count/column-count parameterization — and
composes it as a **single hierarchical instance** rather than `rows * cols`
flattened placements:

```json
{
  "pdk": { "variant": "sky130A" },
  "blocks": [
    { "id": "bitcell", "generator_report": "bitcell.json" }
  ],
  "placement": {
    "strategy": "array",
    "order": ["bitcell"],
    "rows": 16,
    "cols": 8,
    "row_pitch_um": 5.0,
    "col_pitch_um": 3.0,
    "origin_um": { "x": 0.0, "y": 0.0 }
  },
  "options": { "cell_name": "bitcell_array_0", "output": "bitcell_array_0.gds" }
}
```

The response's `blocks[]` still has exactly one entry (echoing the single
`blocks[]`/`order` id an `"array"` request takes): `offset_um` is the base
(row 0, col 0) tile's own origin (`placement.origin_um`, verbatim), and
`bbox_um` is the union bounding box of **every** placed tile — `cols` steps
of `col_pitch_um` along `+x` and `rows` steps of `row_pitch_um` along `+y`
from `origin_um`, not just the base tile's own bbox. The composed GDS's top
cell gets exactly one `kdb.CellInstArray` instance for `bitcell` covering all
128 (`16 * 8`) tiles, confirmed by inspecting `layout.cell(cell_name).each_inst()`'s
own count (`== 1`) rather than only the rendered geometry — a flattened
`rows * cols`-insert implementation could look visually identical while
failing this requirement.

Three things `"array"` deliberately does not do (see "Scope" above):
- **Only one block.** `blocks[]`/`placement.order` must have exactly one
  entry — a caller composing several distinct blocks alongside a repeated
  array needs a separate `gen-compose` request per block (or a follow-on
  request that reads this one's own `gds_path` as an input, once that
  composition-of-compositions capability exists).
- **No orientation.** Like `"row"`/`"explicit"`, every tile is a translation
  only — no per-tile rotation or mirroring.
- **No per-tile `connectivity[]`/`pins[]` shorthand.** `connectivity[]` and
  `pins[]` still address the array-placed block by its one `blocks[].id`, so
  they can only reach the **base** (row 0, col 0) tile's own ports — there is
  no request-level way to wire a shared net (e.g. power, a shared control
  signal) to every tile in the array. This is a deliberate, documented gap:
  a first-class "route this net to every instance in the grid" shorthand is
  a separate follow-on question `"array"` placement does not need to answer
  to be useful on its own (a caller with a fully regular tiling still saves
  the `O(rows * cols)` request-size cost `"explicit"` would otherwise impose,
  even before grid-aware routing exists).

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
