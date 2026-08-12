# `examples/yield/`

A synthetic, seeded 300-sample Monte Carlo campaign for `klt yield` — the
worked example [`docs/cli/yield.md`](../../docs/cli/yield.md) quotes.

| File | What it is |
| --- | --- |
| `mc-samples.json` | The **sample-set document**: two measurements, `vref` (a bandgap-shaped reference, two-sided 1.15–1.25 V window) and `iq_ua` (quiescent current, one-sided 10 µA max). |
| `sim-report.json` | The **same draw** as a trimmed `klt sim --format json` Monte Carlo report — the record format the canary MC harnesses already produce, which `klt yield` consumes with no intermediate format. |
| `spec-limits.json` | The spec limits, with `target_yield: 0.99` on both measurements. |
| `generate.py` | Regenerates all three, byte-identically (fixed seed). |

```bash
klt yield examples/yield/mc-samples.json --limits examples/yield/spec-limits.json
klt yield examples/yield/sim-report.json --limits examples/yield/spec-limits.json
```

Both produce identical statistics — that equivalence is asserted by
`tests/test_yield.py::test_sim_report_and_sample_set_agree_on_the_same_draw`.

## Why the example "fails"

It exits `3`, and that is the point. Not one of the 300 `vref` samples missed
the window, so a tool that stopped at the point estimate would report "100%
yield". What 300 clean samples actually support at 95% confidence is *at
least 98.78%* — which does not clear the declared 99% target. The report says
so explicitly, and tells you the campaign is 68 samples short of being able
to make the claim (`required_n_for_target: 368`).

That is epic [#710](https://github.com/2AMLogic/klayout-tools/issues/710)'s
discipline in one run: a yield number never ships without its confidence
interval and its sample count, and the tool refuses to let a point estimate
stand in for a claim.

## Why synthetic

The samples come from a fixed seed through Python's own `random.gauss`, so
the fixtures regenerate byte-identically on any interpreter and the tests can
assert real numbers against them. A worked example only needs to exercise the
contract shape (two-sided and one-sided limits, a declared `target_yield`,
both input document shapes) — no PDK or simulator dependency required, the
same reasoning `examples/sim/generate.py` uses for its synthetic testbench.
