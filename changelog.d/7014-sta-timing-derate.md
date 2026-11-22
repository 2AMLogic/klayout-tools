- **`klt sta` accepts `constraints.timing_derate`** (#7014, 2026-11-21). A
  request can now carry the `set_timing_derate -early/-late` factors a
  flow's SDC applies: `{"early": 0.95, "late": 1.05}` (either side optional,
  each a finite number `> 0`, `1.0` neutral) emits one global
  `set_timing_derate` per supplied side in every analyzed corner of both
  `def` and `verilog` mode. The response gains an always-present top-level
  `timing_derate` block (`{"applied": false}` when omitted) so a derated
  slack is distinguishable from an un-derated one. Omitting the field leaves
  the generated script byte-for-byte unchanged. Additive; no
  `schema_version` change.
