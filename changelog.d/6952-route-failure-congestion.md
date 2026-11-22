- **`klt place-and-route` exposes structured congestion diagnostics on a
  failed route stage** (#6952, 2026-11-21): a route stage that exits nonzero
  (`GRT-0116` above all) still exits `1` with empty stdout, but its JSON
  error envelope now carries `error.congestion.global_route` — the `GRT-0096`
  per-layer table parsed from that attempt's captured stdout plus the
  worst-20 `overflow_regions` from the `*_route_congestion.rpt` report with
  macro attribution — using the same field names and `null_reasons` shape as
  the success response's `congestion` block, plus `congestion_report_path`
  naming the raw report. The table and the report are independent inputs,
  each `null` with a field-specific reason when that attempt did not produce
  it; any report file left by a prior attempt in a reused output directory is
  cleared before the route stage launches OpenROAD and never named as this
  attempt's evidence. The OpenROAD wrapper also now preserves structured error fields
  (`code`/`job_id`/`details`) when it appends the invocation record to a
  stage error, where they were previously dropped. Additive; the error
  message, exit code and stdout are unchanged for every non-route failure.
