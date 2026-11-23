- **Added** (#5894, 2026-11-17): `klt pex --resistance-graph` echo entries
  (`extraction.resistance_graph.nets[]`) now also carry `terminals` and the
  full `pair_resistance_ohm` matrix, copied verbatim from `klt extract`'s own
  per-net graph. The lumped `resistance_ohm` a `legacy` net keeps is a series
  sum of all the net's fragments, so a tree-routed net's total can overstate
  the terminal-to-terminal path (stub arms to terminals that carry no DC
  current, e.g. MOS gates, are summed in), and nets with three or more
  terminals always keep `legacy` — the pair matrix is what a caller reads
  for the path resistance between two named terminals. Echo fields only; no
  solver or netlist change. Output without the flag is byte-identical; no
  `schema_version` bump. See `docs/cli/pex.md`, `docs/cli/extract.md`'s
  "Terminal-aware resistance graph" and "Known limitations of the series
  sum" notes, and `docs/json-contract.md`.
