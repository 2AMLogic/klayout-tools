# `klt yield-campaign`

Launch and manage a Monte Carlo yield campaign directly from a spec, rather
than requiring a pre-run `klt sim` report — Phase 2a of the statistical/yield
epic ([#710](https://github.com/2AMLogic/klayout-tools/issues/710)),
delivered by [#906](https://github.com/2AMLogic/klayout-tools/issues/906).
`klt yield` (Phase 1) consumes an **already-run** MC sample set and answers
"does the design pass"; this command closes the loop on the input side: it
dispatches `klt sim`'s own Monte Carlo/corner sweep, then hands the result to
`klt yield`'s exact Phase 1 pipeline, unmodified.

```
klt yield-campaign <spec.json> [-o|--out-dir <dir>] [--backend <name>]
                    [--hosts <n>] [--seed <n>] [--confidence <c>]
                    [--target-ci-halfwidth <h>] [--min-samples <n>]
                    [--measurement <name>]... [--format text|json]
```

- `<spec.json>` — a **campaign spec**: a `klt sim` request document with a
  mandatory `monte_carlo` block. See "Campaign spec" below.
- `-o`/`--out-dir` — directory to write the dispatched `klt sim` request, its
  report, and its artifacts into (default: a `.klt/yield-campaign/`
  directory next to the spec file).
- `--backend` — execution backend for the campaign's `klt sim` dispatch
  (default: local, or the spec's own `backend` field); see
  [`docs/cli/sim.md`](sim.md).
- `--hosts` — shard the campaign's corner/Monte-Carlo-sample grid across this
  many hosts (default: 1, or the spec's own `remote.hosts` field); see
  [`docs/cli/sim.md`'s "Fleet sharding"](sim.md#fleet-sharding-remotehosts).
- `--seed` — override the campaign's Monte Carlo seed (default: the spec's
  own `monte_carlo.seed`, or one derived deterministically from the spec's
  own content).
- `--confidence` — two-sided confidence level for every reported interval
  (default `0.95`, or the spec's own `confidence` field).
- `--target-ci-halfwidth` — half-width, in absolute yield, the sample-size
  verdict is measured against (default `0.01`, or the spec's own
  `target_ci_halfwidth` field).
- `--min-samples` — minimum usable samples per measurement (default `2`, or
  the spec's own `min_samples` field).
- `--measurement` — restrict the analysis to this measurement. Repeatable,
  and comma-separated names are accepted; a name absent from the resulting
  report is an error, not a silent skip.
- `--format` — `text` (default) or `json`.

A distinct top-level verb, not a `klt yield` sub-subcommand — mirrors `klt
gen`/`klt gen-compose`'s split (see [`docs/cli/gen-compose.md`](gen-compose.md)'s
"CLI shape" note): `klt yield`'s existing single-positional-argument shape (a
sample-set/`klt sim`-report path) is unchanged; this command produces exactly
the same response shape from a campaign spec instead.

The command is headless and safe in CI, **except** that — like `klt yield` —
it requires the `klt_yield_native` Rust extension to be built and importable;
see [`docs/cli/yield.md`'s "Building the native
extension"](yield.md#building-the-native-extension) (same crate, same gap,
same fix).

## Campaign spec

`<spec.json>` is a [`klt sim` request document](sim.md) with a **mandatory**
`monte_carlo` block — a spec without one is just a `klt sim` request; run
`klt sim` directly instead. Every other field is exactly `klt sim`'s own
schema (`netlist`, `analysis`, `measurements[]` with each measurement's
`limits.min`/`max`/`target_yield`, `corners`, `monte_carlo.vary`, `backend`,
`remote`). Three additional top-level fields are `klt yield`'s own run-level
defaults — there is no separate `--limits` file in this flow, so they live on
the campaign spec's own top level:

| Field | Type | Description |
| --- | --- | --- |
| `confidence` | number | Same as `klt yield --confidence`; the spec's own default when `--confidence` is not given. |
| `target_ci_halfwidth` | number | Same as `klt yield --target-ci-halfwidth`. |
| `min_samples` | integer | Same as `klt yield --min-samples`. |

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

## Seed management

If `monte_carlo.seed` is omitted, one is derived deterministically from the
spec's own sampling-relevant content (netlist, analysis, measurements,
corners, `monte_carlo.n`/`vary` — never `backend`/`remote`/`options`, which
affect *how* the campaign runs but never *what* is sampled). The exact same
spec file, re-run any number of times, reproduces the exact same sample set —
this reuses `klt sim`'s own Monte Carlo seed contract
([`docs/cli/sim.md`'s "Monte Carlo sampling"](sim.md#monte-carlo-sampling))
unchanged. The resolved seed and its source (`"cli"`/`"spec"`/`"derived"`)
are always echoed in the response's `campaign` block (see "Response" below),
so a derived seed is exactly as auditable as an explicit one. `--seed`
overrides the spec's own `monte_carlo.seed` (or its derived default) for a
caller that wants a specific value.

## Dispatch

`--backend`/`--hosts` override the spec's own `backend`/`remote.hosts`
fields — the same precedence rule `klt sim --backend`/`--hosts` use — and are
handed straight to `klt sim`; this command adds no scheduling logic of its
own. The campaign's corner x Monte-Carlo-sample grid is sharded exactly as
any other `klt sim` sweep
([`docs/cli/sim.md`'s "Fleet sharding"](sim.md#fleet-sharding-remotehosts)):
`local`/`local-parallel` shard in-process across a worker pool, and
`backend: "remote"` with `hosts > 1` shards across a real, guarded EC2 fleet
(Epic #375's K-instance launch, fleet-level cost gate, vCPU quota pre-check,
and one-shard retry).

## Response

The dispatched `klt sim` report — already shaped exactly like any other `klt
sim` Monte Carlo report — is written to `<out-dir>/sample-set.json` and
handed to `klt yield`'s own reader/pipeline unmodified. The response is
Phase 1's own yield-report JSON (see [`docs/cli/yield.md`'s "JSON
schema"](yield.md#json-schema-the-contract)) with one added `campaign` block:

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

### `campaign` fields

| Field | Type | Description |
| --- | --- | --- |
| `spec` | string | Echo of the `<spec.json>` argument. |
| `seed` | integer | The resolved Monte Carlo seed actually used. |
| `seed_source` | string | `"cli"` (`--seed` given), `"spec"` (the spec's own `monte_carlo.seed`), or `"derived"` (computed from the spec's own content). |
| `requested_samples` | integer \| null | Echo of the spec's `monte_carlo.n`. |
| `vary` | string \| null | Echo of the spec's `monte_carlo.vary`. |
| `backend` | string | The resolved dispatch backend (`--backend`, else the spec's own `backend`, else `"local"`). |
| `hosts` | integer | The resolved host count (`--hosts`, else the spec's own `remote.hosts`, else `1`). |
| `sim_status` | string | The dispatched `klt sim` report's own top-level `status`. |
| `corner_count` | integer \| null | Echo of the dispatched `klt sim` report's `corner_count`. |
| `sim_report_path` | string | Path the dispatched `klt sim` report was written to. |
| `request_path` | string | Path the resolved `klt sim` request (the spec plus its resolved seed) was written to. |

Every other top-level field, and every `measurements[]` entry, is exactly
`klt yield`'s own contract — see [`docs/cli/yield.md`'s "Top-level
fields"](yield.md#top-level-fields) and
["`measurements[]` entries"](yield.md#measurements-entries).

## Errors

Anything that prevents the campaign from running end to end — a bad or
missing spec, a spec with no `monte_carlo` block, a `klt sim` dispatch
failure (bad netlist, a refused fleet cost/quota gate, ...), or a `klt yield`
analysis failure against the resulting sample set — is reported as this
command's own [`docs/json-contract.md`](../json-contract.md) error shape
(exit `1`), never a Python traceback. `--measurement` given but resolving to
an empty set is likewise an error.

## Exit codes

| Exit code | Meaning |
| --- | --- |
| `0` | The campaign ran and every measurement's declared `target_yield` was met (or none declared one). |
| `1` | The campaign could not be dispatched or analysed — see "Errors" above. |
| `2` | Usage error (argparse) — missing/invalid arguments. |
| `3` | The campaign ran; at least one measurement's yield claim was not met at the stated confidence. |

Exit codes mirror `klt yield`'s own — see
[`docs/cli/yield.md`'s "Exit codes"](yield.md#exit-codes) — including that
they are additive and a consumer should gate on the payload's own verdict,
not the exit code; see
[`docs/json-contract.md`](../json-contract.md#exit-codes)'s "Exit codes"
section.

## Building the native extension

Same crate, same requirement, same steps as `klt yield` — see
[`docs/cli/yield.md`'s "Building the native
extension"](yield.md#building-the-native-extension).

## Scope and limitations

- **No adaptive sampling.** This command launches and manages a fixed-`n`
  Monte Carlo campaign; it does not adjust the sample count during a run
  based on interim results. `sampling`
  ([#907](https://github.com/2AMLogic/klayout-tools/issues/907)) lets that
  same campaign draw its samples via a Latin-hypercube design or an
  importance-sampling proposal instead of plain random Monte Carlo, for a
  tighter interval or better rare-event-tail coverage at a matched sample
  count — see [`docs/cli/yield.md`'s "Sampling strategies (variance
  reduction)"](yield.md#sampling-strategies-variance-reduction).
- Every limitation of `klt yield`'s own Phase 1 pipeline — one distribution
  family, pooled-not-per-corner draws, opt-in negative control/analytic
  cross-check, no sensitivity ranking — applies unchanged here, since this
  command hands its dispatched sample set to that pipeline unmodified. See
  [`docs/cli/yield.md`'s "Scope and limitations"](yield.md#scope-and-limitations).

## See also

- [`docs/cli/yield.md`](yield.md) — Phase 1: the yield-estimation pipeline
  this command dispatches into unmodified, and its own "Campaign
  orchestration" section covering the same mechanics inline.
- [`docs/cli/sim.md`](sim.md) — the Monte Carlo request/response shape and
  fleet-sharding engine this command's dispatch reuses unchanged.
- [`docs/cli/yield-sensitivity.md`](yield-sensitivity.md) — Phase 3: which
  device/process parameters drive the spread in a completed campaign.
- [#710](https://github.com/2AMLogic/klayout-tools/issues/710) — the parent
  statistical/yield epic
  ([#906](https://github.com/2AMLogic/klayout-tools/issues/906) delivered
  this command).
