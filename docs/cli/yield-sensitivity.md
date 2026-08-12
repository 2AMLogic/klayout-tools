# `klt yield-sensitivity`

Rank a completed Monte Carlo campaign's device/process parameters by their
contribution to an output metric's variance — Phase 3 of the
statistical/yield epic ([#710](https://github.com/2AMLogic/klayout-tools/issues/710)),
delivered by [#923](https://github.com/2AMLogic/klayout-tools/issues/923).
`klt yield` (Phases 1a/1b) answers "does the design pass"; this command
answers a different question over the same kind of campaign data: **which
parameter draws actually moved the number**.

```
klt yield-sensitivity <samples> [--measurement <name>]... [--format text|json]
```

- `<samples>` — a **sensitivity sample document** (see "Input" below).
- `--measurement` — restrict the analysis to this measurement. Repeatable,
  and comma-separated names are accepted.
- `--format` — `text` (default) or `json`.

The command is headless and safe in CI, **except** that — like `klt yield`
— it requires the `klt_yield_native` Rust extension to be built and
importable (the same crate `klt yield` uses; see
[`docs/cli/yield.md#building-the-native-extension`](yield.md#building-the-native-extension)).

A distinct top-level verb, not a `klt yield` sub-subcommand: `klt yield
<samples>`'s positional/`--limits` shape is already shipped and load-bearing,
so a second analysis mode of campaign data lives at its own verb instead of
retrofitting subparsers onto it — the same split `klt yield-campaign`
([#906](https://github.com/2AMLogic/klayout-tools/issues/906)) already made,
and the same reasoning [`docs/cli/gen-compose.md`](gen-compose.md) gives for
`klt gen-compose` being its own verb rather than a `klt gen` subcommand.

## Input

### Sensitivity sample document

Unlike `klt yield`, this command does **not** read a `klt sim` Monte Carlo
report directly — see "Why not a `klt sim` report" below. It reads a
dedicated document pairing each sample's parameter draws with the resulting
output value:

```json
{
  "measurements": [
    {
      "name": "offset_mv",
      "unit": "mV",
      "samples": [
        {
          "parameters": { "vth_mismatch_m1": 0.808233, "vth_mismatch_m2": 0.280206 },
          "output": 10.295897
        },
        "..."
      ]
    }
  ]
}
```

| Field | Type | Description |
| --- | --- | --- |
| `name` | string | Measurement key, required. |
| `unit` | string | Optional, echoed back. |
| `samples` | array\<object\> | One entry per campaign sample, required and non-empty. |
| `samples[].parameters` | object\<string, number\> | Every sample's parameter draws, keyed by parameter name. **Every sample must declare the same key set** — a sample missing a parameter another sample carries is a data-quality error, not a silent default (see "Errors" below). |
| `samples[].output` | number | The resulting output-metric value for that sample. |

`--measurement` restricts analysis to named measurements the same way `klt
yield`'s own flag does; a name absent from the document is an error, not a
silent skip.

### Why not a `klt sim` report

Today's `klt sim` Monte Carlo response records each corner's RNG seeds
(`monte_carlo.{seed,process_seed,mismatch_seed}`,
[`docs/cli/sim.md`](sim.md#monte-carlo-sampling)), not the individual
per-device parameter values those seeds resolved to inside ngspice's
`AGAUSS`/`GAUSS` calls — so there is no per-sample parameter draw to read out
of a sim report yet — and `klt yield-campaign`
([#906](https://github.com/2AMLogic/klayout-tools/issues/906)) does not
change that; it dispatches `klt sim` and hands its report straight to `klt
yield` unmodified, so it inherits the same gap. Wiring `klt sim` to also
emit those draws is a separate, unscheduled follow-on; until then, the
caller (a `klt sim`/`klt yield-campaign`-driving harness, or a one-off
analysis script) is responsible for recording each sample's parameter
values alongside its output value in the document above.

## What is computed

**Correlation/regression-based, not variance-based.** A full Sobol/
variance-based decomposition would capture parameter interactions and
nonlinear response; this command deliberately does not implement one — issue
#923 scopes it out as a heavier, sample-hungrier method than a first pass
needs to justify. Instead:

- **Standardized regression coefficient** (primary, when solvable) — each
  parameter's coefficient in a multiple linear regression of the
  (z-scored) output on every (z-scored) parameter jointly, solved via the
  parameter-parameter correlation matrix (the closed form for a regression
  on standardized variables — Draper & Smith, *Applied Regression Analysis*,
  3rd ed., ch. 6). Unlike a bare pairwise correlation, this controls for
  correlation with the other parameters.
- **Pearson `r`** and **Spearman rank correlation** (`rho`) — reported for
  every ranked parameter alongside the primary metric, as single-parameter
  corroborating measures. `spearman_rho` also survives a monotonic-but-not-linear
  response the standardized coefficient would understate.
- **Fallback**: when the regression cannot be fit — fewer than
  `parameter_count + 2` usable samples, or a numerically singular
  parameter-correlation matrix (e.g. two perfectly collinear parameters) —
  ranking falls back to Pearson `r` alone, and
  `method.regression_solvable` is `false`. A warning states which condition
  triggered the fallback.
- A parameter that is **constant** across every sample is excluded from the
  ranking with a warning (there is nothing to correlate). A measurement
  whose **output** is constant across every sample is an error — there is no
  variance to attribute to anything.
- `ranking[]` is sorted **descending by `|contribution|`** (ties broken
  alphabetically by parameter name for a stable, checkable order).
  `contribution` mirrors `standardized_coefficient` when the regression is
  solvable, `pearson_r` otherwise — always present, so a consumer never has
  to branch on `method.primary` just to sort or threshold the ranking.
- A minimum of 4 usable samples per measurement is required; below that, a
  correlation is not so much noisy as undefined in spirit.

### Method limitations (always stated in the report)

`method.limitations` is not a footnote — it ships in every response,
because a ranking without its own caveats is exactly the kind of "looks like
evidence, cannot be checked" number epic #710 already refuses to emit for
`klt yield`'s own numbers:

- **Linear/monotonic effects only.** Neither interaction effects between
  parameters nor curvature in the response are captured — the Sobol
  decomposition this command deliberately does not implement.
- **Collinearity risk** (when the regression was solvable). Standardized
  coefficients can become unstable — large magnitude, unstable sign — under
  strong collinearity between parameters, even when each parameter is
  individually well-behaved; cross-check against `pearson_r`.
- **No collinearity control** (when the fallback applies). Pearson `r` alone
  does not control for correlation between parameters, so two correlated
  parameters can both rank highly even when only one actually drives the
  output.
- **Point estimates only.** No confidence interval on the ranking or its
  coefficients, unlike `klt yield`'s yield/Cpk numbers. A close 1st/2nd
  contribution gap can reorder under a different campaign seed — re-run with
  more samples before treating a narrow gap as decisive.

## Worked example

`examples/yield-sensitivity/` ships a synthetic, seeded 200-sample campaign
for a differential-pair input-offset stand-in (`offset_mv`), built from four
mismatch-term draws where one (`vth_mismatch_m1`) is injected with **10x**
the coefficient of the other three — issue #923's own acceptance-criterion
validation case.

```bash
klt yield-sensitivity examples/yield-sensitivity/dominant-mismatch-samples.json
```

```
samples: examples/yield-sensitivity/dominant-mismatch-samples.json
measurements: 1

offset_mv [mV]  N: 200  parameters: 4
  method: standardized_regression_coefficient  R^2: 0.9998
   1. vth_mismatch_m1                contribution=+0.9812  std_coef=+0.9812  pearson_r=+0.9877  spearman_rho=+0.9871
   2. vth_mismatch_m2                contribution=+0.0991  std_coef=+0.0991  pearson_r=+0.1872  spearman_rho=+0.1692
   3. beta_mismatch_m1               contribution=+0.0961  std_coef=+0.0961  pearson_r=+0.0032  spearman_rho=+0.0225
   4. beta_mismatch_m2               contribution=-0.0958  std_coef=-0.0958  pearson_r=-0.1243  spearman_rho=-0.1130
  limitation: linear/monotonic effects only -- ...
  limitation: standardized coefficients can become unstable ...
  limitation: point estimates only -- ...
```

`vth_mismatch_m1` ranks `1`, with a standardized coefficient (`0.9812`) an
order of magnitude above every other parameter's (`~0.10`, `~0.10`,
`~-0.10`) — exactly the outcome the 10x-scaled injected term should produce,
and exactly what `tests/test_yield_sensitivity.py` asserts against this
committed fixture. See [`examples/yield-sensitivity/README.md`](../../examples/yield-sensitivity/README.md)
for how the fixture is built.

## JSON schema (the contract)

```json
{
  "samples": "examples/yield-sensitivity/dominant-mismatch-samples.json",
  "schema_version": 1,
  "measurement_count": 1,
  "measurements": [
    {
      "name": "offset_mv",
      "unit": "mV",
      "n": 200,
      "parameter_count": 4,
      "method": {
        "primary": "standardized_regression_coefficient",
        "description": "...",
        "limitations": ["...", "...", "..."],
        "regression_solvable": true
      },
      "r_squared": 0.999814168232061,
      "ranking": [
        {
          "rank": 1,
          "parameter": "vth_mismatch_m1",
          "contribution": 0.9811638737134779,
          "standardized_coefficient": 0.9811638737134779,
          "pearson_r": 0.9876542725887228,
          "spearman_rho": 0.9871071776794421
        }
      ],
      "warnings": []
    }
  ],
  "warnings": []
}
```

### Top-level fields

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | Version of this command's JSON shape (starts at `1`; **independently versioned from `klt yield`'s own `schema_version`**, per [`docs/json-contract.md`](../json-contract.md)'s "per command, not globally"). |
| `samples` | string | Echo of the `<samples>` argument. |
| `measurement_count` | integer | `== len(measurements)`. |
| `measurements` | array\<object\> | One entry per analysed measurement, in input order. |
| `warnings` | array\<string\> | Run-level warnings (currently always empty; reserved for future use, additive). |

### `measurements[]` entries

| Field | Type | Description |
| --- | --- | --- |
| `name`/`unit` | string / string \| null | Echoed from the input. |
| `n` | integer | Usable samples analysed. |
| `parameter_count` | integer | Parameters actually ranked (a constant parameter is excluded — see `warnings`). |
| `method` | object | `primary` (`"standardized_regression_coefficient"` or `"pearson_correlation"`), `description`, `limitations` (array\<string\>, always non-empty), `regression_solvable` (boolean). |
| `r_squared` | number \| null | The standardized regression's `R^2` (`sum(coefficient * pearson_r)`), `null` when the regression was not solvable. |
| `ranking` | array\<object\> | Descending by `\|contribution\|` (ties broken alphabetically by `parameter`). See below. |
| `warnings` | array\<string\> | Per-measurement warnings — an excluded constant parameter, or why the regression fell back to `pearson_correlation`. |

### `ranking[]` entries

| Field | Type | Description |
| --- | --- | --- |
| `rank` | integer | 1-indexed position. |
| `parameter` | string | Parameter name, as declared in the input document. |
| `contribution` | number | The value `ranking` is sorted on — mirrors `standardized_coefficient` when non-null, `pearson_r` otherwise. Always present. |
| `standardized_coefficient` | number \| null | `null` when `method.regression_solvable` is `false`. |
| `pearson_r` | number | Linear correlation between this parameter and the output, taken alone. |
| `spearman_rho` | number | Rank (monotonic) correlation between this parameter and the output, taken alone. |

## Downstream consumers

`ranking[]`'s shape — a flat, name-keyed, numerically-sorted list with a
consistently-present `contribution` field — is designed to be consumed
without change by a design-centering loop that adjusts sizing based on which
parameters dominate the spread. [`klt design-centering`](design-centering.md)
(issue [#924](https://github.com/2AMLogic/klayout-tools/issues/924)) is that
consumer: it reads `ranking[].parameter` and `ranking[].contribution`
directly, exactly as this section originally reserved, and bridges them onto
a sized device's own geometry via a caller-supplied `parameter_map`. No
`#924`-specific field was added to this contract — see
[`docs/cli/design-centering.md`](design-centering.md)'s "The
mismatch-parameter vs. sizing-geometry key mismatch" section for how the two
commands' different naming conventions are reconciled outside this contract.

## Errors

| Condition | Result |
| --- | --- |
| `<samples>` not found / not JSON / no `measurements` array | error (exit `1`) |
| A measurement's `samples` array is empty, or has fewer than 4 usable entries | error |
| A measurement's `samples[].output` is not a finite number | error |
| A sample's `parameters` is missing, empty, or declares a different key set than sample 0 | error |
| A measurement's output is constant across every sample | error — no variance to attribute |
| Every parameter in a measurement is constant across every sample | error — nothing left to rank |
| `--measurement` names a measurement absent from the document | error |
| The `klt_yield_native` extension is not installed | error, pointing back to [`docs/cli/yield.md#building-the-native-extension`](yield.md#building-the-native-extension) |

None of the above ever surfaces a Python traceback — every error is the
documented `docs/json-contract.md` error shape.

## Exit codes

| Exit code | Meaning |
| --- | --- |
| `0` | Ranking ran to completion. |
| `1` | Failed to run — see "Errors" above. |
| `2` | Usage error (argparse) — missing/invalid arguments. |

Unlike `klt yield`, there is no `3`: a sensitivity ranking makes no
pass/fail claim to miss.

## Scope and limitations

- **Correlation/regression, not Sobol.** See "What is computed" above —
  this is a deliberate, stated simplification, not an oversight.
- **No `klt sim` integration yet.** See "Why not a `klt sim` report" above.
- **No confidence interval on the ranking itself.** Every number in the
  response is a point estimate; see "Method limitations" above.
- **Independent of #924 — now consumed by it.** This command ships the
  *producer* side of the ranking (issue #923); wiring it into a
  design-centering proposal is [`klt design-centering`](design-centering.md)'s
  job (issue #924, shipped as a reference consumer since #705's own
  analog-sizing engine has no design-centering stage of its own yet — see
  that command's docs for the current state of that hand-off).

## See also

- [`docs/cli/yield.md`](yield.md) — the sibling command this one's crate,
  extension-loading, and JSON-envelope conventions mirror.
- [`docs/cli/design-centering.md`](design-centering.md) — `klt
  design-centering` (issue #924), the reference consumer that wires this
  ranking into a re-centering proposal against a sized device's geometry.
- [#710](https://github.com/2AMLogic/klayout-tools/issues/710) — the parent
  statistical/yield epic.
