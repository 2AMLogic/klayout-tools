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
    placement. **One thing `"explicit"` does not do**: it performs no
    overlap validation of its own — an overlapping or abutting pair of
    declared origins composes successfully; `klt drc` remains the
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
    application error); every tile shares that one entry's own `orientation`
    uniformly (no *per-tile* override).
  - **Block orientation** (`blocks[].orientation`, #1166) — a per-block
    mirror/rotation, orthogonal to (composes with) every strategy above; see
    "Block orientation (mirror/rotate, #1166)" below.

  `"grid"` is a *different*, still-unimplemented feature reserved by the
  accepted spike for a later phase (a row-wrap layout of *distinct* blocks
  into `placement.cols` columns) — deliberately not the name used for
  `"array"` above, to avoid colliding with that reservation.
- **Routing** — point-to-point Manhattan routing between the named ports
  listed in `connectivity[]`, requested by supplying `routing.layer_role`/
  `routing.width_um`. Each 2-pin net is drawn as a native `pya.Path`
  (backbone → corner bends → straight fill) on the resolved
  `routing.layer_role` layer at `routing.width_um` width; `nets[]` reports
  `routed` and `route_length_um` per net. A net the router cannot connect is
  reported in `unrouted_nets[]` (a **partial success**, exit code `3`), not a
  hard failure.
  - **`routing.width_um` is floored by the resolved PDK deck's own
    minimum-width rule (issue #1501).** The floor is looked up in the same
    `ExtractionDeck`/`DrcRule` set `klt drc --deck <family>` judges the
    composed layout with — never a second, private threshold — against the
    requested `routing.layer_role`. A `width_um` narrower than that floor is
    an application error (exit `1`) naming the violated rule id and its
    threshold in µm, rather than silently drawing sub-minimum metal (e.g.
    gf180mcu's `metal1.width.1` = `0.23um` rejects the documented `0.17um`
    default). Each via-drop's own square (`_VIA_DROP_SIZE_UM` by default) is
    floored the same way against the drop's `via_layer`, so a family whose
    via-width minimum exceeds that constant (e.g. gf180mcu's `via1.width.1` =
    `0.26um`) still draws a DRC-clean via.
  - **`routing.cross_block_width_um` is the cross-block plane's own width
    knob, independent of `routing.width_um` (issue #1620).** Before this,
    `routing.width_um` was validated against *both* `routing.layer_role`'s
    floor and (whenever `routing.cross_block_layer_role` was configured) that
    second layer's own floor — so naming a cross-block layer with a stricter
    deck minimum silently forced every net's *primary*-plane routing wider
    too, on a knob the caller never asked to change. `routing.width_um` is
    now validated against `routing.layer_role`'s own floor only, and a leg
    that actually falls back to `routing.cross_block_layer_role` draws at
    `routing.cross_block_width_um` instead — defaulting to that layer's own
    deck minimum when omitted (so a caller wanting "minimum pitch on each
    plane" need not spell either width out), and floored against that
    layer's own minimum-width rule the same way `routing.width_um` is, with
    the identical error shape (naming `routing.cross_block_width_um`, the
    violated rule id, and its threshold). Meaningless, and ignored, without
    `routing.cross_block_layer_role` also configured.
- **Declare-only connectivity (no `routing`, #1188)** — `routing` is
  optional: omitting it (or passing `{}`) with a non-empty `connectivity[]`
  still validates every net's `{block, port}` pins against the referenced
  blocks' own reported ports, but draws no metal. Every net comes back with
  `status: "unrouted"` and `reason: "routing not requested"` on each leg
  (distinct from a geometry-based rejection reason), and lands in
  `unrouted_nets[]` — a **partial success**, exit code `3`, same as an
  unroutable net. Useful for validating a composition's intended net list
  (a typo'd or stale port name still fails the request, exit `1`) without
  requiring the caller to also draw routing metal — e.g. a placement-only
  floorplan, or a request whose nets a router cannot draw anyway. Supplying
  *any* key of `routing` opts back into routing, and both `layer_role`/
  `width_um` become required at that point (unchanged from before #1188).
- **Bundle (>2-pin) routing (#1073)** — a `connectivity[]` net with three or
  more pins (a shared supply/ground rail, a bias line, a clock, any fanout
  node) is routed as a **spanning tree of two-pin legs**: every unordered pin
  pair is a candidate leg, candidates are tried nearest-first (Manhattan
  distance between the two ports' composed-frame positions, ties broken by
  declaration order so the output stays byte-reproducible), and a leg is
  accepted when it joins two so-far-disconnected parts of the net. For the
  canonical rail case — one supply port per block across a placement row —
  that yields exactly a trunk: a chain of adjacent-block legs. Consequences
  worth knowing:
  - **Every leg is routed by the same two-pin router**, so all of its
    routability checks (channel width, guard/collector ring, self-net pad
    crossing, self-net drawn-metal short, obstacle overlap, via-drop
    resolution) apply per leg. A leg one of them rejects is skipped in favour
    of the next candidate joining the same two parts, so a net routes around
    an individually unroutable pair whenever another spanning tree exists.
  - **`pins[]` order is not a routing order.** The tree is built
    nearest-first, so a rail declared in an arbitrary block order routes the
    same way as one declared left to right.
  - **Partial routing when the pins cannot all be joined (issue #1169).** The
    net is still reported in `unrouted_nets[]` (it is not *fully* connected),
    but every leg the spanning-tree search *did* accept is drawn — only the
    pins left stranded (and any candidate leg rejected on the way to reaching
    them) stay undrawn. `nets[].status` is `"routed"` (fully connected),
    `"partial"` (at least one leg drawn but not fully connected), or
    `"unrouted"` (no leg accepted) — the caller must read this field rather
    than infer it from `legs[].routed` counts, since both a partial and a
    fully-unrouted net report `nets[].routed: false`. `nets[].legs[]` reports
    every attempted leg with its own rejection reason (or `routed: true` and
    no reason, for a drawn one), so the failure is per leg, never a blanket
    "this net is too big". (Before #1169, a net that could not be fully
    connected was all-or-nothing: any spanning-tree failure discarded *every*
    leg's geometry, including legs that were individually routable — this
    made debugging a real composition failure harder than necessary, for no
    DRC benefit, since a leg that already passed its own routability checks
    cannot become unsafe just because a different leg of the same net
    failed.)
  - **One net label, not one per leg** — the legs are one conductor, so the
    net gets exactly one `kdb.Text` (same as a 2-pin net's single path).
  - `waypoints_um` (#634) steers a single backbone and is therefore only
    accepted on a **2-pin** net; supplying it on a >2-pin net is an
    application error (exit `1`), never a silently ignored field. Use
    `legs[]` (#1529, below) to steer one or more individual legs of a
    bundle net by name instead.
  - **`legs[]` (#1529)** — an optional array of
    `{from_pin, to_pin, waypoints_um}` objects, each naming one leg of
    *this* net (`from_pin`/`to_pin` must match two of the entry's own
    `pins[]`) and, optionally, the same `[x_um, y_um]` waypoint list
    `waypoints_um` accepts for a 2-pin net. Every named leg is routed and
    seeded into the spanning tree **before** the automatic nearest-first
    search runs, so a caller can hand-route one or more legs of a large
    bundle net (steering around pre-existing geometry, or simply forcing a
    specific pair together) while every pin not named in `legs[]` still
    completes automatically. See "Hand-routing individual legs of a bundle
    net with `legs[]`" below for the full worked example, including why
    this removes the need for the N-entry, pin-adjacent decomposition the
    accepted-leg overlap check otherwise demands.
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
  met1↔met2 via, `68/44`) — usable the same way, from a pin already on
  `"metal2"` (met1, one via hop away) via `"via2"` directly, or from a pin
  still on the base `"metal"` role (li1, two hops from `"metal3"`) via the
  full two-hop via-drop *ladder* (below, issue #1567) — `"via1"` then
  `"via2"`, with a landing pad on met1 in between. gf180mcu's own deck
  exposes the same third level (#1058): `"metal3"` (Metal3 `42/0`) plus its
  own connecting via role (`"via2"`, the Metal2↔Metal3 via, `38/0`) — usable
  the same way, including the two-hop ladder down to the base `"metal"` role
  (Metal1). gf180mcu's deck exposes one level further still (#1670):
  `"metal4"` (Metal4 `46/0`) plus its own connecting via role (`"via3"`, the
  Metal3↔Metal4 via, `40/0`) — the layer a `klt place-and-route`-produced
  macro's own top-level pins routinely land on (OpenROAD's global router
  picks the pin escape layer) — usable the same way, including the
  three-hop ladder down to the base `"metal"` role (Metal1) via
  `"via1"`/`"via2"`/`"via3"`. IHP-Open-PDK's
  `sg13g2`/`sg13cmos5l` (issue #1474) expose the identical two-level shape:
  `"metal2"` (Metal2 `10/0`) plus `"via1"` (Via1 `19/0`, the Metal1↔Metal2
  via) and `"metal3"` (Metal3 `30/0`) plus `"via2"` (Via2 `29/0`, the
  Metal2↔Metal3 via) — both families resolve to the identical four values,
  since `sg13cmos5l`'s own Metal1-TopMetal1 stack is a documented prefix of
  `sg13g2`'s deeper Metal1-TopMetal2 stack.
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
- **Blocks this command did not generate (#1189)** — a `blocks[]` entry names
  its geometry source in exactly one of two ways. `generator_report` is a
  `klt` verb's own JSON response — [`klt gen`](gen.md), [`klt draw`](draw.md),
  or **`klt gen-compose` itself**, whose response now reports
  `generator: "gen-compose"` plus a `ports[]` promoted from its own `pins[]`,
  which is what makes composition **nest** instead of being one flat level.
  `cell` instead names a cell that **already exists** in a stream — a PDK
  standard cell, a hand-drawn library cell — as
  `{gds_path, cell_name, ports, bbox_um}`, with `bbox_um` read straight from
  the stream when omitted. Neither case needs a hand-forged report with a fake
  `generator` field, and neither needs the caller to re-key `klt cells`'
  `{left, bottom, right, top}` bbox into this command's `{x0, y0, x1, y1}`.
  See ["Hierarchical composition and library cells
  (#1189)"](#hierarchical-composition-and-library-cells-1189) below.
- **`drc_hints`** — `matched_groups[]` reports every distinct
  `matched_group_id` seen among the input blocks (read-only echo of
  `generator_report.drc_hints.matched_group_id`, `placement_symmetric: null` —
  symmetry *verification* is out of scope this phase); `min_spacing_um` reports
  the tightest spacing actually used across placement and routing.
- **Geometry is advisory.** A routed net (`routed: true`) is *not* a DRC-clean
  guarantee — `klt drc` remains the rule-compliance authority on the composed
  output, exactly as it is on any single generator's output. **This is not a
  theoretical caveat: run `klt drc` after every composition that draws
  metal, unconditionally.** `nets[].legs[].routed: true` means route_two_pin's
  own routability heuristics and the route-vs-route collision check (#1057,
  spacing-aware since #1386 — see "Route-vs-route collision is spacing-aware"
  below) found no *problem they know how to look for*; it is not an
  exhaustive DRC pass, and every `klt gen-compose` release to date has fixed
  at least one class of violation that heuristic set previously missed (#453,
  #1057, #1197, #1386). Two independently DRC-clean input blocks composed
  together, or two individually-routed nets that each look fine in
  isolation, are not guaranteed to stay DRC-clean together once placed
  side by side — a caller pipeline that treats `routed: true` as "this leg
  needs no further check" and skips the follow-up `klt drc` run is not
  supported and will eventually compose a violation this command did not
  catch.
- **`unrouted_nets: []` plus a clean `klt drc` is not a connectivity
  guarantee either — read this the other way round from the bullet above
  (issue #1527).** The bullet above is about *rule compliance*: a leg that
  reports `routed: true` can still fail `klt drc`. The risk this bullet is
  about is the opposite and, for a coordinate-tapped composition, the more
  dangerous one: a leg that is both `routed: true` **and** `klt drc`-clean
  can still be *electrically wrong*. Two overlapping shapes on the same
  layer merge into one polygon in the output GDS — a **short**, not a
  spacing violation — so no rule deck, however complete, can see it; only
  `klt extract`'s netlist (net count, merged label sets) can. This matters
  specifically when a `blocks[].cell` block's net is tapped by
  **coordinate** (`blocks[].cell.ports[]` names a point on the block's own
  internal wire, since a pre-existing stream never reports its own
  `ports[]` — see "Hierarchical composition and library cells (#1189)"
  below): the one region a coordinate-tapped leg is guaranteed to draw metal
  in — its own approach stub, inside the block it taps — is checked against
  that block's *other* drawn geometry only since #1527 (see "An inter-block
  leg's own approach stub is no longer a silent short to its own block"
  below), and only for a `blocks[].cell` endpoint, not a `generator_report`
  one (a generator can legitimately draw real, unreported geometry near a
  port — e.g. `mos_array`'s own `dummy` matching columns, already excluded
  from `klt extract`'s netlist by its own dummy-suppression convention —
  that this heuristic cannot tell apart from an actual obstacle). A clean
  `unrouted_nets: []` plus a clean `klt drc` run is therefore still not
  proof of the intended connectivity; a net-by-net `klt extract` diff
  against the previous composition (device counts, merged label sets) is
  the only proof, and is worth the extra step precisely because the two
  signals a caller naturally trusts both say "fine" when they are not.

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
(An unrelated block sitting between the two pins — the *third*-block case,
as opposed to these two — is no longer among them: #1167 routes around it,
see "Routing around an unrelated block" below.)
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
  (A narrower, `blocks[].cell`-scoped exception to "not aware of internal
  geometry" was added later — see "An inter-block leg's own approach stub
  is no longer a silent short to its own block (#1527, fixed)" below.)
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
  **detects**; what it does with a detection depends on *which* block was
  clipped — an unrelated one is now routed around (#1167, below), while a
  near-miss against one of the net's own two blocks still reports the net
  unroutable, so the same workarounds (an `add_guard_ring: false` parameter,
  opposite-facing port pairs, or `waypoints_um` with several microns of
  clearance from every block) still apply there.
- **The obstacle-overlap check above is layer-scoped, not layer-agnostic
  (#1656, fixed).** The bbox/margin heuristic described above used to test a
  leg's backbone against every *other* placed block's `bbox_um` regardless of
  which layers that block actually draws shapes on — so a route on, say,
  `metal3` (a router-only role no `klt gen` generator draws pads on) could be
  rejected (or, since #1167, detoured around) for "crossing" a neighboring
  block's bbox even when that block draws nothing at all on `metal3`: no
  physical short is possible, only a bbox intersection on paper. `klt
  gen-compose` now reads each candidate obstacle block's own drawn geometry
  on the leg's `effective_route_layer` (the same `read_block_layer_geometry`
  helper #1520/#1527 already use for a leg's own endpoint block, here reused
  read-only for *any* placed block) and exempts a block from the
  obstacle-overlap check outright when it draws nothing there — a bbox
  crossing against such a block is a false positive, not a real obstacle. A
  block that *does* draw something on `effective_route_layer` is still
  rejected/detoured around exactly as before; this only removes false
  positives, it does not weaken the check against a genuine obstacle.
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
- **Hand-routing individual legs of a bundle net with `legs[]` (#1529,
  fixed).** `waypoints_um` above steers a single backbone, so it is only
  accepted on a 2-pin `connectivity[]` entry (see "Bundle (>2-pin) routing"
  above) — a bundle net has no single backbone for a caller-supplied path
  to belong to. Before this, the only way to hand-route part of a bundle
  net was to decompose it into several 2-pin `connectivity[]` entries
  sharing one `net` name — which then had to stay **pin-adjacent** (each
  entry sharing a pin with the next, forming a chain) to avoid a separate,
  easy-to-trip footgun: the accepted-leg route-vs-route collision check
  (see "Two distinct nets whose backbones cross are no longer a silent
  short" below) exempts two legs from being compared only when they
  **share a pin** — an intended merge, not a short. Two entries of the
  *same* net that don't happen to share a pin (a trunk-and-branch
  decomposition, two sub-chains later bridged) were compared as if they
  belonged to *different* nets, and rejected with a
  `"crosses already-routed net '<net>'"` message that reads like a
  contradiction when both sides are the same net.

  `connectivity[].legs[]` removes the need for that decomposition. Each
  entry is `{"from_pin": {block, port}, "to_pin": {block, port},
  "waypoints_um": [[x, y], ...]}` — `from_pin`/`to_pin` must each match one
  of *this* connectivity entry's own `pins[]` (not merely a valid port
  anywhere in the request), and `waypoints_um` is optional per leg (omit it
  to force that specific pin pair into the spanning tree without steering
  its path). Every named leg is routed through the same
  `route_two_pin()`/routability-check path as any other leg — nothing is
  exempted, only steered — and is seeded into the spanning tree **before**
  the automatic nearest-first search runs, so any pin the caller's
  `legs[]` doesn't cover still completes automatically. Because every named
  leg of one `legs[]` array belongs to the *same* `route_bundle()` call,
  they are never compared against each other by the route-vs-route
  collision check in the first place (that check only ever compares a
  candidate leg against an *already-committed, different* net's regions) —
  so two non-adjacent legs of one bundle net, even ones whose drawn
  footprints overlap, route cleanly with no pin-adjacency ordering required.
  `waypoints_um` (top-level) and `legs[]` are mutually exclusive on one
  `connectivity[]` entry — an application error (exit `1`) if both are
  supplied; express a 2-pin net's own steered path as a single-entry
  `legs[]` instead if it also needs a named `from_pin`/`to_pin` leg.

  ```json
  {
    "net": "VDD",
    "pins": [
      { "block": "cellA", "port": "VDD" },
      { "block": "cellB", "port": "VDD" },
      { "block": "cellC", "port": "VDD" }
    ],
    "legs": [
      {
        "from_pin": { "block": "cellA", "port": "VDD" },
        "to_pin": { "block": "cellB", "port": "VDD" },
        "waypoints_um": [[10.0, 25.0], [40.0, 25.0]]
      },
      {
        "from_pin": { "block": "cellB", "port": "VDD" },
        "to_pin": { "block": "cellC", "port": "VDD" }
      }
    ]
  }
  ```

  Here the `cellA`–`cellB` leg is steered around a specific obstacle (an
  explicit detour at `y=25.0`); the `cellB`–`cellC` leg names the pair to
  connect but omits `waypoints_um`, leaving its path to the default
  backbone. A pin left out of `legs[]` entirely (not shown above) would
  still be picked up by the automatic spanning-tree search, exactly as it
  is with no `legs[]` at all.

  A named leg is *steered, not exempted*, so it can still be rejected (its
  path crosses an unrelated block, or collides with an already-routed
  net). That does not fail the net on its own — the automatic search still
  runs and may connect those two pins another way — but, unlike a rejected
  auto-selected candidate, **a rejected named leg is still reported** in
  `nets[].legs[]` (`routed: false`, with its own `reason`) even when the
  net as a whole comes back `status: "routed"`. A caller who supplied
  `legs[]` must therefore check `nets[].legs[].routed`, not just
  `nets[].routed`, to confirm the path it asked for is the path that was
  drawn.
- **Routing around an unrelated block, not just detecting it (#1167,
  fixed).** The obstacle-overlap check above used to reject *any* backbone
  crossing a third block's bbox, so in a row only **immediately adjacent**
  blocks could be wired at all — a real block's netlist is not a Hamiltonian
  path over its devices, so most of its nets never had a chance (issue #1164
  measured 0/8, 0/9 and 0/9 nets routed across three real gf180mcu blocks).
  When the fixed-shape backbone is rejected **solely** for crossing blocks
  neither pin sits on, the router now retries the same pin pair around them:
  up to **two alternate lanes** (a straight run over/under, or left/right of,
  every block in the way, shortest detour tried first) are routed exactly as
  if the caller had supplied them as `waypoints_um`, and the first that
  passes every check is drawn. Consequences worth knowing:
  - **Nothing is waived to make a detour fit.** A lane goes through the same
    six routability checks as any other path, so a detour that would cross a
    ring, a pad, a block's drawn geometry — or another block's bbox — is
    rejected like any other backbone. The check that used to reject these
    nets still rejects them; it just no longer gets the last word.
  - **The search is bounded, and its limit is on *lanes*, not obstacles.** A
    lane is placed clear of every block it spans, so two blocks between the
    pins cost one lane, not two nested detours; and a lane is never itself
    detoured around (one level, at most two extra attempts per net). When
    neither lane is clear, the net is still reported in `unrouted_nets[]`,
    with the same reason as before plus a note that a detour was tried — so
    "there was no way around" is distinguishable from "no attempt was made".
  - **A route that crosses one of its own two pins' blocks is still
    rejected**, not detoured: that is a statement about which way the two
    ports face (the same-facing pair above), whose remedy stays
    `waypoints_um`.
  - **A caller-supplied `waypoints_um` path is never second-guessed.** The
    caller owns that path; a supplied path that crosses a block is reported
    unroutable exactly as before, never silently replaced by a detour of the
    router's own choosing.
  - **Detoured routes are longer**, and their `route_length_um` reflects it —
    a lane clears each block it passes by `2 × routing.width_um` from that
    block's bbox edge, comfortably more than the `width_um / 2` the overlap
    check itself demands, so the drawn metal keeps a real spacing margin from
    whatever the block draws at its edge rather than being legal by a hair.
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
- **Two distinct nets whose backbones cross are no longer a silent short
  (#1057, fixed).** Every check above compares one `connectivity[]` entry's
  own backbone against *block* geometry -- none of them compared one entry's
  drawn backbone against *another already-routed* entry's. Two nets on the
  same `routing.layer_role` whose fixed (or `waypoints_um`-supplied, #634)
  backbones happened to cross could each independently pass every check
  above and both compose `routed: true`, silently shorting a pair the caller
  explicitly declared distinct. `compose()`'s connectivity loop now
  intersects each newly-accepted backbone (`_drawn_route_region`, the same
  helper #433's check above uses) against the union of every backbone already
  accepted earlier in the *same request* -- a positive-area overlap (a mere
  edge touch is a `klt drc` spacing question, not a short) reports the losing
  net **unroutable** (`unrouted_nets[]`, `routed: false`, a
  `drc_hints.notes[]` entry naming the net it crosses) instead of drawing it.
  This is **order-dependent**, consistent with every other check here: a net
  is compared only against whatever `routed_geometry` already exists at the
  time it is processed, so reversing `connectivity[]`'s order can flip which
  of the two nets "wins". Two entries that share a literal pin (`{block,
  port}`) — e.g. bussing three ports into one node via two chained 2-pin
  nets, as "Via-drop routing" below and #433's own worked reproduction both
  do — are exempt from this check: both backbones necessarily converge on
  the identical point from the identical direction there, so the resulting
  overlap is the caller's intended merge, not an accidental short.
  **This exemption is keyed on sharing a literal pin, not on sharing a
  `net` name (#1529).** A bundle net hand-decomposed into several 2-pin
  `connectivity[]` entries is checked one entry at a time, in declaration
  order — so two of those entries are only exempt from each other when
  their `pins[]` share a `{block, port}` pin. A trunk-and-branch
  decomposition (two chains later bridged, a branch hung off a mid-trunk
  pin the branch entry doesn't itself name, …) produces a pair of entries
  that share a `net` name but no literal pin, and that pair is compared as
  if it belonged to *different* nets — rejected with
  `"crosses already-routed net '<net>'"` even though both sides are the
  same net. Working within the decomposition means keeping every 2-pin
  entry pin-adjacent to the next so they form one unbroken chain — which
  constrains the *topology* the caller may express, not just its
  declaration order (a star, where every leg leaves one hub pin, is fine;
  two separate chains later bridged are not). The better fix, since #1529,
  is to declare the whole net as one `connectivity[]` entry and
  use `legs[]` (see "Hand-routing individual legs of a bundle net with
  `legs[]`" above) instead of decomposing it at all: every leg named in one
  entry's `legs[]` belongs to the same `route_bundle()` call, so this
  exemption's pin-sharing test never even runs between them.
- **Route-vs-route collision is spacing-aware, not just overlap-aware
  (#1386, fixed).** #1057 above only ever caught a literal positive-area
  overlap between two accepted backbones (plus their via-drop landing pads
  and stub-widen boxes, #1197) — two legs that never touch but are drawn
  *closer together* than the resolved deck's own same-layer minimum-spacing
  rule (e.g. sky130's `li1.space.1`/`met1.space.1`) both composed
  `routed: true` while the resulting GDS still failed `klt drc`, precisely
  because "do these two footprints overlap" is a strictly narrower question
  than "does the deck's own spacing rule allow this gap." The same check now
  also looks up that rule for each candidate leg's own effective drawing
  layer (`klt drc --deck <family>`'s own rule table — never a second,
  private threshold; a layer with no matching `"space"` rule keeps the
  pre-#1386 overlap-only behaviour unchanged) and inflates one side of the
  comparison by that threshold before re-testing — the standard "distance
  less than d" Minkowski-sum trick, so a false-negative "not touching, but
  too close" pair is now rejected (`unrouted_nets[]`, a
  `drc_hints.notes[]`/`legs[].reason` entry naming both the other net and
  the deck's own rule id, e.g. `"comes within 0.17um of already-routed net
  'NET_1' -- closer than the resolved deck's own 'li1.space.1' minimum
  same-layer spacing rule"`) instead of silently composing. This check is
  also now **layer-aware**: two accepted legs on genuinely different
  physical layers (e.g. one fell back to
  `routing.cross_block_layer_role` while another stayed on the primary
  `routing.layer_role`) can neither overlap nor violate a same-layer
  spacing rule against each other, so they are no longer compared at all.
  Like #1057, this remains an advisory heuristic, not a substitute for
  `klt drc` — see "Geometry is advisory" above.
- **A via-drop landing pad (or stub-widen box) is checked against its *own*
  block's other drawn geometry, not just against `bbox_um`/`ports[]` (#1520,
  fixed).** The two checks above only ever compare a candidate leg's own
  footprint against *other* nets' already-accepted geometry. Nothing
  previously checked the fixed-size landing pad a via-drop draws (or a
  stub-widen box, #496) against the geometry of the **same block it lands
  on** — a gap that matters specifically for a `blocks[].cell` block (an
  existing stream this command did not generate): the only way to declare a
  port on such a block is to hand-declare it on geometry the cell already
  contains, since a pre-existing stream never reports its own `ports[]`. A
  port declared on an internal wire is legitimate, but the fixed-size pad
  drawn there (`_VIA_LANDING_SIZE_UM`, independent of `width_um` — see
  "Via-drop routing" below) can land close enough to a *different* part of
  that same wire (e.g. a perpendicular leg near a corner the port sits close
  to) to violate the resolved deck's own same-layer spacing rule. The pad
  legitimately touches (merges with) the wire at its own declared port —
  that is never a violation — but the merged shape can still have a
  self-notch elsewhere, which is a real `klt drc` finding (a rule-deck
  `"space"` check is net-agnostic) even though both shapes are the same
  electrical node. This is now caught before anything is drawn: the net is
  reported **unroutable** (`unrouted_nets[]`, `routed: false`, a
  `legs[].reason` entry naming the block and the violated rule id) instead
  of silently composing `routed: true` with the violation left for a later
  `klt drc` run to discover. As with the two checks above, this is an
  advisory heuristic against the block's own drawn shapes on the pad's
  layer, not a substitute for `klt drc`.
- **An inter-block leg's own approach stub is no longer a silent short to
  its own block (#1527, fixed).** The self-net drawn-metal check above
  (#453/#469) only ever ran for a **same-block** self-net — a leg whose two
  pins sit on *different* blocks skipped it entirely, even though such a leg
  still draws an approach stub inside *each* endpoint's own block on its way
  out, exactly like a self-net's backbone does. Nothing checked that stub
  against the block's other drawn geometry: the whole-block bbox check
  (#199) above only ever modelled a pin's own block by its `bbox_um`, with
  an unavoidable margin (`_port_edge_margin_um`) exempting the approach stub
  from being flagged at all — so the one region a leg is *guaranteed* to
  draw metal in was the one region with no obstacle model. This mattered
  most for a `blocks[].cell` block tapped by **coordinate**: the declared
  port sits on one of the block's own internal wires (see "Hierarchical
  composition and library cells (#1189)" below), and an escape direction
  that happened to run across a *different* net already drawn inside that
  same block composed `routed: true` and DRC-clean (a metal-on-metal overlap
  merges into one polygon — a short, not a spacing violation, invisible to
  any rule deck) while `klt extract` silently merged the two nets onto one
  node. `route_two_pin()` now runs the same drawn-metal comparison the
  same-block self-net check above uses, once per endpoint whose own block is
  a `blocks[].cell` block: that endpoint's drawn leg against its *own*
  block's other drawn shapes on the route layer, excluding only the shape
  its own port lands on. A crossing is reported **unroutable**
  (`unrouted_nets[]`, `routed: false`, a `legs[].reason` entry naming the
  block) instead of drawn. **Deliberately scoped to `blocks[].cell`
  endpoints, not every block.** A `blocks[].cell` block's every `ports[]`
  entry is hand-declared by the caller directly onto the stream's own
  geometry, so "every merged shape my own port does not land on is a
  genuinely different net" is a sound assumption there. It is not sound for
  a `generator_report` block: e.g. `mos_array`'s own `dummy` matching
  columns (added by default) draw real, unreported metal pads flanking the
  array purely for layout matching, already excluded from the netlist by
  `klt extract`'s own dummy-suppression convention (#295/#462) — this check
  cannot tell that apart from an actual obstacle, so a `generator_report`
  endpoint is left to the coarser whole-block bbox check above, unchanged.
  As with the checks above, this is an advisory heuristic against the
  block's own drawn shapes on the route layer, not a substitute for `klt
  extract` — see "`unrouted_nets: []` plus a clean `klt drc` is not a
  connectivity guarantee either" above.

## Via-drop routing (metal2/via, #454)

Re-raising #433's Ask options 1/2 (the merged #433 fix implemented only
option 3, "fail visibly"): a family whose curated `ExtractionDeck` already
declares a second routing-metal level and the via that lands on it
(sky130's `EXTRACTION_DECK.metals=((67,20),(68,20),(69,20))` /
`.vias=((67,44),(68,44))`; gf180mcu's Metal1→Metal5 stack; IHP-Open-PDK's
`sg13g2`/`sg13cmos5l`, issue #1474, Metal1→Metal3 of their own
Metal1→TopMetal1/TopMetal2 stacks) now exposes that
second level as a second `routing.layer_role` (`"metal2"`) plus the via role
that connects it back to the base `"metal"` role (`"via1"`) — sourced
directly from the deck's own `metals`/`vias` tuples in
`klayout_tools.gen._PDK_ROLE_LAYERS`, never a second, private layer map.
sky130's deck now declares a third level too (met2, issue #508): `"metal3"`
plus `"via2"` (the met1↔met2 via) work the same way one level up. Before
issue #1567, a `"metal3"` backbone could only via-drop one level down (to a
pin already on `"metal2"`); a pin still on the base `"metal"` role, two
levels down, was rejected outright. Since #1567 the via-drop instead walks
the *full* `metals`/`vias` ladder between the two layers, so a `"metal3"`
backbone reaches a base-`"metal"` pin too — see "Multi-level via-drop" below.

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

One case draws a multi-level *ladder* instead of a single via, and one case
is rejected outright, reporting the net unroutable rather than drawing
something that does not connect:

- A pin whose layer is a *different* metals-stack level than
  `routing.layer_role`, more than one via hop away (issue #1567). Every
  family's third level (`"metal3"`/`"via2"`, sky130 issue #508, gf180mcu
  issue #1058, `sg13g2`/`sg13cmos5l` issue #1474) is where this first
  becomes real: a `"metal3"` route to a pin still on the base `"metal"` role
  (sky130 li1, gf180mcu Metal1, `sg13g2`/`sg13cmos5l` Metal1) is two via
  hops away. `gen_compose` walks the resolved deck's `metals`/`vias` stack
  one level at a time and draws the *full* ladder — a via plus a landing pad
  at every intermediate level, all at the pin's own position, exactly as the
  single-hop case already does per level (see "Multi-level via-drop" below)
  — rather than rejecting it. The only way this still fails is a genuine gap
  in the resolved deck itself: some hop along the way has no via declared in
  `ExtractionDeck.vias` (not the case for any two `routing.layer_role`
  values this table currently exposes on sky130, gf180mcu, `sg13g2`, or
  `sg13cmos5l` — each family's `"metal"`→`"metal2"`→`"metal3"` prefix is a
  fully-connected run of declared vias). gf180mcu's Metal4-5 levels, and
  `sg13g2`'s/`sg13cmos5l`'s own Metal4 and above (Metal4 through TopMetal2
  on `sg13g2`, Metal4/TopMetal1 on `sg13cmos5l`), remain unexposed as
  `routing.layer_role` roles at all (each curated `EXTRACTION_DECK` declares
  the full metals stack for extraction, but `klayout_tools.gen
  ._PDK_ROLE_LAYERS` stops at `"metal3"` for every family), so a route can
  never be asked to reach them via `routing.layer_role`/via-drop in the
  first place.
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

### Multi-level via-drop (#1567)

Before issue #1567, `gen_compose`'s via-drop only ever resolved **one** via
hop: a route on `routing.layer_role: "metal3"` whose `connectivity[]` pin sat
on a `"metal"`-role pad (two via hops away in sky130's/gf180mcu's/
`sg13g2`'s/`sg13cmos5l`'s own `metals`/`vias` stack) was rejected outright —
correctly, rather than drawing a disconnected stub, but the only way to
actually reach that pin was to hand-build one extra composition stage per
intermediate metal level, each declaring two ports at the identical
coordinate on adjacent layers and routing a zero-length net between them
just to make `gen_compose` emit that level's via.

Since #1567, `_resolve_via_drop_layer` instead walks the resolved deck's
`metals`/`vias` stack one level at a time between `routing.layer_role` and
the pin's own reported layer, and draws the **full ladder** — one via square
per hop, plus a landing pad on every level in between (including each
intermediate level, not just the two ends) — all centered at the pin's own
position, exactly as the single-hop case already draws its one via and two
landing pads. A `"metal3"` route reaching a base-`"metal"` pin two hops down
now draws two vias (sky130: `"via1"` then `"via2"`) and three landing pads
(one each on `"metal3"`, `"metal2"`, and `"metal"`), instead of failing:

```json
{
  "pdk": { "variant": "sky130A" },
  "blocks": [{ "id": "cell", "cell": { "gds_path": "cell.gds", "cell_name": "CELL", "bbox_um": {"width": 4.0, "height": 4.0}, "ports": [
    { "name": "IN", "x_um": 0.5, "y_um": 2.0, "direction_deg": 180, "layer": {"layer": 67, "datatype": 20} },
    { "name": "OUT", "x_um": 3.5, "y_um": 2.0, "direction_deg": 0, "layer": {"layer": 69, "datatype": 20} }
  ] } }],
  "placement": { "strategy": "row", "order": ["cell"], "spacing_um": 1.0 },
  "connectivity": [
    {
      "net": "SIG",
      "pins": [
        { "block": "cell", "port": "IN" },
        { "block": "cell", "port": "OUT" }
      ]
    }
  ],
  "routing": { "layer_role": "metal3", "width_um": 0.17 },
  "options": { "cell_name": "ladder_demo", "output": "ladder_demo.gds" }
}
```

`IN` sits on li1 (`67/20`, the base `"metal"` role) and `OUT` sits on met2
(`69/20`, `"metal3"`) — before #1567 this request's `SIG` net came back in
`unrouted_nets[]` with the "more than one via hop"/"single-hop drop"
rejection; since #1567 it routes, with `IN`'s end of the backbone dropping
through the full li1→met1→met2 via ladder to reach it. The composed output
is DRC-clean against `klt drc --deck sky130`, and `klt extract --deck
sky130` recovers `SIG` as a single node spanning both pins — no
hand-built intermediate composition stage required.

## Cross-block bus routing (`routing.cross_block_layer_role`, #1168)

`routing.layer_role` resolves to exactly **one** `(layer, datatype)` pair for
the *whole* composition — selecting `"metal2"` (the "Via-drop routing"
section above) moves every `connectivity[]` net in the request to the second
metal, not just the one net that actually needed to escape a same-layer
short. A real circuit's bus/supply nets are exactly the ones most likely to
cross an intermediate block's own pin on the way to a farther pin (a
same-block self-net leg bussing two of a matched array's terminals together
across a third terminal sitting between them — the reproduction "Self-net
pad-crossing"/"drawn-metal" checks above reject), while most of that same
composition's other nets never need anything but the base `"metal"` role.
Before #1168, fixing the busy nets meant moving the *entire* composition to
`"metal2"`, or hand-supplying `waypoints_um` to route around the crossed pad
on the same layer (not always geometrically possible).

`routing.cross_block_layer_role` names a **second**, higher metal role —
resolved through the same per-PDK-family table `routing.layer_role` is
(`gen_compose._resolve_cross_block_route_layer`), and required to be
connectable to `routing.layer_role` by some via-drop ladder in the resolved
PDK family's own `ExtractionDeck.metals`/`.vias` stack (an application
error, exit 1, otherwise — since issue #1567 this is the same multi-hop
resolution "Multi-level via-drop" documents, not a single-hop-only
restriction). It changes nothing by itself: only a same-block self-net leg
whose backbone
would draw a silent short on `routing.layer_role` (checks 3/4 above) is
retried on `routing.cross_block_layer_role` instead of failing outright, for
that leg's **entire** length (not just the segment crossing the pad) —
`route_two_pin()`'s same-drawing-layer short checks run again, unmodified, on
the second layer, so a *different* obstacle that happens to also sit on the
cross layer is still caught, not silently ignored. Every endpoint whose own
reported pad sits on the primary `routing.layer_role` then needs exactly the
via-drop "Via-drop routing" above already knows how to draw — resolved by the
identical `_resolve_via_drop_layer()` call, just against the leg's own
effective drawing layer. Every other net in the same request — and any leg
that never crosses another same-layer pad in the first place — is completely
unaffected and keeps drawing on `routing.layer_role`, exactly as before this
issue.

A leg that falls back here draws at `routing.cross_block_width_um` (issue
#1620), not `routing.width_um` — the two width knobs are independent, so
naming `routing.cross_block_layer_role` never forces `routing.width_um` up
to satisfy the cross layer's own (possibly stricter) deck minimum, and it
never widens a net that stays on `routing.layer_role`. Omit
`routing.cross_block_width_um` to get the cross layer's own deck minimum
automatically.

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
  "routing": {
    "layer_role": "metal",
    "width_um": 0.17,
    "cross_block_layer_role": "metal2"
  },
  "options": { "cell_name": "bjt_bus_cross_block", "output": "bjt_bus_cross_block.gds" }
}
```

Against the same 8-unit `bjt_array` reproduction "Via-drop routing" uses,
both `EBUS1`/`EBUS2` route here too — but `routing.layer_role` stays
`"metal"` for the request as a whole, so a third net wired between two
*different* blocks in the same composition (an ordinary block-to-block net
that never crosses another pad) still draws on li1, not met1. The composed
output is DRC-clean against `klt drc --deck sky130`, and `klt extract --deck
sky130` merges exactly the three targeted emitters into one node, same as
the whole-composition `"metal2"` case.

Two errors are raised before any routing is attempted, both at request-parse
time (exit 1, matching every other `routing.*` validation error):

- `routing.cross_block_layer_role` resolving to the **same** layer as
  `routing.layer_role` — a cross-block bus layer must be a distinct metal.
- `routing.cross_block_layer_role` and `routing.layer_role` not connectable
  by any via-drop ladder — the same "outside the metals stack entirely" and
  no-declared-via-for-some-hop cases "Via-drop routing"/"Multi-level
  via-drop" document for a pin's own layer, applied here to the two layer
  roles themselves.

### More than one same-block self-net per block (#1393)

`routing.cross_block_layer_role`'s retry above only ever fires from the two
checks it names ("Self-net pad-crossing" and "Self-net drawn-metal" above) —
a leg that falls back to the cross layer because it crosses another of the
block's own *pads* on `routing.layer_role`. That fallback has no visibility
into a route-vs-route collision (#1057/#1386 above) with a **different**
self-net on the **same** block that independently needed the cross layer
too. Until #1393, that made a block's *second* such self-net unroutable: two
independent gate-bussing nets on a `splits`-interleaved `diff_pair`, each
tying together its own device's two split legs, sit at the same pair of x
columns swapped between the two rows, so `manhattan_backbone()`'s fixed
one-jog shape draws the two diagonals of one rectangle — a real crossing.
Whichever net `connectivity[]` declared first claimed the lane and the other
was rejected outright (`"crosses already-routed net '<first net>'"`, or, since
#1386, the spacing-aware wording above); reordering the two entries just moved
the failure to the other net.

`route_two_pin()` now retries that leg the same *way* the bounded detour
search (#1167) already retries a leg blocked by a third, unrelated block —
propose a small, fixed set of alternate paths, re-run every routability check
against each drawn path, keep the first that passes — but with a lane shape
suited to this case. A same-block self-net's two pins sit *inside* the very
bbox that must be cleared, and a different self-net on the same block can own
the identical columns, so an over/under lane at either pin's own column
(#1167's shape) still runs through the other net's approach stub. Instead the
retry loops the leg fully **around the outside of the block's own bbox**: one
waypoint per bbox corner, each pushed clear of the block, shortest detour
first (deterministic, so the composed GDS stays byte-reproducible, #320).
Both gate nets in the example above now route, in either declaration order,
DRC-clean, extracting as two distinct nodes.

The retry is deliberately narrow — it is armed only for a leg that is all of:
a same-block self-net, on its own fixed-shape attempt (never a
caller-supplied `connectivity[].waypoints_um` path, and never a retry of a
retry — recursion is bounded at one level exactly as #1167's is), and one
that actually resolved onto `routing.cross_block_layer_role`. Every other
leg — an inter-block net, a same-block self-net that never needed the
fallback, a caller-routed path — reaches the route-vs-route collision check
exactly as it did before, and that check remains the sole decision-maker
there. Nothing is waived to make a lane fit: each candidate lane goes back
through the *whole* of `route_two_pin()` (ring checks, pad crossings,
drawn-metal shorts, obstacle overlap, via drops) plus the route-vs-route
collision check again, and a leg whose every lane still conflicts is still
reported `routed: false`, with the original collision wording plus a note
that the detour was tried.

One consequence worth knowing: a retried lane is **not** pinned to
`routing.cross_block_layer_role`. Because it loops clear of the block, it no
longer crosses the pads that forced the fallback, so the same checks
naturally resolve it back onto the primary `routing.layer_role` — which is
the intended outcome, leaving the cross layer to the first self-net that
still needs it. Read `nets[].legs[]`'s drawn geometry (or the composed GDS)
rather than assuming which layer a given leg landed on.

`connectivity[].waypoints_um` (#634) remains available for a caller who wants
to pick the path themselves — the retry only fires for a leg that supplied
none. As with every other check in this document, the composed output must
still be re-verified with `klt drc`.

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
specific to composition," section 2). Blocks referenced by
`blocks[].generator_report`/`blocks[].cell` need not all share the same
`dbu` (design-rule grid resolution) — see "Reconciling mismatched `dbu`s"
below — but every dbu disagreement must be an exact integer ratio, or
composing raises `GenComposeError` (exit 1).

Every block resolved against the **same PDK** agrees by construction: both
[`klt gen`](gen.md) and [`klt place-and-route`](place-and-route.md) derive
their output dbu from that PDK's own tech LEF `DATABASE MICRONS` declaration
(`0.001` for sky130, `0.0005` for gf180mcu — see
[`docs/cli/gen.md`](gen.md)'s "Output database unit (dbu)"). So a `klt gen`
guard ring and a `klt place-and-route` macro built for the same PDK compose
cleanly, including on a PDK whose tech LEF declares something other than
`DATABASE MICRONS 1000` (issue #1496 — before that fix `klt gen` always wrote
`0.001`, and such a mix was refused outright).

### Reconciling mismatched `dbu`s (issue #1514)

`klt draw` has no PDK awareness by design (issue #230) and always writes
`0.001`, regardless of `--pdk`; a GDS produced by a `klt gen` build predating
#1512 does too. Composing either of those against a freshly generated
gf180mcu-family `klt gen` block (`0.0005`) is not a composition mistake — it
is the exact same design, just described at two different, evenly-related
grid resolutions.

The composed cell is written at the **finest** (smallest-valued) `dbu` among
all blocks, echoed as the response's own `dbu_um`. Every coarser block's
geometry — including any internal cell hierarchy and array pitch — is
losslessly rescaled onto that finer grid (an exact integer magnification:
`0.001` onto `0.0005` is `2x`, `0.001` onto `0.0002` would be `5x`, and so
on), the same rescale mechanics
[`klt place-and-route`](place-and-route.md)'s DEF/LEF-GDS merge already uses
for the analogous problem. Each rescaled block adds one entry to the
response's `warnings[]` naming the block, its own original `dbu`, and the
`dbu` it was rescaled onto.

A `dbu` mismatch that is **not** an exact integer ratio still raises
`GenComposeError` — that is a genuine composition mistake (e.g. two blocks
built against different, unrelated PDK families), not a reconcilable grid
difference, so it stays a hard error rather than being silently
(and lossily) rescaled.

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
| `blocks[].generator_report` | object \| string | The block's own [`klt gen`](gen.md) JSON response — either an inline object, or a path to a file holding one (mirrors `klt gen --params`'s own path-or-inline duality). **Exactly one** of `generator_report`/`cell` must be present per entry; declaring both, or neither, is an application error (exit 1). A relative path string resolves against **the request file's own directory** (not the process's current working directory), matching `klt lvs`'s request-relative path convention — an absolute path is unaffected. When `compose()` is called directly as a library (no request file at all), relative paths resolve against the process's current working directory instead. This command's only input about a block's geometry is its already-reported `bbox_um`/`ports[]`/`cell_name`/`gds_path` — never a second, private inspection of the GDS stream at request-parse time. [`klt draw`](draw.md)'s own JSON response is also accepted unmodified — its `generator: "draw"` field and `cell_name`/`gds_path`/`bbox_um` already satisfy this schema (a `draw` block has no `ports[]`, which defaults to `[]`). So is **this command's own response** (#1189): it reports `generator: "gen-compose"` and a composed-frame `ports[]`, so a composition nests into a further composition unmodified. `generator` is required — a report without it is an application error whose message points at `blocks[].cell` below, which is the supported way to place a cell no `klt` verb generated (rather than hand-forging a report with a fake `generator`). |
| `blocks[].cell` | object | **Alternative to `generator_report`** (#1189) — an **existing** cell in a GDS/OASIS stream this command did not generate: a PDK standard cell, a vendor macro, any pre-drawn library cell. Placed, oriented, wired, and instantiated exactly like a generated block. Exactly one of `generator_report`/`cell` per `blocks[]` entry. |
| `blocks[].cell.gds_path` | string | Required. The stream holding the cell. A relative path resolves against **the request file's own directory**, the same convention a `generator_report` path string uses. |
| `blocks[].cell.cell_name` | string | Required. The cell to place, by name. A stream with no such cell is an application error (exit 1) that lists the names the stream does contain. |
| `blocks[].cell.ports[]` | array\<object\> | Optional, defaults to `[]`. Named terminals in the **cell's own** coordinate frame, using exactly the shape [`klt gen`](gen.md) reports (`name`, and optional `x_um`/`y_um`, `width_um`, `direction_deg`, `layer: {layer, datatype}`, `net`) — this is what lets a library cell participate in `connectivity[]` and `pins[]`. Unlike a `generator_report`'s ports (which a `klt` verb produced), these are hand-declared and therefore validated: a duplicate `name`, an `x_um` without a `y_um`, a non-positive `width_um`, a non-orthogonal `direction_deg`, or a malformed `layer` is an application error (exit 1). A port may carry a `name` only — it is then placeable but neither routable nor labellable, exactly like an under-reported generated port. |
| `blocks[].cell.bbox_um` | object | Optional. The cell's own (pre-placement) `{x0, y0, x1, y1}` footprint. **When omitted it is read from the stream** — `kdb.Cell.dbbox()`, i.e. the same box [`klt cells`](cells.md) reports under its `{left, bottom, right, top}` field names, translated here so the caller never has to. Declare it explicitly when the placement footprint should differ from the drawn extent (e.g. a standard cell's row-abutment box), or when the cell draws no geometry at all (an empty cell has no readable bbox — that is an application error, exit 1, naming this field as the fix). |
| `blocks[].orientation` | string | Optional, default `"none"` (#1166). `"none"`, `"mirror_x"`, `"mirror_y"`, or `"rotate_180"` — this block's own mirror/rotation, applied about its own local origin *before* placement translates it. See "Block orientation (mirror/rotate, #1166)" below for the exact transform and the same-facing-port case it unblocks. An unrecognised value is an application error (exit 1). |
| `placement.strategy` | string | `"row"` (single horizontal row, left to right in `order`, spaced by `spacing_um`), `"explicit"` (#321 — each block placed at its own declared `origins_um[id]`), or `"array"` (#1053 — the one `blocks[]` entry named in `order` repeated on a `rows` x `cols` grid). Any other value (e.g. `"grid"`, reserved by the spike for a different, still-unimplemented feature) is an application error (exit 1). |
| `placement.order` | array\<string\> | Block `id`s in placement order. Every `id` in `blocks[]` must appear exactly once — a missing or extra/unknown `id` is an application error. **Under `strategy: "array"`, `blocks[]`/`order` must contain exactly one entry** — the single block repeated at every tile; more than one is an application error. Response `blocks[]` ordering follows `order` under every strategy. |
| `placement.spacing_um` | number | Fixed gap between adjacent blocks' bounding boxes. Must be `>= 0`. **Only read under `strategy: "row"`** — ignored (not an error) when present alongside `strategy: "explicit"` or `"array"`. |
| `placement.origins_um` | object | **Required when `strategy: "explicit"`**, otherwise not read. Maps every `placement.order` block `id` to its own `{"x": number, "y": number}` origin — that block's `offset_um`, applied exactly like a `"row"` offset (added directly to the block's own reported `bbox_um`; see "`blocks[]` entries" below). The key set must equal `order` exactly — a missing, extra, or unknown `id` is an application error (exit 1), as is a non-numeric `x`/`y`. |
| `placement.rows`/`placement.cols` | integer | **Required when `strategy: "array"`** (#1053), otherwise not read. The grid's row/column counts — each must be a positive integer (`>= 1`); a non-integer, zero, or negative value is an application error (exit 1). |
| `placement.row_pitch_um`/`placement.col_pitch_um` | number | **Required when `strategy: "array"`**, otherwise not read. The fixed spacing between adjacent tile origins along each axis — each must be `> 0` (a zero or negative pitch is an application error, exit 1, even for a degenerate `rows: 1` or `cols: 1` array, where the corresponding pitch is otherwise unused geometrically). |
| `placement.origin_um` | object | Optional, **`strategy: "array"` only** — the base (row 0, col 0) tile's own `{"x": number, "y": number}` origin, i.e. that block's `offset_um`. Defaults to `{"x": 0.0, "y": 0.0}` when omitted, mirroring `"row"` placement's own implicit first-block origin. A non-numeric `x`/`y` is an application error (exit 1). |
| `connectivity[]` | array\<object\> | One entry per net: a `net` label (caller-chosen, response traceability only) and `pins[]` (at least 2), each `{block, port}` addressing one named port from that block's own `generator_report.ports[]`. Every net's `pins[]` are always validated against the referenced blocks' own reported ports, whether or not `routing` is supplied (#1188 — see below). When `routing` is supplied, a **2-pin** net is routed point-to-point; a **>2-pin** (bundle) net is routed as a spanning tree of two-pin legs, nearest pair first (#1073) — see "Scope". Pin order is not a routing order. A `pins[].block`/`pins[].port` referencing a nonexistent block `id` or port name is an application error (exit 1). |
| `connectivity[].waypoints_um` | array\<array\<number\>\> | Optional, **2-pin nets only**. An ordered, non-empty list of `[x_um, y_um]` points (composed-frame coordinates) the backbone is forced through, between port `a`'s own stub and port `b`'s own stub — see "Routing same-facing port pairs with `waypoints_um`" below. A malformed entry (not an array, not length-2, a non-numeric coordinate) is an application error (exit 1), as is supplying it on a **>2-pin** net (#1073 — a bundle net's spanning tree has no single backbone for the path to belong to; use `legs[]` to steer individual legs by name instead), or supplying it together with `legs[]` on the same entry (#1529 — mutually exclusive). Omitting it changes nothing (today's fixed one-jog/corner shape). |
| `connectivity[].legs[]` | array\<object\> | Optional (#1529). An array of `{from_pin, to_pin, waypoints_um}` objects, each steering one leg of *this* net by name — `from_pin`/`to_pin` are `{block, port}` objects that must each match one of this same entry's own `pins[]`, and `waypoints_um` is the same optional `[x_um, y_um]` list format as the top-level field (omit it to force just that pin pair into the spanning tree, without steering its path). See "Hand-routing individual legs of a bundle net with `legs[]`" below. A `from_pin`/`to_pin` not present in this entry's `pins[]`, a leg naming the same pin as both endpoints, a non-object leg entry, or a malformed `waypoints_um` is an application error (exit 1), as is supplying `legs[]` together with the top-level `waypoints_um` on the same entry. Omitting it changes nothing (every pin routes via the automatic nearest-first spanning-tree search, as before #1529). |
| `pins[]` | array\<object\> | Optional. One entry per single-pin top-level net to label **without routing** (#210) — e.g. a device gate, a bias/supply pad. Omitting it entirely changes nothing. Each entry names **exactly one** port (unlike `connectivity[]`'s 2+ `pins`). See fields below. |
| `pins[].net` | string | Caller-chosen net name written as the `kdb.Text` label on the port, and echoed in the response. Required and non-empty. |
| `pins[].block` | string | A `blocks[].id`. Referencing an unknown `id` is an application error (exit 1). |
| `pins[].port` | string | A port name from that block's own `generator_report.ports[]`. An unknown port is an application error (exit 1). A `(block, port)` that also appears in any `connectivity[]` entry is rejected (exit 1) — the router already labels that shape. The label lands at the port's own composed-frame position on the label layer paired with the port's own drawn layer; a port on a layer with no label convention is not labelled (a `drc_hints.notes[]` partial-success note, not an error). |
| `routing` | object | Optional (#1188). **Absent or `{}`** with a non-empty `connectivity[]` is a **declare-only** request: every net's `pins[]` is still validated, but no metal is drawn — each net comes back in `nets[]` with `status: "unrouted"` and `reason: "routing not requested"`, and its label lands in `unrouted_nets[]` (partial-success exit code `3`; see "Response" below). Supplying **any** key of `routing` opts into routing instead, and both `routing.layer_role`/`routing.width_um` become required at that point (a `routing` object with only one of the two set is an application error, exit 1 — there is no unambiguous partial routing spec). A request with an empty `connectivity[]` ignores `routing` either way. |
| `routing.layer_role` | string | A layer *role* (e.g. `"metal"`) resolved through the **same** per-PDK-family role→layer table every [`klt gen`](gen.md) generator uses — never a raw `{layer, datatype}` pair. **Required** (and must name a role the resolved PDK family actually has a layer for) once any `routing` key is supplied with `connectivity[]` non-empty; omit `routing` entirely for a declare-only request instead (see above). `"metal2"` (#454) runs the backbone on the family's second routing-metal level instead, via-dropping back to each pin's own `"metal"`-role pad through the connecting `"via1"` role — see "Via-drop routing (metal2/via, #454)" below. |
| `routing.width_um` | number | Route wire width. **Required and must be `> 0`** once any `routing` key is supplied with `connectivity[]` non-empty; omit `routing` entirely for a declare-only request instead (see above). |
| `routing.cross_block_layer_role` | string | Optional (issue #1168). A *second* layer role, resolved the same way as `routing.layer_role`, that a same-block self-net leg falls back to when it would otherwise short across another of that block's own pads on `routing.layer_role` (the exact rejection "Bussing this net across the block would draw a silent short" names as the fix) — see "Cross-block bus routing (`routing.cross_block_layer_role`, #1168)" below. Must resolve to a distinct layer connectable to `routing.layer_role` by some via-drop ladder in the resolved PDK family's own metals/vias stack (an application error, exit 1, otherwise; issue #1567 lifted the earlier single-via-hop-only restriction here). Every other net in the same request is unaffected — this is a per-leg fallback, not a whole-composition layer switch like selecting `routing.layer_role: "metal2"` directly. Configuring it also arms the same-block multi-self-net detour retry (#1393, "More than one same-block self-net per block" below), which is what lets a *second* self-net on the same block route when its own fixed-shape backbone would collide with the first's — that retry can land the second leg back on the primary `routing.layer_role`, so read the drawn geometry rather than assuming a leg's layer. |
| `routing.cross_block_width_um` | number | Optional (issue #1620). The width a leg draws at once it actually falls back to `routing.cross_block_layer_role` — independent of `routing.width_um`, so naming a cross-block layer with a stricter deck minimum never forces the *primary* plane's own routing wider than requested. Defaults to `routing.cross_block_layer_role`'s own deck minimum-width rule when omitted; when given, must be `> 0` and floored the same way `routing.width_um` is against `routing.layer_role` (an application error, exit 1, naming this field, the offending value, and the resolved deck's own rule id and threshold). Ignored (and meaningless) without `routing.cross_block_layer_role` also configured. |
| `options.cell_name`/`options.output` | string | Same semantics as `klt gen`'s own `options` fields — see [`docs/cli/gen.md`](gen.md). `cell_name` defaults to `"gen_compose_0"`; `output` defaults to `"<cell_name>.gds"`. |

### Response

```json
{
  "schema_version": 1,
  "generator": "gen-compose",
  "cell_name": "ota_top_0",
  "gds_path": "ota_top_0.gds",
  "pdk": { "name": "sky130A", "variant": "sky130A", "version": "open_pdks 0fe599b" },
  "dbu_um": 0.001,
  "bbox_um": { "x0": -0.92, "y0": -0.92, "x1": 14.2, "y1": 2.16 },
  "ports": [
    {
      "name": "VBIAS",
      "net": "VBIAS",
      "layer": { "layer": 66, "datatype": 20, "name": null },
      "x_um": 11.3,
      "y_um": 1.04,
      "width_um": 0.15,
      "direction_deg": 90,
      "block": "tail",
      "port": "U0_G"
    }
  ],
  "blocks": [
    {
      "id": "diffpair",
      "source": "generator_report",
      "generator": "diff_pair",
      "cell_name": "diff_pair_0",
      "offset_um": { "x": 0.0, "y": 0.0 },
      "bbox_um": { "x0": -0.92, "y0": -0.92, "x1": 3.56, "y1": 2.16 },
      "orientation": "none"
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
      "route_length_um": 3.2,
      "status": "routed",
      "legs": [
        {
          "pins": [
            { "block": "diffpair", "port": "Q1_1_D" },
            { "block": "mirror", "port": "M1_1_D" }
          ],
          "routed": true,
          "route_length_um": 3.2,
          "reason": null
        }
      ]
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
| `generator` | string | Always `"gen-compose"` (#1189) — the marker that makes this whole response a valid `blocks[].generator_report` for another `klt gen-compose` run, mirroring [`klt draw`](draw.md)'s own `generator: "draw"`. It names the *producing verb*, not a `klt gen` generator. |
| `cell_name` | string | Name of the top cell written into `gds_path`, containing every placed block's cell as a translated sub-cell instance plus all routed metal. |
| `gds_path` | string | Resolved output path (echoes `options.output`, or the computed default). |
| `pdk` | object | The resolved PDK reference, echoing `klt pdk find`'s own `variant`/`version` fields. |
| `dbu_um` | number | Database unit (µm) the composed cell was written at — the **finest** dbu among all blocks' own streams (any coarser block is losslessly rescaled onto it, see "Reconciling mismatched `dbu`s" above; a non-integer-ratio mismatch is still an error), matching [`klt gen`](gen.md)'s field of the same name. Reported so this response can be nested as a `blocks[].generator_report` one level up without re-reading the stream. |
| `bbox_um` | object | Bounding box of the *composed* cell — the union of every placed block's own `bbox_um`, translated by its `offset_um` (computed arithmetically from each block's reported `bbox_um`, never re-derived from drawn geometry). |
| `ports[]` | array\<object\> | The composed cell's **own** named terminals (#1189), in the composed (post-placement) coordinate frame — one per request `pins[]` entry, in request order. Same entry shape as [`klt gen`](gen.md)'s `ports[]` (`name`, `net`, `layer`, `x_um`, `y_um`, `width_um`, `direction_deg`) plus `block`/`port` recording which sub-block port it was promoted from. `name` **and** `net` are the `pins[]` entry's own `net` string — the same name written as the port's `kdb.Text` label — so the composed cell's port name, its drawn label, and the name `klt extract` recovers all agree, and the level above addresses it as `connectivity[].pins[].port`. Always present; **empty when the request supplied no `pins[]`** (backward compatible). Only `pins[]` is promoted: auto-exposing every sub-block port would both flood the parent with internal terminals and collide names across blocks (two `mos_array` blocks both report `U0_D`). A `pins[]` port with no reported `{x_um, y_um, layer}` geometry has no composed-frame position and is skipped (the same `drc_hints.notes[]` entry the label path emits explains why); two `pins[]` entries sharing one `net` name both appear, with a note that only the first is addressable by name one level up. |
| `blocks[]` | array\<object\> | Per-block placement result — see below. |
| `nets[]` | array\<object\> | One entry per `connectivity[]` net: an echo of `net`/`pins`, plus `routed` (boolean — `true` only when *every* pin joined one component), `route_length_um` (summed wire length in um across the net's **drawn** legs, or `null` when zero legs were drawn — for a caller doing a first-order parasitic estimate before extraction), `status` (below), and `legs[]` (below). Present for every net including unroutable ones (with `routed: false`). Under a declare-only request (`routing` absent/`{}`, #1188), every net reports `routed: false`, `status: "unrouted"`, `route_length_um: null`, and every leg's `reason: "routing not requested"` — validated against the blocks' own ports, but never drawn. |
| `nets[].status` | string | One of `"routed"` (every pin connected into one component), `"partial"` (at least one leg drawn, but the net is not fully connected), or `"unrouted"` (no leg was ever accepted, including every net under a declare-only request, #1188) — issue #1169. Distinguishes a partially-drawn net from a fully-undrawn one: both report `routed: false`, so a caller must read `status` (not just count `legs[].routed`) to tell them apart. |
| `nets[].legs[]` | array\<object\> | The two-pin legs the net was routed as (#1073): `pins` (the leg's own two `{block, port}` entries), `routed`, `route_length_um`, and `reason` (`null` when routed, otherwise why this leg was not drawn — `"routing not requested"` for every leg under a declare-only request, #1188, as opposed to a geometry-based rejection reason). A 2-pin net has exactly one leg; an N-pin net has N−1 when fully routed (fewer when the same `{block, port}` is listed more than once — a repeated pin is the same physical point and needs no leg of its own). **`routed: true` means this leg's metal is in the output.** Since #1169, a net that could not be *fully* connected still draws every leg the spanning-tree search accepted — only the legs reaching a stranded pin (plus any candidate rejected on the way) report `routed: false`, each with its own `reason`. For a `status: "routed"` net, legs the router tried and rejected on the way to a working spanning tree are dropped from the list entirely; for a `status: "partial"`/`"unrouted"` net, every attempted leg (drawn or not) is kept, so the caller can see the full search, not just the winning subtree. A leg the caller named via `connectivity[].legs[]` (#1529) is reported in this exact same shape — no distinct field marks a leg as caller-steered versus auto-selected — with one behavioural difference: a **rejected named leg is kept even on a `status: "routed"` net** (an auto-selected candidate's rejection is a router detail; a named leg's is the caller's own path not being drawn, and must not be silent). Check `legs[].routed`, not just `nets[].routed`, when supplying `legs[]`. |
| `pins[]` | array\<object\> | One entry per request `pins[]` item (#210), in request order: `net`, `block`, `port` (all echoed) plus `labelled` (boolean — `true` when a label was placed, `false` when the port's layer has no label convention, matching a `drc_hints.notes[]` entry). Always present; **empty when the request supplied no `pins[]`** (backward compatible). |
| `unrouted_nets[]` | array\<string\> | Net labels the router could not *fully* connect — an unroutable 2-pin net, or a bundle net whose pins could not all be joined into one spanning tree (#1073), including a `status: "partial"` net that drew some but not all of its legs (#1169; see `nets[].status` to tell partial from fully-unrouted). Under a declare-only request (#1188), **every** `connectivity[]` net lands here (nothing was routed by request, not by failure — see `nets[].legs[].reason`). Always present, empty when everything routed. **A non-empty array is a partial success** (exit code `3`), not silently dropped connectivity. A listed net's *drawn* legs (if any — `nets[].legs[]`/`nets[].status` say which) are still real, DRC-checked metal; only the stranded pins are left for the caller to wire themselves. |
| `drc_hints` | object | Advisory, same "not authoritative" semantics as `klt gen`'s own `drc_hints` — `klt drc` remains the actual authority on rule compliance. See fields below. |
| `warnings[]` | array\<string\> | Non-fatal notes. Always present, empty when there is nothing to report. |

#### `drc_hints` fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `min_spacing_um` | number \| null | The tightest spacing actually used across placement and routing (the placement gap when any net was routed) — **`"row"` placement only**. `null` whenever nothing was actually routed — no `connectivity[]` was supplied, `connectivity[]` was supplied but declare-only (`routing` absent/`{}`, #1188), or every net in `connectivity[]` failed to route — since none of those exercises any routing/placement spacing as a clearance (#1198). Also always `null` under `"explicit"` (#321) or `"array"` (#1053) placement — neither has a single shared spacing value to report (`"explicit"`'s per-pair separation is exactly what a caller-declared origin expresses; `"array"` has two independent pitches, `row_pitch_um`/`col_pitch_um`, not one). |
| `matched_groups[]` | array\<object\> | One entry per distinct `matched_group_id` seen among the input blocks' own `generator_report.drc_hints.matched_group_id` (in first-seen order): `matched_group_id` (echoed), `blocks` (the request-level block `id`s carrying it), and `placement_symmetric` (always `null` this phase — symmetry *verification* against a declared symmetry axis is out of scope). Empty when no input block carries a `matched_group_id`. |
| `notes[]` | array\<string\> | Free-form composition notes — e.g. why a specific net was left unrouted (narrow channel, a route-vs-route collision with an already-routed net #1057, or, for a bundle net, which pins could not be reached plus the nearest per-leg rejection #1073). Always present, empty when there is nothing to report. |

#### `blocks[]` entries

| Field | Type | Description |
| ----- | ---- | ----------- |
| `id` | string | Echo of the request's `blocks[].id`. |
| `source` | string | Which request form sourced this block's geometry (#1189): `"generator_report"` (a `klt` verb's own response) or `"cell"` (an existing cell in a stream). |
| `generator` | string \| null | Echoed from that block's own `generator_report.generator`; **`null`** for a `source: "cell"` block (#1189), which no generator produced. |
| `cell_name` | string | The source cell's own name (#1189) — from `generator_report.cell_name` or `cell.cell_name`. This is the cell copied into the composed output (instantiated there under a `"<id>__<cell_name>"` sub-cell). |
| `offset_um` | object | `{x, y}` — the translation applied to place this block. Under `"row"`, the first block always has `offset_um: {x: 0.0, y: 0.0}`; every subsequent block is translated along `x` only (row placement never translates `y`) so its bbox sits exactly `placement.spacing_um` past the previous (already translated) block's right edge — regardless of that block's own `bbox_um.x0` (which need not be `0`; a guard-ringed block's bbox can extend to negative coordinates). Under `"explicit"` (#321), `offset_um` is exactly the request's own `placement.origins_um[id]`, verbatim — a block's own `bbox_um` plays no role in computing it (an explicit origin translates a block's bbox by that amount; it does not force the bbox's own `(x0, y0)` corner to land exactly on the declared origin unless that block's own `bbox_um.x0`/`y0` is already `0`). Under `"array"` (#1053), `offset_um` is exactly `placement.origin_um` (the base, row-0/col-0 tile) — every *other* tile's own position is implied by `rows`/`cols`/`row_pitch_um`/`col_pitch_um` rather than reported as a separate `blocks[]` entry (there is still exactly one `blocks[]` entry for an `"array"`-placed block, echoing this base tile). |
| `bbox_um` | object | That block's own `generator_report.bbox_um`, transformed by `orientation` (#1166, about the block's own local origin) then translated by `offset_um`, in the composed cell's coordinate frame — **except under `"array"`** (#1053), where `bbox_um` is instead the union bounding box of *every* placed tile (all `rows * cols` instances), matching the top-level `bbox_um` field above when this is the only block in the request. |
| `orientation` | string | Echo of the request's `blocks[].orientation` (#1166), `"none"` when omitted. |

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
"Engine" above). A `blocks[].cell` entry (#1189) is the one narrow, opt-in
exception: when — and only when — it declares no `bbox_um` of its own, that
one value is read from the stream's own cell. There is no report to consume
for a cell no `klt` verb generated, and requiring the caller to transcribe one
by hand is exactly the gap #1189 filed. A `generator_report` block's `bbox_um`
is still never read from its stream.

## Text format

The default `text` format prints a short summary. It is intended for human
eyes and its exact layout is **not** part of the contract — parse the JSON
instead.

```
$ klt gen-compose request.json
cell_name: ota_top_0
gds_path: ota_top_0.gds
pdk: sky130A (open_pdks 0fe599b)
dbu_um: 0.001
bbox_um: (-0.92, -0.92) - (14.2, 2.16)

blocks:
  diffpair (diff_pair)  offset=(0.0, 0.0)  bbox=(-0.92, -0.92) - (3.56, 2.16)
  mirror (diff_pair)  offset=(5.48, 0.0)  bbox=(4.56, -0.92) - (9.04, 2.16)
  tail (mos_array)  offset=(11.56, 0.0)  bbox=(10.04, 0.0) - (14.2, 0.42)

nets:
  VOUT  routed  length=3.2um
  VDD  routed  length=8.76um  (2 legs)

matched_groups:
  diff_pair:pair:2  (diffpair)
```

## Exit codes and errors

| Exit code | Meaning |
| --------- | ------- |
| `0` | Every block placed and every net routed; `gds_path` was written and the report above is on stdout. |
| `1` | Application error — unresolvable PDK, an unrecognised `pdk` key (anything other than `variant`/`root`), malformed request (missing/invalid `blocks[]`, an unsupported `blocks[].orientation` value (#1166), `placement.order` not matching `blocks[]`, negative `spacing_um`, a missing/mismatched/non-numeric `placement.origins_um` when `strategy: "explicit"` (#321), more than one `blocks[]` entry or a missing/non-positive `rows`/`cols`/`row_pitch_um`/`col_pitch_um`/non-numeric `origin_um` when `strategy: "array"` (#1053), or a missing/invalid `routing.layer_role`/`routing.width_um` when `connectivity[]` is non-empty), an unsupported `placement.strategy`, a `connectivity[]` or `pins[]` entry referencing a nonexistent block `id`/port, a `pins[]` entry naming a `(block, port)` already used by a `connectivity[]` net, a block's `generator_report`/GDS could not be read, or the `options.output` directory does not exist. |
| `2` | Usage error — missing `<request.json>` argument, or a bad `--format` value (from argparse). |
| `3` | **Partial success** — every block placed, but `unrouted_nets[]` is non-empty (a net could not be routed). The full success payload above is still on stdout, mirroring `klt drc`'s own `3` for "ran clean but found violations" (spike section 2, "Proposed exit codes"). |

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

One thing `"explicit"` deliberately does not add (see "Scope" above):
`gen-compose` performs no overlap check of its own — a caller
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

Two things `"array"` deliberately does not do (see "Scope" above):
- **Only one block.** `blocks[]`/`placement.order` must have exactly one
  entry — a caller composing several distinct blocks alongside a repeated
  array needs a separate `gen-compose` request per block (or a follow-on
  request that reads this one's own `gds_path` as an input, once that
  composition-of-compositions capability exists).
- **No per-tile orientation override.** The one `blocks[]` entry's own
  `orientation` (#1166) applies uniformly to every tile — there is no
  per-tile mirroring (e.g. alternating flipped rows/columns, a common
  "stitched" layout style, is not expressible this way).
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

## Block orientation (mirror/rotate, #1166)

Every `blocks[]` entry accepts an optional `orientation` field
(`"none"` (default), `"mirror_x"`, `"mirror_y"`, or `"rotate_180"`), applied
about that block's own local origin *before* placement translates it —
orthogonal to (composes with) every `placement.strategy` above, since it is
a per-block attribute, not a placement-strategy one.

**Why this exists**: placement alone (translation only, no rotation) cannot
make two same-facing ports face each other. A minimal two-device "CMOS
inverter" (one `mos_array` standing in for an nfet, one for a pfet, side by
side) is the canonical case: `mos_array` always reports its source on the
left edge (`180deg`) and its drain on the right edge (`0deg`) of its own
local frame (see [`gen.md`](gen.md)'s `mos_array` section). Two such blocks
placed in a row therefore have their drains on the *same* absolute side —
the second block's drain faces away from the first, on its own far edge —
so wiring them together (`connectivity[]`'s shared `VOUT` net) forces the
backbone to cross straight through the second block's own interior, which
`route_two_pin`'s obstacle-overlap check (check 5) rejects outright. This was
root cause #1 of #1164's friction report: "if [a CMOS inverter] cannot
route, nothing larger can."

`orientation: "mirror_x"` on the second block fixes exactly this: it moves
that block's own drain from its right edge to its left edge (and flips its
reported `direction_deg` from `0` to `180`), so the two drains now sit
directly across the row's `spacing_um` channel, facing each other — the net
routes as a plain straight backbone, no waypoints needed.

```json
{
  "blocks": [
    { "id": "n", "generator_report": "nfet.json" },
    { "id": "p", "generator_report": "pfet.json", "orientation": "mirror_x" }
  ],
  "placement": { "strategy": "row", "order": ["n", "p"], "spacing_um": 1.0 },
  "connectivity": [
    { "net": "VOUT", "pins": [{ "block": "n", "port": "U0_D" }, { "block": "p", "port": "U0_D" }] }
  ],
  "routing": { "layer_role": "metal", "width_um": 0.17 },
  "options": { "cell_name": "inverter_0", "output": "inverter_0.gds" }
}
```

**Transform semantics** (about the block's own local `(0, 0)`, matching
`klayout.db.Trans`'s own mirror-then-rotate-then-translate composition —
verified 1:1 against it in the test suite):

| `orientation` | Point transform `(x, y) ->` | Direction remap (`direction_deg`) |
| --- | --- | --- |
| `"none"` (default) | `(x, y)` — unchanged | unchanged |
| `"mirror_x"` | `(-x, y)` — horizontal flip (left-right) | `0 <-> 180`, `90`/`270` unchanged |
| `"mirror_y"` | `(x, -y)` — vertical flip (top-bottom) | `90 <-> 270`, `0`/`180` unchanged |
| `"rotate_180"` | `(-x, -y)` | `0 <-> 180`, `90 <-> 270` |

Applied consistently everywhere a block's own geometry is consumed, so a
block's reported metadata never disagrees with what is actually drawn:

- **`bbox_um`** (both the response's per-block `bbox_um` and every placement
  strategy's own internal bbox math) — mirrored/rotated, then translated by
  `offset_um`. Width/height are always preserved (every orientation is an
  axis-aligned flip, never a diagonal rotation).
- **`ports[]`** — each port's `x_um`/`y_um` and `direction_deg` are
  transformed identically, so `connectivity[]`/`pins[]` routing, ring-opening
  detection, and stub-widen all see an already-correct, already-oriented
  port without any of that logic special-casing orientation itself.
- **Drawn GDS geometry** — the actual sub-cell instance
  `_write_composed_gds` inserts (via `kdb.Trans(rot, mirrx, x, y)`) and the
  obstacle geometry `read_block_layer_geometry` reads back for the self-net
  drawn-metal check (#453/#469) both apply the identical transform, so a
  self-net's pad-crossing checks stay correct for a mirrored block too.

**What orientation does not do**: it never changes a port's *identity* (its
name, `width_um`/contact size, or which layer it is on) — only its position
and facing direction. It composes with `"explicit"` placement's own
`offset_um` (mirror first, about the block's local origin, then translate by
the declared origin — never the other way around) and with `"array"`
placement (the one array-placed block's orientation applies uniformly to
every tile, matching `kdb.CellInstArray`'s own semantics — its `a`/`b` step
vectors are added in the *parent* frame, after the instance's own
rotation/mirror). An unrecognised `orientation` value is an application
error (exit 1).

## Hierarchical composition and library cells (#1189)

Before #1189, `blocks[].generator_report` was the only way to name a block's
geometry, and it required a `generator` field — so the only cells this command
could consume were the ones `klt gen` (or `klt draw`, #1059) had produced.
Two cells a caller plainly wants to place fell outside that: **this command's
own output** (no `generator`, no `ports[]`, so composition could not nest) and
**a cell out of the PDK's own library** (no report at all). Both were reachable
only by hand-forging a report object with a fake `generator` — and, for a
library cell, by re-keying `klt cells`' `{left, bottom, right, top}` bbox into
this command's `{x0, y0, x1, y1}` — with nowhere to put ports, so nothing about
such a block could participate in `connectivity[]`/`pins[]`.

### Nesting a composition into a further composition

A `klt gen-compose` response now reports `generator: "gen-compose"` and a
`ports[]` promoted from its own `pins[]`, in the composed coordinate frame. So
the response feeds straight back in, unmodified:

```bash
# Level 1: compose two devices and promote the outer terminals to pins.
klt gen-compose stage.json --format json > stage.json.out
```

```json
{
  "blocks": [
    { "id": "r1", "generator_report": "r1.json" },
    { "id": "r2", "generator_report": "r2.json" }
  ],
  "placement": { "strategy": "row", "order": ["r1", "r2"], "spacing_um": 2.0 },
  "connectivity": [
    { "net": "MID", "pins": [{ "block": "r1", "port": "P2" },
                             { "block": "r2", "port": "P1" }] }
  ],
  "routing": { "layer_role": "metal", "width_um": 0.17 },
  "pins": [
    { "net": "IN", "block": "r1", "port": "P1" },
    { "net": "OUT", "block": "r2", "port": "P2" }
  ],
  "options": { "cell_name": "stage_0", "output": "stage_0.gds" }
}
```

`stage.json.out` now carries `ports[]` entries named `IN` and `OUT`. Level 2
places that whole composed cell as one block and wires `OUT` onward — the
promoted name is what `connectivity[].pins[].port` addresses:

```json
{
  "blocks": [
    { "id": "stage", "generator_report": "stage.json.out" },
    { "id": "load", "generator_report": "load.json" }
  ],
  "placement": { "strategy": "row", "order": ["stage", "load"], "spacing_um": 2.0 },
  "connectivity": [
    { "net": "CHAIN", "pins": [{ "block": "stage", "port": "OUT" },
                               { "block": "load", "port": "P1" }] }
  ],
  "routing": { "layer_role": "metal", "width_um": 0.17 },
  "options": { "cell_name": "chain_0", "output": "chain_0.gds" }
}
```

The nested cell is placed **whole** — its own `bbox_um` (union of its
sub-blocks plus routing) is what the parent's placement math uses, and its
hierarchy is preserved in the output (the parent's top cell instantiates
`stage__stage_0`, which still contains the child's own `r1__…`/`r2__…`
sub-cells). Promotion is transitive: the parent may name a promoted port in
its *own* `pins[]` to re-promote it a level further up.

Only `pins[]` entries are promoted. That is deliberate: `pins[]` already means
"this port is a top-level pin of the composed cell", so it is the caller's own
declaration of the module's interface. Auto-promoting every sub-block port
would flood the parent with internal terminals and collide names across blocks
(two `mos_array` blocks both report `U0_D`).

### Placing a cell out of a library

`blocks[].cell` names a cell that already exists in a stream. Nothing about it
came from a `klt` verb, so nothing is echoed from a report: the ports are
declared by the caller (in the cell's own frame, using `klt gen`'s port shape),
and the bounding box is read from the cell itself unless declared.

```json
{
  "blocks": [
    {
      "id": "u1",
      "cell": {
        "gds_path": "/pdk/sky130A/libs.ref/sky130_fd_sc_hd/gds/sky130_fd_sc_hd.gds",
        "cell_name": "sky130_fd_sc_hd__inv_2",
        "ports": [
          { "name": "A", "layer": { "layer": 67, "datatype": 20 },
            "x_um": 0.0, "y_um": 1.32, "width_um": 0.2, "direction_deg": 180 },
          { "name": "Y", "layer": { "layer": 67, "datatype": 20 },
            "x_um": 1.38, "y_um": 1.32, "width_um": 0.2, "direction_deg": 0 }
        ]
      }
    },
    { "id": "u2", "cell": { "gds_path": "…", "cell_name": "sky130_fd_sc_hd__buf_2",
                            "ports": [ … ] } }
  ],
  "placement": { "strategy": "row", "order": ["u1", "u2"], "spacing_um": 0.0 },
  "connectivity": [
    { "net": "N1", "pins": [{ "block": "u1", "port": "Y" },
                            { "block": "u2", "port": "A" }] }
  ],
  "routing": { "layer_role": "metal", "width_um": 0.17 },
  "options": { "cell_name": "cellrow_0", "output": "cellrow_0.gds" }
}
```

A `cell` block is otherwise an ordinary block: it takes `blocks[].orientation`
(#1166), works under every `placement.strategy` including `"array"` (#1053),
routes through `connectivity[]`, labels through `pins[]`, and can be mixed
freely with `generator_report` blocks in one request. Its response entry
reports `source: "cell"`, `generator: null`, and the `cell_name` placed.

**Related limitation, not fixed here.** `placement.strategy: "explicit"` still
supports no per-block rotation beyond `blocks[].orientation`'s four
mirror/rotate values, and there is no *per-tile* orientation override under
`"array"` (#1166). Standard-cell rows are conventionally built by mirroring
alternate rows so adjacent rows share a power rail; expressing that today
means one `blocks[]` entry per row, each with its own `orientation`, rather
than one array. That is a placement question, tracked separately.

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
# node (decomposed into two 2-pin nets sharing the tail.U0_D endpoint -- the
# only way to express it before #1073; see the note under this request for
# the single 3-pin form that now routes the same node); N1/VOUT tie each input
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

**The tail node as one bundle net (#1073).** The `TAIL_A`/`TAIL_B` pair above
is one three-way node written as two 2-pin nets sharing the `tail.U0_D`
endpoint, because that was the only shape a two-pin-only router accepted.
Since #1073 the same node can be declared directly, and the router builds the
spanning tree itself:

```json
{ "net": "TAIL", "pins": [
  { "block": "tail", "port": "U0_D" },
  { "block": "diffpair", "port": "Q1_1_S" },
  { "block": "diffpair", "port": "Q2_1_S" }
] }
```

Verified on this exact block set: exit `0`, `unrouted_nets: []`, the net
routed as two legs (`diffpair.Q1_1_S`–`diffpair.Q2_1_S`, then
`tail.U0_D`–`diffpair.Q1_1_S`) totalling 4.52um, `klt drc` clean, and
`klt extract` reporting a single `TAIL` pin instead of the `TAIL_A|TAIL_B`
two-label alias the decomposed form produces. The example above is left in
its two-net form because the LVS reference netlist below matches against that
node naming.

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
