# `klt yield-threshold`

Probit threshold fit for **binary-outcome decision circuits** — a clocked
comparator, latch, sense amplifier or trip point
([#2976](https://github.com/2AMLogic/klayout-tools/issues/2976)).

Such a circuit never reveals its input-referred offset: each Monte Carlo
sample only reports "decided high / decided low" at the stimulus level it was
driven with. `klt sim`'s `measurements[].monte_carlo` statistics reduce
*continuous* values, and [`klt yield`](yield.md) grades pass/fail against
limits; neither can recover a threshold. `klt yield-threshold` fits

```
P(decided high | stimulus s) = Phi((s - mu) / sigma)
```

to the per-sample outcomes and reports the threshold mean `mu` and sigma
`sigma` with confidence intervals — per corner.

```
klt yield-threshold <records.json> [--confidence 0.95]
                    [--target-error-rate RATE]
                    [--threshold T [--outcome-when above|below]]
                    [--format text|json]
```

Pure Python (stdlib only); it needs no native extension, `ngspice` or KLayout.
It runs no simulations — the caller produces the `(stimulus, outcome)` records
(generating the sweep inside `klt sim` is out of scope).

## Input: the records document

Either a `corners` list …

```json
{"corners": [
  {"name": "tt", "records": [{"stimulus": -2.0, "outcome": false},
                             {"stimulus":  2.0, "outcome": true}]},
  {"name": "ss", "records": [ ... ]}
]}
```

… or a flat `records` list whose records may carry a `corner` (default
`"default"`):

```json
{"records": [{"corner": "tt", "stimulus": 1.5, "outcome": true}]}
```

Each record needs a finite numeric `stimulus` and **either**

- `outcome`: `true`/`false` (or `1`/`0`) — decided high / low, **or**
- `value`: a number, with `--threshold T` on the command line — the outcome is
  `value > T` (`--outcome-when above`, the default) or `value < T`
  (`--outcome-when below`). Use this to derive the outcome from a measurement.

A record with `"status": "errored"` or `"inconclusive"` is excluded from the
fit and counted in `excluded` (the same split `klt sim`'s statistics use). Any
other `status` except `"ok"` is an input error.

The fraction deciding high must rise with the stimulus; otherwise the corner
reports `non_increasing`. For a circuit that decides high on a *falling*
stimulus, negate the stimulus (or flip each `outcome`) before fitting.

## Output

A flat payload through the shared envelope in
[`docs/json-contract.md`](../json-contract.md); `schema_version` is `1`.

| Field | Meaning |
| --- | --- |
| `input` | `path` and `content_hash` (`sha256:…`) of the records document. |
| `confidence` | The level used for every interval. |
| `target_error_rate` | The requested `--target-error-rate`, or `null`. |
| `outcome` | `derived_from` (`"outcome"` or `"value"`), `threshold`, `high_when`. |
| `corners[]` | One entry per corner, in input order (below). |

Each `corners[]` entry:

| Field | Meaning |
| --- | --- |
| `name` | Corner name. |
| `n_samples` | Samples used. |
| `excluded` | `{errored, inconclusive}` counts of excluded records. |
| `levels[]` | Per distinct stimulus: `stimulus`, `n`, `high`, `proportion`, `ci` (exact **Clopper-Pearson** `[lower, upper]` at `confidence`). |
| `fit` | The probit fit (below). |
| `goodness_of_fit` | `null` unless `fit.status` is `ok`; else the block below. |
| `target` | `null` unless `--target-error-rate` was given; else the block below. |

### `fit`

| Field | Meaning |
| --- | --- |
| `status` | `ok`, or why there is no estimate — see below. |
| `reason` | `null` when `ok`; otherwise a sentence. |
| `mu`, `sigma` | Maximum-likelihood threshold mean and sigma, or `null`. |
| `mu_ci`, `sigma_ci` | `{lower, upper}` **profile-likelihood** intervals (the likelihood-ratio contour at the chi-square(1) quantile). A `null` bound means the profile did not cross within the search range — the interval is open on that side. |
| `log_likelihood`, `iterations` | Fit diagnostics (`null` when no estimate). |
| `transition_interval` | Present only when `status` is `separated`: `[low, high]` bracketing the transition. |

The chi-square interval on a sample standard deviation does **not** apply
here — the threshold is never observed directly — which is why the intervals
come from the profile likelihood.

**Degenerate data is reported, never faked.** `mu`, `sigma` and both intervals
are `null` (mirroring `stddev: null` for `n < 2` in `klt sim`) and `status` is:

| `status` | Data |
| --- | --- |
| `all_one_outcome` | Every sample decided the same way; the sweep does not bracket the threshold. |
| `insufficient_stimulus_levels` | Fewer than 2 distinct stimulus levels. |
| `separated` | The outcomes do not overlap in stimulus (the likelihood is maximised as `sigma → 0`); only `transition_interval` is known. |
| `non_increasing` | The fraction deciding high does not rise with the stimulus. |
| `non_convergence` | The iteration failed to converge. |
| `no_samples` | No usable samples. |

A corner with such a status is still a successful run (exit `0`); the caller
reads `fit.status`.

### `goodness_of_fit`

`pearson_chi2`, `deviance`, `dof` (= levels − 2), `p_value` (chi-square
reference), `reliable` and `reason`. `reliable` is `false` — and `reason` says
why — when any expected count (either outcome) is below 5 or `dof ≤ 0`
(`p_value` is then `null` for `dof ≤ 0`). A small `p_value` means the response
is not a normal CDF in the stimulus.

### `target` (with `--target-error-rate RATE`)

The wrong-decision rate at stimulus `s` is `Phi(-|s - mu| / sigma)`, so it
reaches `RATE` at `mu ± sigma · Phi⁻¹(1 − RATE)`.

| Field | Meaning |
| --- | --- |
| `error_rate` | `RATE`. |
| `stimulus_low`, `stimulus_high` | The two stimuli at that error rate (`null` if no fit). |
| `stimulus_high_conservative`, `stimulus_low_conservative` | Same, using the profile-interval bound of `mu` and the **upper** `sigma` bound — a conservative bound, not an exact interval. `null` when a needed bound is open. |
| `resolvable_error_rate` | The smallest rate `n_samples` can resolve at `confidence`: the Clopper-Pearson upper bound after **zero** errors, `1 − ((1 − confidence)/2)^(1/N)`. |
| `rule_of_three_error_rate` | `3 / N`, for comparison. |
| `extrapolated` | `true` when `RATE < resolvable_error_rate` (no direct evidence at that rate) **or** the stimulus lies outside the swept range. |
| `outside_stimulus_sweep` | `true` when either stimulus lies outside the swept range, else `false` (`null` if no fit). |

An `extrapolated: true` stimulus is a model extrapolation from the normal-CDF
assumption, not something the sample count resolves; treat it accordingly.

## Numerics

- `log Phi` uses `erfc` in the body and an asymptotic series below −20 σ, and
  the score uses the inverse Mills ratio in log space, so the fit stays finite
  and accurate far past the ≈ −38 σ point where a naive CDF underflows.
- Maximum likelihood is Newton's method with step halving on the (concave)
  `alpha + beta · s` linear predictor.
- Clopper-Pearson is implemented in `probit.py` rather than reusing
  `native/yield`: that crate exposes only whole-request entry points to
  Python, and this verb must not require the native build. Both are checked
  against the same reference values in `tests/test_probit.py`.

## Exit codes

| Exit code | Meaning |
| --- | --- |
| `0` | Every corner reported (including corners with a non-`ok` `fit.status`). |
| `1` | The records document is missing, unreadable or malformed, or an option is invalid (`--confidence` outside (0, 1), `--target-error-rate` outside (0, 0.5), `value` records without `--threshold`). JSON error envelope on stderr under `--format json`; stdout empty. |
| `2` | Usage error (argparse). |
