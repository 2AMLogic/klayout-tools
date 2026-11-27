- **`klt characterize`: asynchronous set/reset arcs for sequential cells**
  (#5520, 2026-11-13): a sequential cell may now declare its async pins with
  `cell.async_pins` (an array of `{"pin", "kind": "clear"|"preset",
  "active": "low"|"high"}`, requiring `cell.clock_pin`). Each declared pin is
  held at its **inactive** level for the existing clock-to-output and
  setup/hold measurements, and gains two new kinds of Liberty group: a
  `preset`/`clear` propagation arc into each output it forces (one direction
  only -- `cell_rise`/`rise_transition` where the output is driven high,
  `cell_fall`/`fall_transition` where it is driven low), and `recovery_*` /
  `removal_*` constraint groups against the clock, found with the same
  lockstep bisection setup/hold uses. Which way each output is forced is
  *measured*, not inferred from `kind`, so a clear pin reads `clear` on `Q`
  and `preset` on an inverted `QN`. Removal passes on the clock edge being
  *ignored* (no output transition), so its entries report
  `reference_delay_ns` and `degradation` as `null`. Additive: the cell
  response gains `constraints[]` entries of `kind` `recovery`/`removal` and,
  **only for a cell that declares `async_pins`**, an `async_arcs[]` list --
  a request without them emits a byte-identical `.lib` and response. No
  `schema_version` bump. Worked example:
  `examples/characterize/request-dffsr.json`. Constraint probe decks now
  carry `.options abstol=1e-10`: a probe on the metastability boundary made
  ngspice 42 abort the whole transient at the release ramp's PWL breakpoint
  (`Timestep too small`), losing every probe batched into that deck. The
  emitted `.lib` is byte-identical with and without the card, on ngspice 42
  and 47 alike.
