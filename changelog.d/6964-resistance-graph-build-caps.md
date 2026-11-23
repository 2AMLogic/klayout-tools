- **`klt extract` and `klt pex --resistance-graph` can raise the resistance
  graph's four build caps** (#6964, 2026-11-21): `--resistance-graph-max-terminals`
  (default 64), `--resistance-graph-max-cells` (4096), `--resistance-graph-max-nodes`
  (5000) and `--resistance-graph-max-edges` (20000) expose the builder's own
  keyword arguments, which no flag reached. A power rail with 65 or more terminals,
  or a poured polygon needing more than 4096 mesh cells, stayed
  `graph_too_large` on the legacy series-sum scalar. Each flag is a positive
  integer that changes only its own limit and requires `--resistance-graph`
  (request fields `resistance_graph_max_*`); a `graph_too_large` diagnostic names
  the cap exceeded and the value applied. `klt pex` passes them through and, when
  any was given, records all four effective values under
  `extraction.resistance_graph.caps` -- absent otherwise, so existing output is
  byte-identical. Raising a cap costs time and output size, not a different
  model: the terminal matrix is `T x T` from `sum(T_c - 1)` injection solves per
  net. Additive; no `schema_version` change.
