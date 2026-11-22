- **`klt functional-verification` accepts `testbench.plusargs` on the cocotb
  path** (#6906, 2026-11-21): the field the Verilog-bench form gained in
  #5006 now also works with `testbench.module` — one argv element per entry,
  forwarded to the simulator run (`Runner.test(plusargs=...)`; on Icarus they
  land after the `.vvp` path), so a DUT that loads a ROM image, seed or mode
  through `$value$plusargs` is verified unedited instead of gaining a
  tool-specific ifdef. Same shape rules and error messages as the
  Verilog-bench field; a plusarg *named* `klt_init_seed` is exit 1 (that name
  belongs to `options.initial_state`'s own generated plusarg) whether or not
  `initial_state` is set, and with `initial_state.mode: "random"` each
  per-seed run receives the user entries followed by its own
  `+klt_init_seed=<n>`. Entries are not path-rewritten: the simulator's cwd
  is the output directory (`<request_dir>/.klt/functional-verification` by
  default), so file arguments are absolute or relative to that directory.
  Plusargs apply under `options.sdf` and to `--mutations` baseline and
  variants alike, and `--check` validates the field offline. The report
  echoes them as `environment.plusargs` (`[]` when absent — the only change
  to an existing report); `provenance.input.content_hash` stays
  RTL-sources-only and does not hash a plusarg-referenced file. Additive; no
  `schema_version` change.
