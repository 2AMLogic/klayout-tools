- **`klt lvs` accepts a fixed part-select operand inside an `assign`
  concatenation, and validates a `gate-level-verilog` reference before
  layout extraction** (2026-11-24, additive — `klt lvs`
  `schema_version` unchanged): a Yosys-written
  `assign x = { a[7:0], a };` was refused with "operand 'a[7:0]' is a
  multi-bit part-select, which is not supported inside a concatenation" —
  and only *after* device extraction had finished (minutes on a large
  layout). A concatenation right-hand-side operand may now be a fixed
  part-select of a declared bus in its declared direction (`a[7:4]`, also
  on an escaped base `\bus [7:0]`), including inside a nested or replicated
  operand; the same total-width requirement, and the same rejection for a
  slice with non-integer bounds, an undeclared base, or one against the
  declared direction, apply as for a standalone part-select `assign`
  (issue #2941). Independently, the whole gate-level conversion now runs
  before the extraction stage, so any reference-input error it can raise
  names the failing `reference.netlist` immediately instead of after
  extraction; a valid reference is converted once up front and reused, with
  unchanged pin bindings, warnings, and provenance. See
  `docs/cli/lvs.md`, "Digital gate-level LVS". (#7822)
