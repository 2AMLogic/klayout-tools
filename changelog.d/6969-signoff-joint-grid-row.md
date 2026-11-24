- **`klt signoff` grades a joint PVT-grid row across several benches** (#6969, 2026-11-22).
  A `spec_rows` row on item 5 may carry `joint_grid`: an explicit `members`
  list of the item's citations plus the PVT axes of a `klt sim` `corners`
  block. The row counts as `derived` coverage only when every named member
  graded `met`, its envelope's `corners[]` covers the full grid with every
  corner at each point `pass`, and it carries a passing, `limits`-bearing
  condition probe tagged with the row id. Otherwise the item is
  `unmet`/`partial_coverage` with `detail.joint_rows` naming each member's
  missing points and missing probe; citing the row on one bench's `rows`, a
  malformed form, an uncited member name, or a Monte Carlo member is
  `invalid_evidence`. The per-spec-row table renders the row as a `joint`
  record with the per-member projection.
