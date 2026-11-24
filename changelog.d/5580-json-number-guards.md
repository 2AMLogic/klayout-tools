- **`klt place-and-route` / `klt sim`: a huge JSON number is non-finite, not a
  crash** (#5580, 2026-11-17): the remaining private copies of the shared
  JSON-number predicates (`place_and_route_groups._is_finite_number`,
  `sim_process_legs._is_finite_number`, `route_request._is_number`) moved onto
  `_jsonnum`, whose finiteness test survives an integer too large for a float —
  it returns `False` where the inlined `math.isfinite(value)` raised
  `OverflowError`. A `region_um` coordinate or `order.pitch_um` beyond float
  range now fails `klt place-and-route` request validation with the usual
  message instead of a traceback, and the `klt sim` process-leg diagnostic
  treats such a measurement as non-finite and skips it. Error `code`/`message`
  text is unchanged; `schema_version` is unaffected.
