# `klt functional-verification`

Run a cocotb testbench against RTL sources through Icarus Verilog or
Verilator, and report a per-test pass/fail/skip breakdown plus optional
coverage — Phase 3 of
[Epic #391](https://github.com/2AMLogic/klayout-tools/issues/391) ("adopt the
digital engine class — Yosys + OpenROAD — RTL→GDS as a first-class `klt`
flow"), and the hard gate behind
[`klt eval`](eval.md)'s `valid` field (issue #387).

```
klt functional-verification <request> [--format text|json]
```

This is the build phase carried by two accepted Phase 1 spikes — read them
first; where this document and the code disagree with either, this document
(and the code) win:

- [`docs/design/cocotb-verification-spike.md`](../design/cocotb-verification-spike.md)
  (#398) — the engine survey, the invocation surface (cocotb 2.0's
  first-party Python `Runner` API, never a generated Makefile), the
  `results.xml`/coverage extraction recipes, and this request/response
  contract.
- [`docs/design/digital-flow-contracts-spike.md`](../design/digital-flow-contracts-spike.md)
  section 6 (#399) — the same contract restated alongside the
  synthesis/place-and-route ones, and the exit-code table.

Like `klt lvs`/`klt sim`/`klt synthesize`, this verb takes a **request
document** — RTL sources plus a testbench module plus engine/coverage
options is richer than a flag line carries cleanly — not positional file
args.

- `<request>` — a path to a request JSON file, `-` to read the request from
  stdin, or an inline JSON object string (the same three forms `klt lvs`
  accepts). Relative paths inside the request (`sources`, and the
  `testbench.module` file) resolve against the **request file's own
  directory**; for the stdin/inline forms, against the current working
  directory.
- `--format` — `text` (default, a human-readable summary) or `json`.

## Engines

`request.engine` selects the simulator: `"icarus"` (default) or
`"verilator"`. Both are invoked *through cocotb*, never directly — the
testbench is simulator-agnostic Python, which is the whole point of cocotb
as the harness layer (the spike ran the identical `test_gcd.py` against both
and got the same pass/fail structure).

| Engine | Why you'd pick it | Cost (spike's worked example) |
| --- | --- | --- |
| `icarus` | The CI-friendly default — an interpreter, so there is no native compile step. | ~0.7 s build + run |
| `verilator` | Required for coverage; a compiler, so it wins once a design runs enough cycles for per-cycle interpretation overhead to dominate. | ~8 s build + run |

The Verilator tax is fixed per invocation, so it **compounds** inside a
design-space-exploration loop (N candidates × that build cost) in a way it
does not for a single per-PR run — budget it accordingly.

### Requirements

- **cocotb** (`pip install cocotb`). It is deliberately *not* a `klt`
  dependency: cocotb 2.0 refuses to run on Python 3.14+, while this repo
  supports Python 3.10+ with no upper bound, so pinning it would break `klt`
  installs that never verify anything. A missing install is a clear,
  actionable error (exit 1), never a traceback — the same posture `klt
  synthesize` takes toward a missing `yosys` binary.
- **`iverilog`** or **`verilator`** (plus `verilator_coverage` for coverage
  runs) on `$PATH`, matching the requested engine.

## Never trusting an exit code

The spike observed the *same* deliberately-failing regression exit `2`, `1`,
and `0` through three different invocation paths — a `make`-wrapped run
reports Make's own convention, not the simulator's, and cocotb's own
`Runner.test()` may either `sys.exit()` or return normally on a run with
failing tests. So this verb:

- invokes cocotb's Python `Runner` API (`build()` → `test()`), never a
  generated Makefile;
- **discards** whatever exit code the simulator process produced;
- parses the run's own `results.xml` itself for every count and the final
  verdict — a `<testcase>` with no child is a pass, a `<failure>` child is a
  failure, a `<skipped>` child is a skip.

cocotb's `get_results()` helper is deliberately *not* used as the source of
truth: its `num_tests` counts skipped tests too, so it cannot produce the
passed/failed/skipped split this contract reports.

Two related consequences:

- **A run that produced no `results.xml` is exit 1, not a pass.** Verified
  live: a testbench module that fails to import leaves cocotb exiting `0`
  with no results file at all.
- **A regression that registered zero `@cocotb.test()` functions is exit 1,
  not a vacuous pass.** It verified nothing, and `status: "pass"` there
  would hand `klt eval` a `valid: true` for a design nothing ever checked.

## Artifacts

Everything a run produces is written to `.klt/functional-verification/` next
to the request file — the same "next to the input" default `klt sim`/`klt
synthesize` already use — and kept, never deleted:

| Artifact | What it is |
| --- | --- |
| `results_<engine>.xml` | The raw cocotb results file every count in the response is derived from (echoed as `environment.results_xml`). |
| `sim_build_<engine>/` | cocotb's build directory (the compiled model / `.vvp`). |
| `build_<engine>.log` / `test_<engine>.log` | The engine's own transcripts. These are captured to files rather than inherited, so simulator chatter can never corrupt `--format json`'s stdout. |
| `coverage.info` | lcov-format coverage, on coverage runs only (see below). |

## Coverage

`options.coverage: true` **requires `engine: "verilator"`** — Icarus has no
coverage path through this flow at all, so the combination is rejected with
exit 1 rather than silently ignored.

A coverage run adds Verilator's `--coverage --trace` build args, then
post-processes the emitted `coverage.dat` with two `verilator_coverage`
passes: `--write-info` (the portable lcov `.info` artifact the response
references **by path**, matching the "artifacts are paths, not inlined
blobs" rule `klt sim`'s waveforms and `klt extract`'s netlists already
follow) and `--report summary` (the line/toggle/branch/expr percentages).
Coverage requested but not producible (no `coverage.dat`, a
`verilator_coverage` failure) is exit 1 — never a silent `coverage: null`,
which would be indistinguishable from "not requested".

Functional coverage (`cocotb-coverage`-style bins defined in the testbench)
is out of scope for this contract; `coverage` is structural coverage only.

## Reproducibility: `random_seed`

cocotb's regression manager seeds its own `random` module per run and logs
the value it used in `results.xml`'s `<property name="random_seed">`
element (verified live, `docs/design/cocotb-verification-spike.md` section
4) — but does not accept one back in as a request-level input by default.
`options.random_seed` closes that gap end to end:

- **Request → `Runner.test()`.** `options.random_seed`, when given, is
  forwarded to `Runner.test()`'s own `seed` parameter, which sets
  `COCOTB_RANDOM_SEED` in the simulator subprocess's environment — a pinned
  seed reproduces cocotb's own seeded `random` module state run-to-run, not
  merely the same *logged* value after the fact.
- **`Runner.test()` → response.** Whether pinned or left to cocotb's own
  generator, the *effective* seed is always echoed back in
  `environment.random_seed`, read from `results.xml`'s own `<property>`
  element — the same artifact-derived-truth discipline every other count in
  this contract already follows. An unpinned run still gets a
  randomly-generated seed this way, worth capturing to reproduce that
  *specific* run later by feeding it back in as `options.random_seed`.

This is the same reproducibility bar `klt sim`'s Monte Carlo seeding and
`klt lvs`'s `environment` hashes already set (issue #423) — a stored CI
result's `environment.random_seed` is enough to reproduce it exactly.

## Request

```json
{
  "schema": "klt.functional_verification.request/1",
  "engine": "icarus",
  "sources": ["gcd.v"],
  "hdl_toplevel": "gcd",
  "testbench": { "module": "test_gcd", "testcase": null },
  "options": {
    "coverage": false,
    "timescale": ["1ns", "1ps"],
    "random_seed": 1785780800
  }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema` | string | Request contract identifier + major version. Not validated — user-authored input, never emitted by this tool. |
| `engine` | string | `"icarus"` (default) or `"verilator"`. An unsupported value is an application error (exit 1). |
| `sources` | array\<string\> | RTL source file paths, resolved relative to the request. Required, non-empty. May point at original RTL **or** at `klt synthesize`'s `netlist_path` — a gate-level equivalence re-check against the same testbench needs no contract change, only a different `sources` value. |
| `hdl_toplevel` | string | The DUT module name. Required. |
| `testbench.module` | string | The Python test module **name** (`"test_gcd"`, not `"test_gcd.py"`), resolved as `<request dir>/<module>.py`. Required — this verb does not synthesize testbenches; the module is human- or generator-authored. |
| `testbench.testcase` | string \| array\<string\> \| null | Optional testcase-name filter; `null`/omitted runs every `@cocotb.test()` in the module. Filtered-out tests still appear in the report as `skipped`. |
| `options.coverage` | boolean | Defaults to `false`. `true` requires `engine: "verilator"` (see "Coverage"). |
| `options.timescale` | `[string, string]` | `[unit, precision]`, defaulting to `["1ns", "1ps"]`. Passed to **both** the build and test steps — Icarus elaboration otherwise fails the moment a testbench's `Clock(..., unit="ns")` meets an unset (default 1 s) simulator precision. |
| `options.random_seed` | integer \| null | Optional. Pinned to `Runner.test()`'s own `seed` parameter (`COCOTB_RANDOM_SEED`) when given; omitted/`null` lets cocotb generate its own. Either way the seed actually used is echoed in `environment.random_seed` (see "Reproducibility: `random_seed`"). |

## Response

```json
{
  "schema_version": 1,
  "engine": "icarus",
  "hdl_toplevel": "gcd",
  "testbench": "test_gcd",
  "status": "fail",
  "test_count": 3,
  "passed_count": 2,
  "failed_count": 1,
  "skipped_count": 0,
  "tests": [
    { "name": "test_gcd_known_pairs", "status": "passed", "sim_time_ns": 520.0, "real_time_s": 0.0051 },
    { "name": "test_gcd_random_pairs", "status": "passed", "sim_time_ns": 4720.0, "real_time_s": 0.0471 },
    {
      "name": "test_gcd_deliberately_wrong_expectation",
      "status": "failed",
      "sim_time_ns": 130.0,
      "real_time_s": 0.003,
      "error_type": "AssertionError",
      "error_message": "gcd(48, 18): got 6, want 999 (deliberate failure)"
    }
  ],
  "coverage": null,
  "environment": {
    "engine": "icarus",
    "engine_version": "13.0",
    "cocotb_version": "2.0.1",
    "results_xml": "/abs/path/.klt/functional-verification/results_icarus.xml",
    "random_seed": 1785780800
  }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | Per-command version, per [`docs/json-contract.md`](../json-contract.md). |
| `engine` | string | Echo of the request's engine. |
| `hdl_toplevel` / `testbench` | string | Echo of the request's DUT / testbench-module identifiers. |
| `status` | string | `"pass"` (`failed_count == 0`) or `"fail"` (`failed_count > 0`). Never `"error"` in-band — a run that failed to *run* emits no envelope at all (see "Exit codes"). |
| `test_count` / `passed_count` / `failed_count` / `skipped_count` | integer | Derived from `results.xml`'s own `<testcase>`/`<failure>`/`<skipped>` structure. `test_count` includes skipped tests, so `passed + failed + skipped == test_count`. |
| `tests` | array\<object\> | One entry per `@cocotb.test()`, in the order cocotb ran them. `status` is `"passed"`/`"failed"`/`"skipped"`; `sim_time_ns`/`real_time_s` are `null` when the simulator did not report them. `error_type`/`error_message` are present **only** on `"failed"` entries, taken verbatim from the `<failure>` element's attributes. |
| `coverage` | object \| null | `null` unless `options.coverage: true`; otherwise `line_pct`/`toggle_pct`/`branch_pct`/`expr_pct` (numbers, or `null` for a category `verilator_coverage` did not report) plus `info_path`, an absolute path to the lcov `.info` artifact. |
| `environment` | object | Reproducibility block: `engine`, `engine_version` (the simulator's own version token, `null` if unresolvable), `cocotb_version`, `results_xml` — the absolute path to the raw evidence this report was derived from, so a stored verdict can be re-checked against it — and `random_seed` (the effective seed cocotb used, `null` only if `results.xml` lacked the property; see "Reproducibility: `random_seed`"). |

There is no shared `provenance` block: this verb's verdict depends on no PDK
and no rule deck (see `docs/json-contract.md` → "Shared `provenance`
block"), and `environment` is the contract's own reproducibility surface.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Every test passed (`status: "pass"`). |
| `1` | Failed to run — bad request, unresolvable RTL source or testbench module, coverage requested on an engine that has none, missing cocotb/simulator install, build or elaboration error, simulator crash, no `results.xml` produced, or a regression that registered zero tests. |
| `2` | Usage error (missing argument, bad `--format` value) — from argparse. |
| `3` | Ran successfully; at least one test failed (`status: "fail"`). |

This is the same `0`/`1`/`2`/`3` trichotomy [`klt lvs`](lvs.md) and
[`klt drc`](drc.md) use — **not** `klt sim`'s four-way split. A cocotb
regression has no analogue of "this corner's simulator errored but the rest
of the batch is still trustworthy": either the build+run pipeline produced a
`results.xml` to report from, or it did not.

## Composing into `klt eval`

`status: "pass"` → exit `0` → `valid: true`; `status: "fail"` → exit `3` →
`valid: false`. That is exactly [`klt eval`](eval.md)'s existing convention
(issue #387), so a gate needs no adaptation:

```json
{
  "gates": [
    { "check": "functional-verification",
      "args": { "request": "verify.json" } }
  ],
  "objective": { "check": "synthesize", "metric": "area_um2",
                 "polarity": "minimize", "args": { "request": "synth.json" } }
}
```

A partial result (2 of 3 tests passing) still collapses to one boolean at
the gate: `klt eval` reports that gate as `status: "fail"`, contributing one
`false` to `valid`, with the per-test detail available in this verb's own
`tests[]` for whoever wants to know *which* test failed. A run that never
produced evidence surfaces as `klt eval`'s own exit 1 — an optimizer must
never read "crashed" as "scored badly".

## Worked example

[`examples/functional-verification/`](../../examples/functional-verification)
holds the GCD worked example from the spike verbatim — an iterative-subtractor
GCD core (`gcd.v`), a cocotb testbench with three tests, one of them
deliberately failing (`test_gcd.py`), and two requests (Icarus; Verilator
with coverage):

```console
$ klt functional-verification examples/functional-verification/request.json
engine: icarus 13.0
hdl_toplevel: gcd
testbench: test_gcd
status: fail
tests: 3  passed: 2  failed: 1  skipped: 0

[passed] test_gcd_known_pairs  520.0 ns
[passed] test_gcd_random_pairs  4720.0 ns
[failed] test_gcd_deliberately_wrong_expectation  130.0 ns
    AssertionError: gcd(48, 18): got 6, want 999 (deliberate failure)

results_xml: .../.klt/functional-verification/results_icarus.xml
random_seed: 1785780800
$ echo $?
3
```

The identical testbench under `engine: "verilator"` reproduces the same
`TESTS=3 PASS=2 FAIL=1 SKIP=0` structure and adds coverage:

```console
$ klt functional-verification examples/functional-verification/request-verilator-coverage.json
...
coverage:
  line_pct: 100.0
  toggle_pct: 60.0
  branch_pct: 100.0
  expr_pct: 100.0
  info_path: .../.klt/functional-verification/coverage.info
```

Toggle coverage at 60% is correct, not a defect: the random test only drives
values up to 500 through a 16-bit datapath, so many high-order bits never
toggle — a real signal a designer (or an agent widening stimulus ranges)
would act on.

## Out of scope

- **Testbench generation.** `testbench.module` is an input. This verb never
  synthesizes Verilog or Python; who authors testbenches (hand-written per
  block, or eventually generator-assisted the way `klt gen` assists layout)
  is a separate question.
- **Functional coverage.** Structural (line/toggle/branch/expr) coverage
  only — see "Coverage".
- **Waveform inspection / interactive debug.** Batch pass-fail + coverage is
  the contract; `--trace` is enabled on coverage builds as a side effect of
  Verilator's coverage recipe, but no waveform artifact is contracted.
- **Commercial simulators.** cocotb supports several; open-tooling posture
  keeps them out (the same reasoning that excludes Calibre/HSPICE-class
  tools from the LVS and SPICE contracts).
