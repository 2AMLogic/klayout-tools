- **`klt sta` reports the true clock sink count** (#7417, 2026-11-23).
  `corners[].clock_propagation.clock_sinks` was derived from the pins named
  in the `report_clock_latency` dump -- but that report prints only the
  min- and max-latency sink per transition table (≤ 2 pins each), so a
  routed design with hundreds of sequential cells reported `5` while
  `clock_sinks_sample` happened to match it. The propagated-clock session
  now also runs OpenSTA's own sink-count query
  (`all_registers -clock [get_clocks <port>] -clock_pins`,
  live-verified on the pinned build to follow the clock through the CTS
  buffers) in a marker-delimited, `catch`-wrapped block, and `clock_sinks`
  is that engine-queried number. `0` keeps its "propagation could not be
  established" meaning (the query returns zero, or — on a count-marker-less
  stdout — the latency section printed with no rows); `null` means unknown
  (query absent/errored and no tree-less shape) — the latency report's
  distinct-pin count is never substituted, which is exactly the bug.
  `clock_sinks_sample` keeps its name and cap but is documented as the
  report-named min/max-extreme pins, not a sink enumeration;
  `max_network_latency_ns` is unchanged, as is every field name (no
  `schema_version` bump: the count under an already-shipped key changing
  what it counts is additive under the pre-1.0 policy,
  docs/json-contract.md § "Pre-1.0 caveat").
