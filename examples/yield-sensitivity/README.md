# `examples/yield-sensitivity/`

A synthetic, seeded 200-sample campaign for `klt yield-sensitivity` — the
worked example [`docs/cli/yield-sensitivity.md`](../../docs/cli/yield-sensitivity.md)
quotes, and issue [#923](https://github.com/2AMLogic/klayout-tools/issues/923)'s
own acceptance-criterion validation case.

| File | What it is |
| --- | --- |
| `dominant-mismatch-samples.json` | The **sensitivity sample document**: one measurement, `offset_mv` (a differential-pair input-offset stand-in), with four per-sample mismatch-term draws. |
| `generate.py` | Regenerates it byte-identically (fixed seed). |

```bash
klt yield-sensitivity examples/yield-sensitivity/dominant-mismatch-samples.json
```

## Why this validates the ranking

`generate.py` builds `offset_mv` as a known linear combination of its four
parameter draws:

```
offset_mv = 10 * vth_mismatch_m1 + 1 * vth_mismatch_m2
          + 1 * beta_mismatch_m1 - 1 * beta_mismatch_m2 + noise
```

`vth_mismatch_m1`'s coefficient is **10x** every other term's — issue #923's
own acceptance criterion: "a campaign where one injected mismatch term is
scaled 10x the others — the ranking must correctly surface it first."
`tests/test_yield_sensitivity.py::test_worked_example_surfaces_the_known_dominant_parameter_first`
asserts exactly that against this committed fixture: `vth_mismatch_m1` ranks
`1` with a standardized coefficient an order of magnitude above every other
parameter's, and every corroborating metric (`pearson_r`, `spearman_rho`)
agrees.

## Why synthetic

The draws come from a fixed seed through Python's own `random.gauss`, so the
fixture regenerates byte-identically on any interpreter and the tests can
assert real ranking numbers against it — the same reasoning
`examples/yield/generate.py` uses. A worked example only needs to exercise
the contract shape (multiple parameters, a solvable standardized regression,
a clearly-dominant term) — no PDK or simulator dependency required.
