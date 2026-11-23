- **`klt place-and-route` raises request-level validation errors once, before any `rebuild_corners` unit starts** (#7538, 2026-11-23).
  A defect in any corner-independent field (`target_stage`, `floorplan`,
  `io`, `output_dbu_um`, `post_route_sdf`, ...) used to be recorded once per
  corner in `corner_runs[].error` under `pdk.rebuild_corners`; it now raises
  a single `PlaceAndRouteError` carrying the same message the single-corner
  path gives, and no unit directory is written. Corner-dependent failures are
  still recorded per corner.
