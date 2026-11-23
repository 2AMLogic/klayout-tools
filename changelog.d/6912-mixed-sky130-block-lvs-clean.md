- **`klt layout-plan execute` derives a MOS group's `flavor` from its
  declared `device_class`** (#6912, 2026-11-23): a `mos_array`/`diff_pair`
  group whose `params` do not name a `flavor` now reads it
  from the netlist digest's declared class — `PFET` draws `pfet` (the
  flavor that draws its own well), `NFET` draws `nfet` — mirroring #6919's
  `res_array` derivation. Pre-derivation, a `device_class: "PFET"` group
  silently drew the `nfet` default: no well, wrong polarity, a layout that
  could never LVS-match its netlist. The derivation is recorded in
  `resolved_params` plus one `warnings[]` entry; an explicit
  `params.flavor` still wins; mixed classes in one group warn and use the
  first; any other class name derives nothing. `esd_device` is left
  alone: it declares no `flavor` param (always an NMOS-style device), so
  nothing is derived or injected for it.
- **The router's automatic plane climb (#6920) now also lifts the
  self-net pad-crossing classes (#433/#453/#1527) and the
  guard/collector-ring classes (#199 case 2 / #434)** (#6912, 2026-11-23):
  a leg refused for crossing its own block's pad metal, or for crossing a
  guard/collector ring's loop or missing its declared opening, is retried
  on each higher metal before the net is reported unroutable. Safe because
  both families are already plane-conditional — the pad checks skip a pad
  on another physical layer and the drawn-metal reads read the obstacle's
  geometry on the route layer, while the ring admission re-proves, per
  attempt, that the climbed backbone and every via-drop landing pad share
  no conductor with the ring's trace (#1960's `_leg_plane_clears_ring`).
  A net that names its own plane (`connectivity[].layer_role` /
  `routing.nets[].layer_role`, #1655 "explicit wins") still never climbs,
  so the same-plane rejections and messages stay observable verbatim.
  Together these let `layout-plan execute` place own-ringed pfet groups in
  a mixed row (their inner S/D/G ports route over their tie rings) and
  close #6912's fixture: the six-device mixed sky130 `glitch_filter` block
  now routes under default routing, `klt lvs` reports `match` as a
  four-terminal compare (resistor as the declared `res_xhigh_po` with its
  well tie on `GND`, pfet bodies on `VDD33`, nfet bodies on `GND`, body
  check `verified` — nothing ignored or waived), and `klt drc --deck
  sky130` is `clean` as drawn. The fixture's substrate tie uses
  `guard_ring` with `params.add_well: false`: at the default `add_well:
  true` every tap is a *well* tie, so a `net: "GND"` ring leaves the NFET
  bodies (and a precision resistor's `W` terminal) on the deck's
  synthesized `vsubs` global. `docs/cli/layout-plan-execute.md` names this
  block as the largest mixed-device sky130 block verified to route,
  LVS-match and run DRC-clean end to end. No `schema_version` change: the
  new `warnings[]` entries are additive strings.
