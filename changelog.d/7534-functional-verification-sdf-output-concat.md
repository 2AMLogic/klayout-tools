- **Added** (#7534, 2026-11-23): `klt functional-verification` with
  `options.sdf` now rewrites a top module's separately assigned multi-bit
  output (`wire b0, b1; assign y[0] = b0; assign y[1] = b1;` — the spelling
  P&R netlist writers emit) into the equivalent whole-vector
  `assign y = {b1,b0};` in a build-dir copy of the source
  (`.klt/functional-verification/klt_sdf_output_concat_sources/<n>/<file>`,
  the same normalization posture the SDF itself already gets), so the nonzero
  `INTERCONNECT` arcs onto those bits — typed-refused per #6937's bound —
  annotate and the run reaches a graded verdict without touching the
  caller's netlist or SDF (both stay byte-identical). The rewrite is
  disclosed as an additive `environment.sdf.output_assign_rewritten` list of
  `{port, bits, file}` entries (the file as its `{path, scope, sha256}`
  envelope), never sets `partial` and adds no `dropped` class: no timing is
  discarded, only respelled — **no `schema_version` bump** per
  `docs/json-contract.md`. Strictly gated: it fires only when the SDF
  carries at least one nonzero `INTERCONNECT` onto a bit of the port (an
  all-zero run keeps #2285's `zero_delay_alias_port_interconnect` drop,
  `partial: true` and `klt_sdf_alias_dropped.sdf` byte-identical to before)
  and only for a port klt can prove equivalent — every bit `0..N` of a plain
  `[N:0]` range assigned exactly once, each right-hand side a bare scalar
  identifier, and no other statement in the module driving or reading any
  bit (an instance port connection `.x(y[i])` counts as a driver), for ANSI
  and non-ANSI headers alike. Every other shape — a bit not assigned,
  assigned twice, or cell-driven, a non-identifier right-hand side, a range
  other than `[N:0]`, a top module not found — keeps #6937's typed
  pre-build refusal, simulator build never started. Direct cell-to-port
  connections and whole-vector assigns are unchanged (no copy, no new
  field). Verified live against Icarus 13.0 (`v13_0`) + cocotb 2.1.0.
