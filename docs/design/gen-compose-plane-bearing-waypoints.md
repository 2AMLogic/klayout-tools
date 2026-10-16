# Decision: plane-bearing waypoints for `klt gen-compose`

**Status:** decision record, implemented by the same PR that adds this file
(issue #2930). It follows
[`gen-compose-per-net-layer.md`](gen-compose-per-net-layer.md) (#1655), which
settled *which plane a net or a leg draws on* and left one thing out: a net
changes plane only at a **block port**, through the #454 via-drop. This record
settles how a leg changes plane at an *interior point*. Nothing here adds a
dependency, a `klt` verb, or a `schema_version` bump.

## Why this exists

A two-layer H/V channel route (horizontals on one metal, verticals on the
next, a via at each corner) cannot be expressed per leg: a leg is one plane
end to end, and its only plane change is the via-drop at its two pins. The
workaround was a caller-built junction GDS cell with four co-located ports per
corner — a hand-made block per via, which defeats generating the layout from
JSON alone.

## Options considered

- **A. Plane-bearing waypoint** — a waypoint may be `[x, y, "layer_role"]`;
  the leg changes plane there. Additive: the two-number form is unchanged.
- **B. Net-level H/V mode** (`orientation_layers`) — vias auto-placed at every
  corner. Matches the "channel route" mental model, but is the largest change
  and collides with track assignment (#1467/#1962): which corners get vias is
  a routing decision, not a request field.
- **C. Built-in junction pseudo-block** the caller places by hand. Reuses the
  leg logic unchanged, but keeps one extra caller-declared block per via and
  the "reach the pin from behind" rule special-cased.

## Decision: option A

```jsonc
"legs": [{
  "from_pin": {...}, "to_pin": {...},
  "layer_role": "metal2",                       // plane the leg starts on
  "waypoints_um": [
    [5.6, 2.5],                                 // plain waypoint, same plane
    [7.0, 2.5, "metal3"],                       // plane change: continues on metal3
    [9.8, 2.5], [9.8, 0.21]
  ]
}]
```

The role names the plane the route **continues on after** that point. A 2-pin
net's own `waypoints_um` takes the same form (the net is then routed as that
single named leg). A role equal to the plane already being drawn on is a plain
waypoint.

### Implementation: reuse the pin machinery, no new geometry

The decisive observation is that a plane change *is* a via-drop to "a pin on
the other plane" — exactly what `route_two_pin()` already draws at a block
port. So the leg is split at each plane-bearing waypoint into one same-plane
**segment** per plane, and each segment is routed by the ordinary
`route_two_pin()` between two ends that are a real pin or a **junction**: a
synthetic, zero-area, one-port block placed at the waypoint whose port sits on
the plane the route continues on.

- The segment arriving at a junction drops to the junction's plane through
  the same #454/#1567 ladder (`_resolve_via_drop_layer`) it would use for a
  block pad, with its landing pads on both planes at the waypoint — so the
  via ladder is DRC-legal by the same enclosure/min-area sizing every other
  drop already gets.
- The segment leaving the junction starts on that plane with no drop; it merges
  with the landing pad the ladder drew.
- A junction port has no approach stub (`junction: true` makes the stub zero),
  so the path meets it exactly at the waypoint.

This answers the issue's open question: the via-drop helper is not invoked at
an arbitrary point — a junction pin makes the point an ordinary pin.

### Checks run per segment, on that segment's plane

Because each segment is a normal `route_two_pin()` call, every routability
check (obstacle overlap, guard rings, own-block shorts, via-drop resolution)
and the route-vs-route `leg_conflict` run **per segment against the plane it
is drawn on**: two same-plane segments of different nets that cross are still
rejected (`crosses already-routed net '…'`); segments on different planes are
not compared as a short. A segment's rejection names the plane change it
belongs to (`plane change at waypoints_um[k] (x, y): …`).

A leg with plane-bearing waypoints is pinned like any layer-pinned leg (#1655):
no cross-block retry, and no automatic spanning-tree re-pickup of its pin pair
if a segment is rejected.

### Response and drawing

The response is unchanged: one `legs[]` entry per named leg (its `pins` are the
real pins; junctions never appear). Internally the leg carries one drawing
payload per plane (`segments`); `compose()` draws, labels (one label per net)
and records each for later nets' route-vs-route comparison.

### Why B and C are rejected

- **B** decides *where* vias go on the caller's behalf for every corner of
  every net; that is track assignment's job (#1467) and would have to be
  designed together with it. Option A is the primitive such a solver would
  emit into, the same argument #1655 made for per-net planes.
- **C** leaves the hand-declared block per via; A needs only coordinates.

## Contract

- **Additive, no `schema_version` bump.** The two-number waypoint is unchanged;
  a request without a third element is composed byte-for-byte as before (the
  `sky130_5t_ota_gen_compose` golden is untouched).
- **Errors are application errors (exit 1)**, the existing `GenComposeError`
  shape: a malformed third element (`...waypoints_um[k][2] must be a non-empty
  layer role string`), a waypoint with more than three elements, an unknown
  role or a role with no layer in the family's deck (re-labelled to name
  `legs[i].waypoints_um[k]`), and an inherited width below the new plane's
  deck floor (names `legs[i].width_um`). A plane with no via ladder to the
  adjacent plane is a per-leg rejection (`routed: false`, `reason`), never a
  disconnected stub.

## Known limitations

- A segment with no waypoints of its own between a pin and a junction is drawn
  with a single elbow (x first, then y) from the pin's stub — the same corner
  convention `waypoints_um` already uses. Steer it with a plain waypoint.
- Plane-bearing waypoints are honoured in `legs[].waypoints_um` and a 2-pin
  net's `waypoints_um`; a bundle net uses `legs[]` as for any per-leg steering.
