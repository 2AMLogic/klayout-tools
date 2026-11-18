- **A 400-digit JSON integer is a documented error envelope, not an
  `OverflowError` traceback, across every request-JSON number guard**
  (#5994, 2026-11-17): `math.isfinite` raises `OverflowError` when handed the
  `int` the stdlib JSON parser produces for a huge integer literal, so any
  request field guarded by the hand-rolled
  `isinstance(..., bool) or not isinstance(..., (int, float)) or not
  math.isfinite(...)` chain crashed with a traceback instead of the verb's
  documented error (the same defect class #5891 fixed in five validators).
  All 28 raw-JSON guard sites now route through
  `_jsonnum.is_finite_number`, which catches the overflow: the eight curated
  sites (`sim_corners` nodeset volts, `sim_passes` alter values,
  `sim_batch`'s `max_job_seconds` / `capacity_wait_s` / `interrupted_grace_s`,
  `keepout_check --margin`, `sim_settled` `tol_pct`,
  `constraints.power_activity`) plus the same-shaped guards found by
  re-reviewing every `math.isfinite` hit (`clock_uncertainty_ns`,
  corner-matrix measurements, ERC antenna ratios, place-and-route request
  numbers, resistance-graph resistors, attestation timeouts, ngspice options,
  nodeset matches, `mismatch_scale`, corner-point params, crossings levels,
  step alters/pwl vectors, supply-relative fractions, Tian starts, abc_constr
  loads, yosys timeouts, yield target sigmas). Exception types and wording
  are unchanged for ordinary input. Engine-float `isfinite` uses are
  untouched; no output-shape change; no `schema_version` bump.
