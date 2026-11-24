- **`klt synthesize` recovers ABC timing on WASI engines (#7010, 2026-11-23)**: the
  YoWASP/WASI Yosys wheels run ABC in-process and lose its redirected
  stdout, so the `stime -p` summary never reached the captured
  `<top>_abc.log` and `timing` was unconditionally `null` with a
  `timing_unavailable` warning even though the mapping completed. On the
  constrained mapping path, `klt synthesize` now stages the engine's own
  documented constrained script (parsed from `help abc`, so the selected
  mapping commands — `-D` substitution included — are preserved verbatim;
  the mapped netlist is byte-identical) with the `stime` tail widened to
  `stime -pd`, and executes Yosys from the run directory so ABC's
  statistics dump (`stats.txt`) lands beside that invocation's own
  artifacts. A single-region design then reports `timing` with
  `"source": "abc_stime_stats_file"`, an integer `critical_path_ps`
  (truncated by ABC; echoed as the additive `precision_ps: 1`), `null`
  `wire_load` (unknown, not "none"), and the region's module from the ABC
  log banner. The captured log stays authoritative — native engines keep
  the full-precision per-region `abc_stime` report unchanged — and
  anything ambiguous stays honestly `null` with the warning: multi-region
  designs on WASI engines (the dump's later records are percentages, not
  delays), a missing dump, or an engine whose help does not document the
  constrained script (the capture silently does not stage and today's
  behaviour stands). Each eligible arithmetic `auto` trial can recover its own single-region timing
  from its own statistics dump (#7504).
