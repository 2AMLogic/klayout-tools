# `klt yield`

Turn a Monte Carlo **sample set** plus its **spec limits** into a yield
estimate — with a confidence interval, a distribution fit, Cpk/sigma-to-spec,
and a sample-size verdict. Phase 1a of the statistical/yield epic
([#710](https://github.com/2AMLogic/klayout-tools/issues/710)), delivered by
[#816](https://github.com/2AMLogic/klayout-tools/issues/816). Phase 1b
([#817](https://github.com/2AMLogic/klayout-tools/issues/817)) adds the two
self-checks described below: a **negative control** and an **analytic
cross-check**. Phase 2a ([#906](https://github.com/2AMLogic/klayout-tools/issues/906))
adds `klt yield-campaign`, a sibling command that **launches and manages the
MC campaign itself** — see "Campaign orchestration" below — rather than
requiring a pre-run sample set.

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
| `negative_control` | object | Optional; see "Negative control" below. |
| `analytic_cross_check` | object | Optional; see "Analytic cross-check" below. |
| `sampling` | object | Optional; see "Sampling strategies (variance reduction)" below. |

A `klt sim` Monte Carlo report accepts the same three fields on its own
`measurements[]` rollup entries (alongside `limits`) — a negative control, an
analytic model, or a sampling strategy is metadata about the measurement, not
a corner, so each is supplied the same way regardless of which input shape
carries the draw.

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

### Negative control

Phase 1b ([#817](https://github.com/2AMLogic/klayout-tools/issues/817)): a
yield statistic that has never been shown to detect a bad design is not
evidence, it is an assumption. `negative_control` supplies a **seeded,
known-bad variant's own samples** — the same distribution a caller would draw
if the deliberate defect (a forced offset, a mismatch seed pushed past spec,
a corner known to fail) were injected — and `klt yield` checks that it
actually shows up as degraded yield, not just a lower point estimate:

```json
"negative_control": {
  "samples": [1.32, 1.34, "..."],
  "errored": 0,
  "description": "vos forced to 0.6 V (12x the 0.05 V spec sigma)"
}
```

The negative control's own samples are analysed against the **same limits**
as the nominal measurement (its own `empirical`/`normal` yield estimate,
carrying its own confidence interval and `n` — the "never a bare point
estimate" rule applies here too). The verdict requires more than a lower
point estimate:

| `verdict` | Meaning |
| --- | --- |
| `detected` | The negative control's empirical yield is lower **and** its exact (Clopper-Pearson) confidence interval does not overlap the nominal measurement's — a difference too large to be sampling noise. |
| `not_detected` | The point estimates may differ, but the intervals overlap (or the control isn't even lower) — the deliberate defect did not show up as a statistically distinguishable degradation. |

`negative_control` is `null` when a measurement declares none. **A campaign
where no measurement declares a negative control at all — or whose negative
control is `not_detected` — is flagged with a run-level warning**, not
silently accepted; there is no exit-code change for this (see "Exit codes"
below), only the warning, so existing automation is not broken by adopting
this discipline.

### Analytic cross-check

Also Phase 1b: where a closed-form distribution exists, `klt yield` compares
the empirical draw against it directly, rather than trusting the Monte Carlo
result on its own. `analytic_cross_check` names one of two models:

| `kind` | Fields | Prediction |
| --- | --- | --- |
| `ktc_noise` | `capacitance_f` (required), `temperature_k` (default `300`), `analytic_mean` (default `0`) | Thermal (`kT/C`) sampled-capacitor noise: `sigma = sqrt(kB * T / C)`. |
| `mismatch_offset` | `sigma` (required), `mean` (default `0`) | A mismatch-dominated offset/spread with a known sigma the caller has already evaluated (e.g. a Pelgrom-model prediction), in the measurement's own units. |

```json
"analytic_cross_check": { "kind": "ktc_noise", "capacitance_f": 1e-12 }
```

The check compares the analytic mean and stddev against the measurement's
own empirical mean/stddev, each with a confidence interval built from the
same asymptotic approximation the `normal` yield estimator's delta method
already relies on (`Var(mu_hat) = sigma^2/n`, `Var(sigma_hat) =
sigma^2/(2n)`):

| Field | Description |
| --- | --- |
| `analytic_mean`/`analytic_stddev` | The model's prediction. |
| `empirical_mean`/`empirical_stddev` | The sample's own fit (same numbers `distribution` reports). |
| `empirical_mean_confidence_interval`/`empirical_stddev_confidence_interval` | Confidence intervals for the two empirical numbers above, at the report's own confidence level. |
| `stddev_relative_delta` | `(empirical_stddev - analytic_stddev) / analytic_stddev`. |
| `mean_delta` | `empirical_mean - analytic_mean`, in the measurement's own units. |
| `verdict` | `consistent` only when **both** the analytic mean and stddev fall inside their empirical confidence intervals; `inconsistent` otherwise (with a warning). |

`analytic_cross_check` is `null` when a measurement declares none — there is
no requirement that every measurement have a closed form to check against;
only that when one exists, it is checked rather than assumed.

### Sampling strategies (variance reduction)

Phase 2b of epic #710
([#907](https://github.com/2AMLogic/klayout-tools/issues/907)): plain random
Monte Carlo needs sample counts that scale badly for a rare-event (high-sigma)
yield question — brute-forcing a 5-6 sigma tail estimate is impractical for
most canaries. `sampling` declares which strategy a measurement's `samples`
were drawn with, as an alternative to Phase 1's plain random Monte Carlo (the
default when `sampling` is omitted, or stated explicitly as
`{"strategy": "plain_random"}`):

| `strategy` | Fields | What it buys |
| --- | --- | --- |
| `plain_random` | (none) | Phase 1's baseline — every sample is an independent, equally-weighted draw. |
| `latin_hypercube` | `replicates` (integer, >= 2) | Stratified coverage: `samples` is `replicates` consecutive equal-sized Latin-hypercube designs, in that order. Replication is what makes the design's own variance estimable (McKay, Conover & Beckman 1979) — a single unreplicated LHS draw's within-sample proportion has a genuinely *smaller* true variance than an iid draw the same size (the whole point of stratifying), so scoring it with the plain Clopper-Pearson formula (which assumes iid) would understate what the design earned. |
| `importance` | `weights` (array\<number\>, one positive finite weight per sample, same order as `samples`) | Rare-event / high-sigma estimation: `samples` are drawn from a biased proposal distribution concentrated in the failure region, and `weights[i]` is the importance weight (`f_target(x_i) / f_proposal(x_i)`) that reweights draw `i` back to the target distribution. |

```json
"sampling": { "strategy": "latin_hypercube", "replicates": 6 }
```

```json
"sampling": { "strategy": "importance", "weights": [0.87, 1.42, "..."] }
```

A non-`plain_random` strategy adds a **third**, strategy-aware yield estimate
alongside `yield.empirical`/`yield.normal` (the "Two yield estimates" table
above is unaffected — the plain estimates are computed exactly the same way
regardless of `sampling`):

| `sampling.strategy` | `yield.variance_reduced.method` | Point estimate | Interval |
| --- | --- | --- | --- |
| `latin_hypercube` | `lhs-replicated` | Mean of each replicate's own empirical pass fraction | Normal approximation from the **across-replicate** spread — `se = stddev(replicate proportions) / sqrt(replicates)` |
| `importance` | `importance-weighted` | Horvitz-Thompson estimator: `sum(w_i * pass_i) / sum(w_i)` | Normal-approximation delta-method interval on the ratio estimator (the standard sandwich/robust variance for a self-normalized importance-sampling estimator; Hesterberg 1995) |

`null` for `plain_random` — `yield.empirical` already is that estimate, so
there is nothing to add. Every `variance_reduced` estimate still carries its
own `confidence_interval` and `n` — the "never a bare point estimate" rule
applies here exactly as it does everywhere else in this payload.

Each measurement also always carries a `sampling` report (even for
`plain_random`, so the strategy is never ambiguous by its absence):

| Field | Type | Description |
| --- | --- | --- |
| `strategy` | string | `"plain_random"` / `"latin_hypercube"` / `"importance"`. |
| `replicates` | integer \| null | Echoed from the request; `null` unless `strategy == "latin_hypercube"`. |
| `effective_sample_size` | number \| null | Kish's effective sample size for an importance-weighted draw — `(sum w)^2 / sum(w^2)`, "how many equally-weighted samples this reweighted draw is worth". Equals `n` exactly when every weight is equal, and falls toward `1` as the weight mass concentrates on a few draws. `null` unless `strategy == "importance"`. A campaign whose weights are highly concentrated (`effective_sample_size` under 5% of `n`) is flagged with a warning — the reweighted estimate then has far less statistical power than `n` alone would suggest. |

**The sampling strategy's effect on the sample-size verdict is surfaced
too**, not just the point estimate: `sample_size.variance_reduced` (`null`
unless `yield.variance_reduced` exists) is the same *precision* verdict as
`sample_size.observed_ci_halfwidth`/`required_n`, recomputed from the
strategy-aware estimate instead of the plain empirical interval:

```json
"sample_size": {
  "...": "the Phase 1 fields, unchanged",
  "variance_reduced": {
    "observed_ci_halfwidth": 0.0021,
    "target_ci_halfwidth": 0.01,
    "verdict": "sufficient"
  }
}
```

**Validated, not just implemented**: `native/yield/src/estimate.rs`'s own
test suite includes a matched-sample-count comparison against a known
analytic distribution for each strategy — `latin_hypercube` reaches a
measurably tighter confidence interval than plain random Monte Carlo at the
*same* total sample count (so it needs fewer samples than plain MC for the
same CI width, not just a coincidentally narrower one), and `importance`
resolves a one-sided 4-sigma tail (`Phi(4) = 0.99996833...`, a fail rate
~3.17e-5 that a 5000-sample plain-MC run at the same budget essentially never
observes even once) correctly centered on the analytic answer, using **fewer**
total samples than that plain-MC run. Both tests build their own sample sets
with a small, dependency-free, deterministically-seeded PRNG (no external
`rand` crate, per this crate's "dependency-free numerics" convention) —
genuinely random draws, not the exact quantile-grid construction Phase 1's
own tests use, since a variance-reduction claim has to be checked against
sampling noise, not sidestepped around it.

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
  negative control: none declared
  warning: measurement 'vref': no sample fell outside the limits, so the empirical
    yield is bounded only from below -- "at least 98.7779% at 95% confidence,
    N = 300" is the honest statement, not "100% yield"
  warning: measurement 'vref': the observed pass rate clears the 0.99 target, but
    N = 300 is too small to *claim* it at 95% confidence (the lower bound is
    0.987779); 368 samples at this rate would support the claim

...

warnings:
  no measurement declared a negative_control -- this campaign has no seeded,
    known-bad variant demonstrating that the statistics above can actually
    detect a degraded design; see docs/cli/yield.md#negative-control
```

This example deliberately declares no `negative_control`, so `klt yield`
flags it — the point being made in "Negative control" above: an unflagged
campaign is not the same thing as a checked one.

**Read the verdict, not the point estimate.** Not one of the 300 `vref`
samples missed the window, and a tool that stopped there would report "100%
yield". What is actually supported at 95% confidence is *at least 98.78%* —
which does **not** clear the declared 99% target, so the run exits `3`. The
campaign is not wrong; it is 68 samples short of being able to make the claim,
and the report says exactly that. Running `sim-report.json` instead produces
the identical statistics — the point of consuming the MC record format
directly.

## Real canary evidence

Phase 1c of epic #710
([#818](https://github.com/2AMLogic/klayout-tools/issues/818)) ran `klt
yield` end to end against a real canary's existing Monte Carlo campaign,
rather than the synthetic data above, to prove the statistical T1-row
evidence this command produces is actually consumable by a signoff
aggregator — not just by its own worked example.

**Canary and campaign chosen**: `2AMLogic/gf180-sar-adc`'s
`sim/mc-cdac-mismatch/` — CDAC unit-capacitor mismatch → INL/DNL, against
the block's **ratified** spec row ("INL / DNL, < 1 LSB baseline / < 0.5 LSB
stretch, untrimmed, 3σ Monte Carlo mismatch"). This was the more complete of
the two candidate canaries' existing MC records at the time: `N = 20000` raw
per-trial samples (vs. the bandgap canaries' `N = 300`), a ratified spec
window to grade against (vs. `sky130-bandgap`'s still-DRAFT spec), and the
source experiment's own closed-form Pelgrom-law sigma prediction already on
record — a ready-made analytic cross-check input, not one this exercise had
to invent.

**What was real, what was new**: the `N = 20000` DNL/INL draw
(`sim/mc-cdac-mismatch/runs/20260801-093800-c033611/trials_n20000.csv`) is
the canary's own pre-existing, committed simulation output — reused as-is,
reformatted (not re-simulated) into `klt yield`'s sample-set shape. The one
genuinely new simulation is the negative control: the same experiment's own
`mc_cdac_mismatch.py` tool, re-run with the unit-cap mismatch sigma forced
to 3x the calibrated design value (`sigma_u = 2.211629342323457 %`,
`N = 2000`, seeded) — a real, re-runnable defect injection, not a synthetic
offset.

**Result** (`klt yield mc-samples.json --limits spec-limits.json --format
json`, run from the evidence bundle below):

| Measurement | Limits | Status | `sigma_to_spec` | Negative control | Analytic cross-check |
| --- | --- | --- | --- | --- | --- |
| `dnl_at_256_lsb_baseline` | ±1.0 LSB | pass | 5.975 | detected | consistent (Δ +0.37%) |
| `dnl_at_256_lsb_stretch` | ±0.5 LSB | **fail** | 2.986 | detected | consistent |
| `inl_at_256_lsb_baseline` | ±1.0 LSB | pass | 11.954 | **not_detected** | consistent (Δ +0.27%) |
| `inl_at_256_lsb_stretch` | ±0.5 LSB | pass | 5.975 | detected | consistent |

Two findings are worth calling out because they are exactly what "read the
verdict, not the point estimate" (above) means in practice on a real
campaign, not a synthetic one:

- `klt yield`'s independently-computed `sigma_to_spec` (5.975 DNL / 11.954
  INL, ±1 LSB baseline) agrees with the source experiment's own
  bespoke-script `sigma_at_spec` (5.978 / 11.957,
  `summary_n20000.json`) to 3 significant figures — an independent
  cross-check that neither tool's arithmetic is wrong, computed two
  different ways from the same raw draw.
- The negative control is `not_detected` on `inl_at_256_lsb_baseline`: a 3x
  sigma-scale defect is not big enough to push INL's yield measurably below
  1.0 against the *loose* ±1 LSB limit (it does show up against the tighter
  ±0.5 LSB stretch limit, on the same samples). This is the honest, expected
  behavior the negative-control mechanism is supposed to surface — a defect
  detector's power depends on the limit it is checked against, and a single
  campaign-wide "detected/not detected" summary would have hidden exactly
  this.

**Evidence bundle**
([2AMLogic/gf180-sar-adc#149](https://github.com/2AMLogic/gf180-sar-adc/pull/149),
record
[`20260812-132011-f613571.md`](https://github.com/2AMLogic/gf180-sar-adc/blob/main/sim/mc-cdac-mismatch/records/20260812-132011-f613571.md)):
`sim/mc-cdac-mismatch/yield-evidence/` carries `build_yield_evidence.py`
(the deterministic, re-runnable script that reformats the real CSVs into
`klt yield`'s input shape — it invents no numbers), `mc-samples.json`,
`spec-limits.json`, and the committed `klt-yield-report.json`/`.txt`
outputs, recorded in the canary's `sim/` directory per its own append-only
evidence convention (new record, nothing edited or superseded).

### Consumable by a signoff aggregator without bespoke parsing

`klt-yield-report.json` is a stock `klt yield --format json` envelope —
`klt signoff --manifest`'s Phase 2a binding
([`signoff.md`](signoff.md#tier-verdict-report---manifest), issue #870)
already classifies this exact shape by content (`measurement_count` + a
`source` object), so a block manifest can cite it with **zero
canary-specific parsing**, either command-backed:

```json
"6": {
  "command": ["klt", "yield", "mc-samples.json", "--limits", "spec-limits.json", "--format", "json"],
  "cwd": "sim/mc-cdac-mismatch/yield-evidence/"
}
```

or file-backed, to grade against the exact committed report without
re-running anything:

```json
"6": {"file": "sim/mc-cdac-mismatch/yield-evidence/klt-yield-report.json"}
```

### Checked against the T1 statistical-row bar

[`docs/design-evidence-tiers.md`](../design-evidence-tiers.md) item 6
("Statistical claims carry Monte Carlo evidence") requires a recorded seed,
sample count, a deterministic negative control, and results **combined
with (not instead of) process corners**. This evidence bundle satisfies the
first three plainly (seeds `20260801` nominal / `99109901` negative
control; `N = 20000` nominal / `N = 2000` negative control; a real, seeded,
detected-on-3-of-4-rows negative control) but is honest about the fourth:
it is the **mismatch leg only** — `sim/mc-cdac-mismatch/`'s own scope
statement excludes the PVT-corner axis by design (mismatch is not a
supply/temperature-corner phenomenon), and combining it with
`sim/adc-inl-dnl/`'s separate process-corner leg into one verdict is not
done here. Per the doc's own "Coverage honesty" rule, that gap is part of
the claim, not hidden by it: this bundle is complete, real, T1-row
statistical evidence for the mismatch leg of item 6, not yet a closed
item-6 claim for the full ratified spec row.

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
        },
        "variance_reduced": null
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
        "method": "clopper-pearson-zero-failures",
        "variance_reduced": null
      },
      "negative_control": null,
      "analytic_cross_check": null,
      "sampling": {
        "strategy": "plain_random",
        "replicates": null,
        "effective_sample_size": null
      },
      "status": "fail",
      "warnings": ["..."]
    }
  ],
  "warnings": ["..."]
}
```

`negative_control` is `null` above because the worked example declares
none — see "Negative control" for its populated shape
(`{"description", "n", "errored", "yield", "nominal_empirical_estimate",
"degradation_detected", "verdict"}`) and "Analytic cross-check" for
`analytic_cross_check`'s (`{"kind", "analytic_mean", "analytic_stddev",
"empirical_mean", "empirical_stddev",
"empirical_mean_confidence_interval", "empirical_stddev_confidence_interval",
"stddev_relative_delta", "mean_delta", "verdict"}`). `yield.variance_reduced`
and `sample_size.variance_reduced` are likewise `null` above because the
worked example declares no `sampling` strategy — see "Sampling strategies
(variance reduction)" for their populated shapes
(`{"method", "estimate", "confidence", "confidence_interval", "n"}` and
`{"observed_ci_halfwidth", "target_ci_halfwidth", "verdict"}` respectively).

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
| `warnings` | array\<string\> | Run-level warnings (skipped measurements, no `target_yield` declared, no measurement declared a `negative_control`, or one that did not detect degradation), followed by the core's own. |

### `measurements[]` entries

| Field | Type | Description |
| --- | --- | --- |
| `name`/`unit` | string / string \| null | Echoed from the input. |
| `n` | integer | Usable samples — the population every statistic is computed over. |
| `errored` | integer | Samples excluded because they had no usable value. `n + errored` is the total draw. |
| `limits` | object | The **merged** limits actually used (`min`/`max`/`target_yield`, each present only when set). |
| `source_corners` | array\<string\> | Originating corners the draw was pooled from. |
| `distribution` | object | See "Distribution fit" above. |
| `yield` | object | `empirical` (always present), `normal` (`null` when the fit is degenerate), and `variance_reduced` (`null` unless `sampling.strategy` is `latin_hypercube`/`importance`) — see "Two yield estimates" and "Sampling strategies (variance reduction)". |
| `capability` | object | See "Capability" above. |
| `sample_size` | object | See "Sample-size verdict" above; its own `variance_reduced` field is `null` unless `yield.variance_reduced` exists — see "Sampling strategies (variance reduction)". |
| `negative_control` | object \| null | `null` unless the input declared one — see "Negative control" above. |
| `analytic_cross_check` | object \| null | `null` unless the input declared one — see "Analytic cross-check" above. |
| `sampling` | object | Always present, even for the `plain_random` default — see "Sampling strategies (variance reduction)" above. |
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

Phase 2b ([#907](https://github.com/2AMLogic/klayout-tools/issues/907)) adds
the variance-reduction estimators described in "Sampling strategies (variance
reduction)" above — still dependency-free (no `rand` crate; the crate's own
test suite ships a small seeded PRNG, test-only, to validate them against
sampling noise rather than a noise-free quantile grid). Campaign
*orchestration* (Phase 2a, see "Campaign orchestration" below) is pure Python
glue over `klt sim`/`remote_fleet` and needs none of it. Sensitivity ranking
(Phase 3, [#923](https://github.com/2AMLogic/klayout-tools/issues/923)) has
shipped as [`klt yield-sensitivity`](yield-sensitivity.md), reusing this same
crate (standardized-regression/correlation numerics, still dependency-free).

## Building the native extension

`klt yield` requires the `klt_yield_native` extension to be built once per
environment (it is not published as a prebuilt wheel). Building it needs a
Rust toolchain (`cargo`/`rustc`) — which is exactly why it is **optional**
rather than a hard dependency: every other `klt` verb stays installable with
no Rust toolchain in sight.

> **Not reachable from a single-package install.** Neither `uv tool install
> klayout-tools`/`pip install klayout-tools` nor the git-pinned form (`uv
> tool install "klayout-tools @ git+https://github.com/2AMLogic/klayout-tools@<ref>"`)
> builds this extension — both install only the pure-Python package, with no
> `native/yield/` source tree or dependency group in scope. `klt yield`,
> `klt yield-campaign`, and `klt yield-sensitivity` all share this
> requirement (same crate). Getting the extension needs the **full repo
> checkout** shown below, not just an installed `klt`. `klt mom` and
> `klt synthesize --restructure-timing` have the same from-source gap
> for their own Rust extensions — see [`docs/cli/mom.md`](mom.md).

To build it:

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

## Campaign orchestration

Phase 1 above consumes an **already-run** MC sample set. `klt yield-campaign`
([#906](https://github.com/2AMLogic/klayout-tools/issues/906), Phase 2a of
epic #710) closes the loop on the input side: it launches the campaign
itself, then runs the exact Phase 1 pipeline above against the result --
unmodified.

```
klt yield-campaign <spec.json> [-o|--out-dir <dir>] [--backend <name>]
                   [--hosts <n>] [--seed <n>] [--confidence <c>]
                   [--target-ci-halfwidth <h>] [--min-samples <n>]
                   [--measurement <name>]... [--format text|json]
```

A distinct top-level verb, not a `klt yield` sub-subcommand -- mirrors `klt
gen`/`klt gen-compose`'s split. `klt yield`'s own single-positional-argument
shape (a sample-set/report path) is unchanged.

### Campaign spec

`<spec.json>` is a [`klt sim` request document](sim.md) with a **mandatory**
`monte_carlo` block -- a spec without one is just a `klt sim` request; run
`klt sim` directly instead. Every other field is exactly `klt sim`'s own
schema: `netlist`, `analysis`, `measurements[]` (with each measurement's
`limits.min`/`max`/`target_yield`, already a `klt sim` field -- yield's own
pass/fail vocabulary needs no new one here), `corners` (device/PVT ranges;
optional, defaults to a single nominal corner), `monte_carlo.vary`
(`"process"`/`"mismatch"`/`"both"` -- the device-mismatch axis), `backend`,
and `remote`. Three additional top-level fields are `klt yield`'s own
run-level defaults, mirroring a `klt yield --limits` spec file's
`confidence`/`target_ci_halfwidth`/`min_samples` (there is no separate
limits file in this flow, so they live on the campaign spec's own top
level):

```json
{
  "netlist": "amp.sp",
  "models": {"pdk": "sky130A", "lib": "libs.tech/ngspice/sky130.lib.spice"},
  "analysis": {"kind": "tran", "args": "1n 1u"},
  "measurements": [
    {
      "name": "vref",
      "spice": ".meas tran vref FIND v(out) AT=1u",
      "limits": {"min": 1.15, "max": 1.25, "target_yield": 0.99}
    }
  ],
  "monte_carlo": {"n": 300, "vary": "mismatch"},
  "confidence": 0.95,
  "backend": "local-parallel"
}
```

### Seed management

If `monte_carlo.seed` is omitted, one is derived deterministically from the
spec's own sampling-relevant content (netlist, analysis, measurements,
corners, `monte_carlo.n`/`vary` -- never `backend`/`remote`/`options`, which
affect *how* the campaign runs but never *what* is sampled). The exact same
spec file, re-run any number of times -- on one host or sharded across a
fleet -- reproduces the exact same sample set: this reuses `klt sim`'s own
Monte Carlo seed contract (`docs/cli/sim.md`'s "Monte Carlo sampling")
unchanged, deriving every per-sample seed from one base value. The resolved
seed and its source (`"cli"`/`"spec"`/`"derived"`) are always echoed in the
response's `campaign` block, so a derived seed is exactly as auditable as an
explicit one. `--seed` overrides the spec's own `monte_carlo.seed` (or its
derived default) for a caller that wants a specific value.

### Dispatch

`--backend`/`--hosts` override the spec's own `backend`/`remote.hosts`
fields, the same precedence rule `klt sim --backend`/`--hosts` use, and are
handed straight to `klt sim` -- this command adds no scheduling logic of its
own. That means the campaign's corner x Monte-Carlo-sample grid is sharded
exactly as any other `klt sim` sweep is (`docs/cli/sim.md`'s "Fleet
sharding"): `local`/`local-parallel` shard in-process across a worker pool,
and `backend: "remote"` with `hosts > 1` shards across a real, guarded
EC2 fleet (Epic #375's K-instance launch, fleet-level cost gate, vCPU quota
pre-check, and one-shard retry).

### Collection

The dispatched `klt sim` report -- already shaped exactly like any other
`klt sim` Monte Carlo report -- is written to `<out-dir>/sample-set.json`
and handed to the exact `klt yield` reader/pipeline described earlier in
this document, unmodified: the same `klt sim` report auto-detection, the
same distribution fit/CI/Cpk/negative-control/analytic-cross-check pipeline.
The response is Phase 1's own yield-report JSON (see "JSON schema" above)
with one added `campaign` block:

```json
{
  "campaign": {
    "spec": "spec.json",
    "seed": 2122464451,
    "seed_source": "derived",
    "requested_samples": 300,
    "vary": "mismatch",
    "backend": "local-parallel",
    "hosts": 4,
    "sim_status": "pass",
    "corner_count": 300,
    "sim_report_path": ".klt/yield-campaign/sample-set.json",
    "request_path": ".klt/yield-campaign/sim-request.json"
  }
}
```

`--out-dir` overrides where the dispatched `klt sim` request, its report,
and its artifacts are written (default: a `.klt/yield-campaign/` directory
next to the spec file). Exit codes match `klt yield`'s own (see "Exit
codes" below): `1` if the campaign never runs (a bad spec, a `klt sim`
dispatch failure -- bad netlist, a refused fleet cost/quota gate, ...), `3`
if it runs but a measurement's `target_yield` claim is not supported at the
stated confidence.

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
- **No adaptive sampling.** `klt yield-campaign`
  ([#906](https://github.com/2AMLogic/klayout-tools/issues/906), Phase 2a, see
  "Campaign orchestration" below) launches and manages a fixed-`n` Monte Carlo
  campaign directly from a spec, with deterministic seed derivation and
  dispatch across `klt sim`'s local/fleet backends; `sampling`
  ([#907](https://github.com/2AMLogic/klayout-tools/issues/907), Phase 2b) lets
  that same campaign draw its samples via a Latin-hypercube design or an
  importance-sampling proposal instead of plain random Monte Carlo, for a
  tighter interval or better rare-event-tail coverage at a matched sample
  count. Neither phase adjusts the sample count *during* a run based on
  interim results — a campaign's `n`/`replicates` are fixed for its whole
  duration.
- **The negative-control and analytic cross-check are opt-in, not
  mandatory.** Phase 1b ([#817](https://github.com/2AMLogic/klayout-tools/issues/817))
  makes `klt yield` *flag* a campaign that omits a negative control or whose
  negative control doesn't degrade as expected, and *report* an analytic
  discrepancy when a cross-check is supplied — but there is no flag or exit
  code that turns either into a hard failure. Neither block is required for
  a measurement to `pass`/`fail` its own `target_yield`.
- **No sensitivity ranking here.** Which device mismatches drive the spread
  is a separate command, [`klt yield-sensitivity`](yield-sensitivity.md)
  (Phase 3, issue [#923](https://github.com/2AMLogic/klayout-tools/issues/923)).

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
  and the report shape this command consumes directly, and
  [sim.md#fleet-sharding-remotehosts](sim.md#fleet-sharding-remotehosts) —
  the shard/merge engine `klt yield-campaign`'s dispatch reuses unchanged.
- [#710](https://github.com/2AMLogic/klayout-tools/issues/710) — the parent
  statistical/yield epic ([#817](https://github.com/2AMLogic/klayout-tools/issues/817)
  delivered the negative control and analytic cross-check above;
  [#907](https://github.com/2AMLogic/klayout-tools/issues/907) delivered the
  Latin-hypercube/importance sampling strategies above;
  [#906](https://github.com/2AMLogic/klayout-tools/issues/906) delivered
  campaign orchestration, "Campaign orchestration" above;
  [#923](https://github.com/2AMLogic/klayout-tools/issues/923) delivered
  sensitivity ranking, [`klt yield-sensitivity`](yield-sensitivity.md);
  [#924](https://github.com/2AMLogic/klayout-tools/issues/924) delivered
  design centering, [`klt design-centering`](design-centering.md)).
- [`docs/cli/yield-sensitivity.md`](yield-sensitivity.md) — Phase 3: which
  device/process parameters drive the spread.
- [`docs/cli/design-centering.md`](design-centering.md) — Phase 3: turning
  that ranking into re-centering candidates against a sized device.
- [`docs/design-evidence-tiers.md`](../design-evidence-tiers.md) — the T1
  statistical-row bar this command produces evidence for.
- [`docs/json-contract.md`](../json-contract.md) — the shared envelope,
  `schema_version` policy, and error shape.
