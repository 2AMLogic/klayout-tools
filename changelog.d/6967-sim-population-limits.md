- **`klt sim` `measurements[].population_limits` bounds the Monte Carlo
  dispersion itself** (#6967, additive): a per-measurement
  `population_limits: {max_k_sigma[, quantization_variance]}` grades each
  `by_corner[]` population's `k·σ_net` — σ net of the optional declared
  quantization variance, `sqrt(max(σ² − v, 0))` — against the upper bound,
  worst corner first. The worst corner (smallest margin, first in corner
  order on ties) is named in the additive `monte_carlo.population_limits`
  block (`statistic`/`k`/`max`/`quantization_variance`/`status`/`margin`/
  `worst_corner`/`by_corner`), and a population `fail` fails the measurement
  and the run (exit 3) unless `error`/`inconclusive` outrank it. The mean,
  the pooled population, per-sample `limits`, `sigma_window`, and
  `window_scope` semantics are untouched: a corner with `n < 2` trusted
  samples is not graded, and omitting the field keeps responses
  byte-identical. Invalid declarations (non-object, missing/negative/
  non-numeric `max_k_sigma`, bad `quantization_variance`, unknown key, no
  `monte_carlo`, no resolvable `k_sigma`) are `SimError`s naming the field,
  raised before any corner is dispatched. No JSON-contract change (additive
  only, no `schema_version` bump).
