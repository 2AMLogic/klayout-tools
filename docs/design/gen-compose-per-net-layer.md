# Decision: per-net routing planes for `klt gen-compose`

**Status:** decision record, implemented by the same PR that adds this file
(issue #1655). It settles one JSON-contract question — *how does a caller ask
`gen-compose` to route different nets on different metal layers in a single
call?* — and follows the decision-record pattern the accepted spikes in this
directory use ([`gen-composition-spike.md`](gen-composition-spike.md) is the
parent spike this command was built from;
[`digital-fleet-unit-abstraction-decision.md`](digital-fleet-unit-abstraction-decision.md)
is the shape of a *decision*-only record). Nothing here adds a dependency, a
new `klt` verb, or a `schema_version` bump.

## Why this exists

`routing.layer_role` resolves **one** `(layer, datatype)` pair for the whole
composition (`gen_compose.compose()` → `_resolve_route_layer()`, one call per
request). `routing.cross_block_layer_role` (#1168) adds a second plane, but as
a *fallback the router reaches for on its own* — a same-block self-net leg
that would otherwise short across another of that block's pads is retried
there (`route_two_pin`'s checks 3/4 retry; `route_bundle`'s #1680
route-vs-route retry). Neither is a caller-directed, per-net choice.

That is a structural limit, not an ergonomic one. Model the composition the way
the router actually draws it: terminals on a line (the placement row), every
backbone routed in the half-planes above/below it. A set of nets is drawable on
one metal plane only if its connectivity graph has an embedding with no
crossings; a graph containing a K3,3 subdivision has none, in any floorplan,
on any track assignment. Concretely, the six-block `mos_array` K3,3 fixture
added with this decision (`tests/test_gen_compose.py`,
`test_compose_k33_non_planar_graph_is_unroutable_on_one_layer`) composes with
two of its nine nets in `unrouted_nets[]`, each rejected with `crosses
already-routed net '…'` — the route-vs-route check (#1057) doing exactly its
job: on one plane those nets *are* shorts. Non-planar net graphs are not exotic
(#1467 is a real 13-net block that routes 1 net for a related reason), so "one
`gen-compose` call cannot draw this cell at all" is a real capability gap.

## Options considered

The issue offered two, and they are not two spellings of one change — they
imply different request contracts, which is why this record exists before the
implementation rather than after it.

**Option 1 — an optional per-net (and per-leg) `layer_role` override in
`connectivity[]`.** The caller names the plane a given net draws on; the router
resolves it through the *same* `_resolve_route_layer()` path
`routing.layer_role` already goes through, and legs of one net that span two
planes are stitched by the existing #454 via-drop mechanics.

**Option 2 — a first-class N-pass request shape** (an array of `routing`
blocks over one shared `blocks`/`placement`), with `gen-compose` merging each
pass's own top-cell shapes internally. This is the caller's current
workaround — run `gen-compose` N times at identical origins and union the
outputs with `klayout.db` — promoted to a builtin.

## Decision: option 1

Two additive, optional fields, resolved per request and defaulting to exactly
today's behaviour when absent:

```jsonc
"connectivity": [
  {
    "net": "CLK",
    "layer_role": "metal2",     // this net's own plane (optional)
    "width_um": 0.2,            // its width on that plane (optional)
    "pins": [ /* … */ ],
    "legs": [
      { "from_pin": {...}, "to_pin": {...},
        "layer_role": "metal3", // one leg's own plane (optional)
        "width_um": 0.2 }       // and its width (optional)
    ]
  }
]
```

Resolution rules, in one place so the contract is unambiguous:

- **A role resolves exactly like `routing.layer_role` does** — same
  `_PDK_ROLE_LAYERS` table, same "unknown role"/"no layer in this family's
  deck" errors, re-labelled to name the caller's own field (the same
  re-labelling `_resolve_cross_block_route_layer()` already does).
- **Width is inherited, then floored.** An entry with no `width_um` draws at
  the width it would have drawn at anyway (`routing.width_um` for a net, the
  net's effective width for a leg), validated against the *named* plane's own
  minimum-width rule from the same curated deck `klt drc` judges the output
  with. An inherited width that does not clear a stricter plane's floor is an
  application error naming that entry's own `width_um` as the fix — never a
  silent widening of every other net's routing, which is the regression #1620
  fixed for `cross_block_width_um`.
- **Explicit wins over the automatic fallback.** A net that names its own
  `layer_role` is not retried onto `routing.cross_block_layer_role`; a net that
  names none keeps that fallback unchanged, exactly as before. The reason is
  predictability: the caller took control of this net's plane, so silently
  moving it to a third one would make the drawn layer unpredictable — and is
  ill-defined when the two roles name one metal. A net that genuinely needs two
  planes says so with a per-leg override instead. `legs[].layer_role` follows
  the same rule for the one leg it names.
- **No `schema_version` bump.** Both fields are optional and additive; a
  request that omits them is byte-identical on disk to what it produced before
  (asserted directly, as raw GDS bytes, by
  `test_compose_per_net_layer_role_naming_the_primary_plane_is_byte_identical`).
  The keys are added to `_CONNECTIVITY_ENTRY_KEYS`/`_LEG_ENTRY_KEYS`, so the
  #1548 "unknown key is an error, not a silent no-op" guard still holds for
  everything else.

### Cross-plane stitching needs no new geometry

This is the question the curated acceptance criteria asked to be answered
explicitly. Every leg already drops from its own drawing layer down to **each
endpoint pin's own reported layer**, at that pin, through the via ladder
`_resolve_via_drop_layer()` resolves from the family's own
`ExtractionDeck.metals`/`.vias` stack (#454, generalised to multi-hop by
#1567). So two legs of one net that ran on two different planes both terminate
on the *same pin's own pad* and merge there — the stitch is the via drop that
already existed, not a new construct. When no ladder exists between a plane
and a pin's own layer, that leg is rejected with the existing reason string;
it is never drawn as a disconnected stub (the #492 guarantee). The
implementation's own test asserts this electrically rather than
geometrically: `klt extract` on the mixed-plane bundle reports all three
device terminals on one net.

### Why option 2 is rejected

- **It duplicates `blocks`/`placement` per pass or invents a sharing rule for
  them.** The one thing every pass must agree on is precisely the thing a
  `routing[]` array does not express; nothing in the shape prevents two passes
  from disagreeing, so the request gains an invariant only prose can state.
- **It makes the response's provenance multi-valued.** `nets[]`,
  `unrouted_nets[]`, `drc_hints`, `warnings`, `dbu_um`, and `bbox_um` are all
  singular today. Under N passes each becomes "which pass?" — either N nested
  responses (a breaking reshape) or a merged one whose fields no longer map to
  a single routing configuration.
- **It still leaves the planar split to the caller.** The caller must decide
  which nets go in which pass — the actual hard part — and gets *less* help
  doing it, because no pass can see another pass's geometry. Option 1 keeps
  every net in one `route_bundle()`/`leg_conflict` world, where the
  route-vs-route check already compares layer-aware footprints (#1386) and
  correctly skips genuinely different planes.
- **It is strictly weaker for the motivating case.** A single net whose legs
  span two planes cannot be expressed at all: each pass sees the whole net or
  none of it.

## Interactions

- **`routing.cross_block_layer_role` (#1168) / `cross_block_width_um`
  (#1620)** — unchanged for every net that names no plane of its own; skipped
  for a net (or leg) that does. Covered by
  `test_compose_per_net_layer_role_wins_over_cross_block_layer_role`, where
  one net still falls back to met1 through #1168's own retry while another
  draws on met2 because it asked to.
- **Track assignment (#1467)** — orthogonal, and the two compose. This
  decision allocates *planes* between nets; #1467 is about allocating *tracks*
  (y offsets) within one plane, which is what would let several non-crossing
  nets share a channel without the first one rejecting the rest. Neither
  subsumes the other: tracks cannot make a non-planar graph drawable on one
  plane, and planes do not stop two same-plane nets from wanting the same
  channel. A later track assigner would run *within* whatever plane a net
  resolved to here.
- **Obstacle-overlap layer-blindness (#1656, fixed)** — with more nets
  legitimately crossing *over* blocks that draw nothing on the crossing net's
  own plane, the layer-aware obstacle check matters more; it is already in.
- **Automatic planarization is explicitly out of scope.** `gen-compose` does
  not compute the split (no `--check-planar`, no "route what you can on plane
  A, spill the rest to plane B"). The caller names planes; the router draws
  and checks them. That is a deliberate first increment: the assignment policy
  is a solver question, and this field is the contract any such solver would
  emit into.

## Known limitation: families with one routing plane

The mechanism can only name planes a family's own role table exposes. IHP's
`sg13g2`/`sg13cmos5l` tables exposed exactly one routing metal when #1474 was
filed, which made even `cross_block_layer_role` unnameable there; #1474 has
since landed and all four supported families (`sky130`, `gf180mcu`,
`sg13g2`, `sg13cmos5l`) now expose `metal`/`metal2`/`metal3` plus their
connecting vias. A family that exposes a single plane would still be unable to
express a non-planar graph — the limitation is documented here rather than
worked around, because the fix belongs in that family's role table (as #1474's
did), not in this field.

## Validation

Added with the implementation, all on sky130 against real `klt gen` blocks:

- A six-block K3,3 composition (each block spending its three `mos_array`
  ports on its three edges — a genuine K3,3, not a multigraph) routes **all
  nine nets in one `gen-compose` call** across two planes, is `klt drc` clean,
  and extracts to six nfets whose s/g/d bindings reproduce the K3,3 bipartite
  adjacency exactly, with `merged_net_labels: []`.
- The identical request with the per-net fields dropped leaves two nets
  unrouted with `crosses already-routed net` — the "before" state.
- A per-net override naming the primary plane is byte-identical on disk to
  omitting the field.
- One named leg of a 3-pin bundle routes on met1 while the rest of the net
  stays on li1; `klt extract` reports one conductor, and the layout is DRC
  clean.
