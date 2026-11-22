- **`klt power` reports the drawn cross-section density beside the per-row
  one** (#6525, 2026-11-18): each `ir_drop_map.nets[].islands[].edges[]`
  entry gains `cross_section_density_a_per_um` — `abs(Σ signed branch
  currents) / Σ row widths` of the highest-density eligible multi-branch
  section the edge lies in, `null` for an edge in no such section (a single
  row, a via, an ineligible cut, a member with no solved current, an unsolved
  island) and `0.0` for an eligible section with zero net current. #6475
  fixed the EM *verdict* on a lengthwise-split arm but left
  `current_density_a_per_um` — the per-mesh-row solver diagnostic — reading
  2× the drawn density on the stair-step case (0.0014 against 0.0007 A/µm;
  that is an example, not a bound); that field is unchanged, and the new one
  needs no declared limit, so a role the PDK publishes no limit for gets the
  drawn-section number too.
  Additive: no `schema_version` bump, and `em_verdict`, `coverage` and exit
  codes are byte-identical for identical inputs.
