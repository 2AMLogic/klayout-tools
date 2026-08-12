# `klt yield`

Turn a Monte Carlo **sample set** plus its **spec limits** into a yield
estimate — with a confidence interval, a distribution fit, Cpk/sigma-to-spec,
and a sample-size verdict. Phase 1a of the statistical/yield epic
([#710](https://github.com/2AMLogic/klayout-tools/issues/710)), delivered by
[#816](https://github.com/2AMLogic/klayout-tools/issues/816).

```
klt yield <samples> [--limits <file>] [--confidence <c>]
                    [--target-ci-halfwidth <h>] [--min-samples <n>]
                    [--measurement <name>]... [--format text|json]
```

- `<samples>` — a `klt sim --format json` **Monte Carlo report**, or a plain
  **sample-set document**. Auto-detected; see "Input" below.
- `--limits` — a **spec-limits file** supplying (or overriding) each
  measurement's `min`/`max`/`target_yield`, plus optional run-level defaults.
  Required when the sample document carries no limits of its own.
- `--confidence` — two-sided confidence level for every reported interval,
  strictly between 0 and 1 (default `0.95`).
- `--target-ci-halfwidth` — the interval half-width, in absolute yield, the
  sample-size verdict is measured against (default `0.01`, i.e. ±1
  percentage point).
- `--min-samples` — minimum usable samples per measurement (default and hard
  floor `2`).
- `--measurement` — restrict the analysis to this measurement. Repeatable,
  and comma-separated names are accepted.
- `--format` — `text` (default) or `json`.

The command is headless and safe in CI, **except** that it requires the
`klt_yield_native` Rust extension to be built and importable — see "Building
the native extension" below.

## Never a bare point estimate

A yield number with no stated sample size and no confidence interval is worse
than no number: it looks like evidence and cannot be checked. Epic #710 makes
that a rule, and this command **enforces it rather than advising it**:

- **Every** yield number in the payload is an object carrying `estimate`,
  `confidence`, `confidence_interval` (`{low, high}`), and `n`. There is no
  shape in which an estimate travels without them — the Rust response types
  make it unrepresentable, and a final guard rejects a non-finite or inverted
  interval before anything is serialised.
- A request that could only produce a bare point estimate is an **error**
  (exit `1`), not a warning:

  | Request | Result |
  | --- | --- |
  | A measurement with fewer than 2 usable samples | error — no sample standard deviation, no interval |
  | `--min-samples 1` (or `0`) | error — below the hard floor of 2 |
  | `--confidence 1` (or `0`) | error — admits no finite interval |
  | A measurement with neither `min` nor `max` | error — no limit to estimate a yield against |

- A run with **zero observed failures** is reported as an interval bounded
  only from below (`[low, 1]`), plus a warning that spells out the honest
  statement: *"at least 98.7779% at 95% confidence, N = 300"*, not *"100%
  yield"*.

There is deliberately no flag to turn any of this off.

## Input

### `klt sim` Monte Carlo report (no intermediate format)

The primary input is the record format the canary blocks' MC harnesses
already produce: a `klt sim --format json` response whose request declared a
`monte_carlo` block ([`docs/cli/sim.md`](sim.md#monte-carlo-sampling)).
`klt yield` reads it directly.

```bash
klt sim mc-request.json --format json > mc.json
klt yield mc.json --limits spec-limits.json --format json
```

- **Samples** come from `corners[]` entries carrying a non-null
  `monte_carlo` block. Deterministic (non-sampled) corners in the same report
  are ignored — they are not draws from any distribution.
- **A `null` measurement value** counts into `errored` and is excluded from
  every statistic, exactly as `klt sim`'s own `monte_carlo` rollup does.
- **Spec limits** come from the report's own `measurements[].limits`, unless
  `--limits` overrides them.
- **`source_corners`** echoes the originating (pre-sampling) corner ids, with
  `klt sim`'s `/mc<i>` suffix stripped. Pooling a draw across more than one
  originating corner raises a warning: each corner is its own population, so
  the pooled estimate is a worst-case envelope rather than a distribution
  anyone sampled (the same caveat `klt sim`'s "Pooled vs. per corner" states).

### Sample-set document

For a draw that did not come from `klt sim`:

```json
{
  "measurements": [
    {
      "name": "vref",
      "unit": "V",
      "samples": [1.2035, 1.1987, 1.2101, "..."],
      "errored": 0,
      "limits": { "min": 1.15, "max": 1.25, "target_yield": 0.99 }
    }
  ]
}
```

| Field | Type | Description |
| --- | --- | --- |
| `name` | string | Measurement key, required. |
| `unit` | string | Optional, echoed back. |
| `samples` | array\<number \| null\> | The draw. A `null` entry counts into `errored` rather than being analysed. |
| `errored` | integer | Optional extra count of samples that produced no usable value (added to the `null`s found in `samples`). |
| `limits` | object | `min`/`max`/`target_yield`, each optional — but at least one of `min`/`max` must be resolvable (here or via `--limits`). |
| `source_corners` | array\<string\> | Optional; the originating corners the draw was pooled from. |

### Spec-limits file (`--limits`)

The statement of what the design is being held to. It **wins** over whatever
limits the sample document carried — it is the caller's explicit spec.

```json
{
  "confidence": 0.95,
  "target_ci_halfwidth": 0.01,
  "min_samples": 2,
  "target_yield": 0.99,
  "measurements": {
    "vref":  { "min": 1.15, "max": 1.25, "target_yield": 0.99 },
    "iq_ua": { "max": 10.0 }
  }
}
```

- `measurements` (object, required) — keyed by measurement name.
- The four run-level keys are defaults; a CLI flag beats the file, which
  beats the documented default. A run-level `target_yield` applies to every
  measurement that does not state its own.
- A measurement present in the sample document but **not** in this file keeps
  whatever limits it already had. A measurement that ends up with neither
  `min` nor `max` is skipped with a warning — unless it was named explicitly
  via `--measurement`, in which case it is an error.

## What is computed

### Distribution fit

A **normal** fit (the only family Phase 1a fits; `model` exists so a later
family is additive rather than a shape change), reported with `mean`,
`stddev` (Bessel-corrected sample standard deviation, matching `klt sim`'s
own estimator), `min`/`max`/`median`, `skewness`, and `excess_kurtosis`.

Alongside it, an **Anderson-Darling** normality verdict with both parameters
estimated (`A2*`, Stephens' finite-`n` adjustment), against the 5% critical
value `0.787`:

| `verdict` | Meaning |
| --- | --- |
| `consistent` | The sample set is not inconsistent with a normal. |
| `rejected` | `A2*` exceeds the critical value — a warning says so, and the parametric estimate and Cpk should be treated as indicative only. |
| `insufficient_samples` | `N < 8`, below where the critical values apply; normality was **not** tested. |
| `degenerate` | Zero sample spread; the statistic is undefined. |

### Two yield estimates, side by side

They fail in opposite directions, and disagreement is the useful signal:

| | `yield.empirical` | `yield.normal` |
| --- | --- | --- |
| `method` | `clopper-pearson` | `normal-delta` |
| Estimate | Fraction of samples inside the limits | The fitted normal's probability mass inside the limits |
| Interval | **Exact** binomial (Clopper-Pearson) | Delta method, propagating `Var(mu_hat) = sigma^2/n` and `Var(sigma_hat) = sigma^2/(2n)` |
| Assumes | Nothing about the distribution | Normality (check the verdict above) |
| Blind spot | Cannot see past the samples it has — with zero failures it can only bound from below | Extrapolates into a tail the samples never visited |

Clopper-Pearson rather than Wald/Wilson because a yield estimate lives
exactly where the normal approximation is worst: `p` near 1, often with zero
observed failures, where a Wald interval degenerates to the useless `[1, 1]`.
The parametric interval is clamped to `[0, 1]` (a probability cannot leave
it); a clamped endpoint is a signal that the approximation is being pushed
past where it is informative.

### Capability

| Field | Definition |
| --- | --- |
| `cp` | `(USL - LSL) / (6 sigma)` — `null` unless **both** limits are declared. |
| `cpk` | `min(USL - mu, mu - LSL) / (3 sigma)`, over whichever sides are declared. |
| `cpk_confidence_interval` | Bissell's normal approximation, `Cpk ± z * sqrt(1/(9n) + Cpk^2 / (2(n-1)))`. |
| `sigma_to_spec` | `3 * cpk` — the distance from the mean to the nearest limit, in sample standard deviations. |
| `sigma_to_spec_confidence_interval` | `3 ×` the `cpk` interval. |
| `limiting_side` | `"lower"` or `"upper"` — which limit `cpk` is measured against. |

All are `null` when the sample spread is zero (the fit is degenerate).

### Sample-size verdict

Two different questions, answered separately, because they have different
answers:

| Field | Question |
| --- | --- |
| `required_n` | **Precision**: how many samples the interval needs to reach `target_ci_halfwidth` at the observed pass rate. |
| `required_n_for_target` | **The claim**: how many samples the *lower* bound needs to reach the declared `target_yield` at the observed pass rate. `null` when no `target_yield` was declared, or when the observed pass rate is already at or below it — in which case no sample count helps, and it is the design falling short, not the campaign. |
| `verdict` | `sufficient` only when both are satisfied; `insufficient` otherwise. |
| `method` | How `required_n` was derived: `clopper-pearson-zero-failures` or `normal-approximation`. |

`required_n` uses the exact zero-failure form when nothing failed — the
Clopper-Pearson interval is then `[(alpha/2)^(1/n), 1]`, so a half-width
`<= h` needs `n >= ln(alpha/2) / ln(1 - 2h)` — and the standard
`n = z^2 p(1-p) / h^2` otherwise. `required_n_for_target` is searched
directly against the same exact interval the report publishes, so the two can
never disagree.

### Pass / fail

A measurement that declares a `target_yield` **passes only if the lower
confidence bound reaches it** — a claim at the stated confidence, not a point
estimate that happened to clear the bar. A measurement with no `target_yield`
is `reported`: it can never fail, exactly as an unlimited measurement in `klt
sim` is reported and never failed.

## Worked example

`examples/yield/` ships a synthetic, seeded 300-sample campaign for a
bandgap-shaped reference (`vref`, a two-sided 1.15–1.25 V window) and its
quiescent current (`iq_ua`, a one-sided 10 µA max), in **both** input shapes
over the identical draw:

| File | What it is |
| --- | --- |
| `mc-samples.json` | The sample-set document |
| `sim-report.json` | The same draw as a trimmed `klt sim` Monte Carlo report |
| `spec-limits.json` | The spec limits, with `target_yield: 0.99` on both |
| `generate.py` | Regenerates all three, byte-identically (fixed seed) |

```bash
klt yield examples/yield/mc-samples.json --limits examples/yield/spec-limits.json
```

```
samples: examples/yield/mc-samples.json
limits: examples/yield/spec-limits.json
source: sample-set  samples_analyzed: 600
confidence: 0.95  target_ci_halfwidth: 0.01
status: fail  measurements: 2

vref [V]  status: fail  N: 300
  limits: min=1.15 max=1.25 target_yield=0.99
  fit (normal): mean=1.2015 stddev=0.0159719 normality=consistent
  yield (empirical, clopper-pearson): 1.000000 [0.987779, 1.000000] at 95%, N=300
  yield (normal, normal-delta): 0.998172 [0.996626, 0.999718] at 95%, N=300
  capability: cp=1.0435 cpk=1.0122 [0.9227, 1.1016] sigma_to_spec=3.0365 limiting=upper
  sample size: insufficient (N=300, required>=183, observed +/-0.006110 vs target +/-0.01)
    to claim target_yield=0.99: N>=368
  warning: measurement 'vref': no sample fell outside the limits, so the empirical
    yield is bounded only from below -- "at least 98.7779% at 95% confidence,
    N = 300" is the honest statement, not "100% yield"
  warning: measurement 'vref': the observed pass rate clears the 0.99 target, but
    N = 300 is too small to *claim* it at 95% confidence (the lower bound is
    0.987779); 368 samples at this rate would support the claim
```

**Read the verdict, not the point estimate.** Not one of the 300 `vref`
samples missed the window, and a tool that stopped there would report "100%
yield". What is actually supported at 95% confidence is *at least 98.78%* —
which does **not** clear the declared 99% target, so the run exits `3`. The
campaign is not wrong; it is 68 samples short of being able to make the claim,
and the report says exactly that. Running `sim-report.json` instead produces
the identical statistics — the point of consuming the MC record format
directly.

## JSON schema (the contract)

```json
{
  "samples": "examples/yield/mc-samples.json",
  "limits": "examples/yield/spec-limits.json",
  "source": {
    "kind": "sample-set",
    "netlist": null,
    "monte_carlo": null,
    "sample_count": 600
  },
  "schema_version": 1,
  "confidence": 0.95,
  "target_ci_halfwidth": 0.01,
  "min_samples": 2,
  "status": "fail",
  "measurement_count": 2,
  "measurements": [
    {
      "name": "vref",
      "unit": "V",
      "n": 300,
      "errored": 0,
      "limits": { "min": 1.15, "max": 1.25, "target_yield": 0.99 },
      "source_corners": [],
      "distribution": {
        "model": "normal",
        "mean": 1.201501763333333,
        "stddev": 0.015971884739161467,
        "min": 1.163617,
        "max": 1.249805,
        "median": 1.201084,
        "skewness": 0.0711006078204,
        "excess_kurtosis": -0.2162306276710,
        "normality": {
          "test": "anderson-darling",
          "statistic": 0.2171564757018,
          "critical_value": 0.787,
          "significance": 0.05,
          "verdict": "consistent"
        }
      },
      "yield": {
        "empirical": {
          "method": "clopper-pearson",
          "estimate": 1.0,
          "confidence": 0.95,
          "confidence_interval": { "low": 0.987779025305707, "high": 1.0 },
          "n": 300
        },
        "normal": {
          "method": "normal-delta",
          "estimate": 0.998172285990072,
          "confidence": 0.95,
          "confidence_interval": { "low": 0.9966262637409, "high": 0.9997183082392 },
          "n": 300
        }
      },
      "capability": {
        "cp": 1.043500309378121,
        "cpk": 1.0121584993192079,
        "cpk_confidence_interval": { "low": 0.9226947359884, "high": 1.1016222626500 },
        "sigma_to_spec": 3.036475497957624,
        "sigma_to_spec_confidence_interval": { "low": 2.7680842079653, "high": 3.3048667879500 },
        "limiting_side": "upper"
      },
      "sample_size": {
        "n": 300,
        "observed_ci_halfwidth": 0.0061104873471465,
        "target_ci_halfwidth": 0.01,
        "required_n": 183,
        "required_n_for_target": 368,
        "verdict": "insufficient",
        "method": "clopper-pearson-zero-failures"
      },
      "status": "fail",
      "warnings": ["..."]
    }
  ],
  "warnings": []
}
```

### Top-level fields

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | Version of this command's JSON shape (starts at `1`; per-command, per [`docs/json-contract.md`](../json-contract.md)). |
| `samples` | string | Echo of the `<samples>` argument. |
| `limits` | string \| null | Echo of `--limits`, or `null`. |
| `source` | object | `kind` (`"sim-report"` or `"sample-set"`), `netlist` and `monte_carlo` (echoed from a sim report, else `null`), and `sample_count` (usable samples analysed across every reported measurement). |
| `confidence` | number | Effective two-sided confidence level. |
| `target_ci_halfwidth` | number | Effective sample-size target. |
| `min_samples` | integer | Effective per-measurement minimum. |
| `status` | string | `"pass"`, `"fail"`, or `"reported"` (no measurement declared a `target_yield`). `fail` wins over `pass`. |
| `measurement_count` | integer | `== len(measurements)`. |
| `measurements` | array\<object\> | One entry per analysed measurement, in input order. |
| `warnings` | array\<string\> | Run-level warnings (skipped measurements, no `target_yield` declared), followed by the core's own. |

### `measurements[]` entries

| Field | Type | Description |
| --- | --- | --- |
| `name`/`unit` | string / string \| null | Echoed from the input. |
| `n` | integer | Usable samples — the population every statistic is computed over. |
| `errored` | integer | Samples excluded because they had no usable value. `n + errored` is the total draw. |
| `limits` | object | The **merged** limits actually used (`min`/`max`/`target_yield`, each present only when set). |
| `source_corners` | array\<string\> | Originating corners the draw was pooled from. |
| `distribution` | object | See "Distribution fit" above. |
| `yield` | object | `empirical` (always present) and `normal` (`null` when the fit is degenerate) — see "Two yield estimates". |
| `capability` | object | See "Capability" above. |
| `sample_size` | object | See "Sample-size verdict" above. |
| `status` | string | `"pass"`, `"fail"`, or `"reported"`. |
| `warnings` | array\<string\> | Per-measurement warnings. |

## Why Rust

The statistics core is the third Rust component in klayout-tools (after
`native/mom/` and `native/congestion/`), following
[`docs/ARCHITECTURE.md`](../ARCHITECTURE.md)'s "Where Rust code lives"
convention. It is dependency-free numerics: `erfc` (Cody's rational
Chebyshev approximations), the inverse normal CDF (Acklam + a Halley
refinement), Lanczos log-gamma, and the regularized incomplete beta by
modified-Lentz continued fraction — the last of which is what makes the
*exact* Clopper-Pearson interval available at all, rather than only a
normal approximation. Each is a published, citable algorithm with its own
closed-form unit test, so the numbers are checkable rather than trusted.

Later epic phases (campaign orchestration, importance/Latin-hypercube
sampling for rare-event yield, sensitivity ranking) are the compute-bound
work this core is positioned for.

## Building the native extension

`klt yield` requires the `klt_yield_native` extension to be built once per
environment (it is not published as a prebuilt wheel). Building it needs a
Rust toolchain (`cargo`/`rustc`) — which is exactly why it is **optional**
rather than a hard dependency: every other `klt` verb stays installable with
no Rust toolchain in sight.

```bash
# From a repo checkout, with a Rust toolchain installed. `yield` is a PEP 735
# dependency group in the top-level pyproject.toml, resolved from native/yield/
# via [tool.uv.sources]; uv drives maturin to compile and install it.
uv sync --extra dev --group yield
uv run klt yield examples/yield/mc-samples.json --limits examples/yield/spec-limits.json
```

Equivalently, without uv:

```bash
pip install maturin   # or: uv tool install maturin
cd native/yield
maturin develop --release   # builds + installs into the active venv
```

> **Rebuilding after a Rust change**: the crate's version does not change when
> you edit its source, so `uv sync` will happily reuse the cached wheel and
> your edit will appear to have no effect. Force it with
> `uv sync --extra dev --group yield --reinstall-package klt-yield-native`, or
> use `maturin develop` (which always rebuilds).

If `klt_yield_native` cannot be imported, `klt yield` fails cleanly with exit
code `1` and a message pointing back to this section — never a bare
`ImportError` traceback.

### Running the Rust tests

```bash
cd native/yield
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
```

`cargo test` works from the same manifest as the maturin build because
`Cargo.toml` deliberately does **not** enable pyo3's `extension-module`
feature; `native/yield/pyproject.toml`'s `[tool.maturin] features` turns it on
for the wheel build only.

## Scope and limitations

Phase 1a's bar is "a defensible yield statement from a sample set", not the
whole epic:

- **One distribution family.** Normal only, with an honest normality verdict
  and a warning when it is rejected. Lognormal/skew fits and mixture models
  are follow-on work.
- **Pooled, not per corner.** A draw spanning several originating corners is
  pooled into one population, with a warning that this is an envelope rather
  than a sampled distribution. A per-corner breakdown (`klt sim`'s
  `by_corner`) is not yet reproduced here.
- **No campaign orchestration.** `klt yield` consumes a campaign; it does not
  drive one. Seed management, adaptive sampling, and importance/Latin-hypercube
  sampling for rare-event tails are epic #710 Phase 2.
- **No negative control, no analytic cross-check — yet.** Enforcing a seeded
  known-bad variant per campaign and checking the empirical result against a
  closed-form distribution where one exists is Phase 1b
  ([#817](https://github.com/2AMLogic/klayout-tools/issues/817)). This
  command's *own* numerics are checked against closed forms in
  `native/yield/`'s unit tests and `tests/test_yield.py`, but nothing here
  yet enforces that discipline on a caller's campaign.
- **No sensitivity ranking.** Which device mismatches drive the spread is
  Phase 3.

## Exit codes

| Exit code | Meaning |
| --- | --- |
| `0` | Analysis ran; every measurement with a declared `target_yield` met it at the stated confidence (or none declared one). |
| `1` | Failed to run: input file not found/unreadable, an unrecognised document shape, no measurement with spec limits, a sample set too small to carry a confidence interval, a degenerate `--confidence`, or the `klt_yield_native` extension is not installed. |
| `2` | Usage error (argparse) — missing/invalid arguments. |
| `3` | Analysis ran; at least one measurement's yield claim was not supported at the stated confidence. |

Exit `3` matches [`klt sim`](sim.md#exit-codes)'s "ran successfully, a limit
was missed" precedent rather than inventing a new number.

## See also

- [`docs/cli/sim.md`](sim.md#monte-carlo-sampling) — the Monte Carlo request
  and the report shape this command consumes directly.
- [#710](https://github.com/2AMLogic/klayout-tools/issues/710) — the parent
  statistical/yield epic (later phases: negative controls and analytic
  cross-checks, campaign orchestration + variance reduction, sensitivity and
  design centering).
- [`docs/design-evidence-tiers.md`](../design-evidence-tiers.md) — the T1
  statistical-row bar this command produces evidence for.
- [`docs/json-contract.md`](../json-contract.md) — the shared envelope,
  `schema_version` policy, and error shape.
