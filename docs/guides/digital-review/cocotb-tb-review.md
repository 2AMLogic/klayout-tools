# Testbench review guide: false-pass detection & coverage (cocotb)

> **Ported content, Apache-2.0.** Ported from
> [boldaxolotl/booley](https://github.com/boldaxolotl/booley)'s
> `src/booley/data/refs/code_review/testbench/cocotb-tb-review.md`
> ([Apache License 2.0](https://github.com/boldaxolotl/booley/blob/main/LICENSE)),
> commit [`1fdc706e`](https://github.com/boldaxolotl/booley/commit/1fdc706e2c54c3b9f80ee4a9472e2889a83d722f),
> fetched 2026-09-09 — see [`NOTICE`](NOTICE) for the full attribution
> record. Reworded from booley's own Flow/Target/Ticket/Specialist
> vocabulary and its machine-parsed findings-JSON schema into this repo's
> `klt` verbs, request/response fields, and plain PR-review-comment
> convention; the severity/confidence contract and the checklist content
> carry over unchanged. See [`README.md`](README.md) for when this guide
> applies and which `klt` verb (if any) supplies the tool evidence it asks
> a reviewer to check.

This guide applies to **cocotb (Python) testbenches** — the kind
`klt functional-verification` actually runs (see
[`docs/cli/functional-verification.md`](../../cli/functional-verification.md)):
a test module named by `request.testbench.module`, attached to
`request.hdl_toplevel` (the DUT itself, or a thin HDL wrapper instantiating
it when the DUT's ports are SystemVerilog interfaces — do **not** flag such
a wrapper as a defect), with the verdict derived entirely from the run's
`results_<engine>.xml` (echoed as `environment.results_xml` in the JSON
response). Your primary job is **false-pass detection** — tests that appear
green but don't actually verify correctness. Secondary: coverage gaps and
robustness. [`tb-review.md`](tb-review.md)'s SystemVerilog-specific rules
(sentinels, clocking blocks, `dut` instance naming) do not apply here; this
guide replaces them for a cocotb TB.

A canary's own `CLAUDE.md` may layer additional project-specific overlays
on top of this checklist; apply them when reviewing that canary's PRs.

## Inputs

Everything you need is the PR/issue and the TB sources:

- **Specification** — the PR/issue description, or the external spec file
  it points at. This is your source of truth for what the DUT is supposed
  to do. Read it before the checklist; skip spec-dependent checks if it is
  absent rather than guessing.
- **Documented assumptions** — decisions the RTL/TB author recorded for
  points the spec leaves open, when any exist.
- **Change classification** — if the PR describes itself as a narrow
  bugfix vs. a new feature vs. a refactor, that can affect the severity of
  the coverage-expansion checks below. Apply judgment; this repo has no
  formal classification field for this.
- The TB module(s) the PR changes, plus the helper packages they import.

**Do not read RTL source.** You are checking the testbench against the
spec, not against the implementation: a TB that agrees with buggy RTL is
exactly the false pass you exist to catch. Trace freely *within* the TB
and its Python helpers, but stop at the DUT boundary.

## Procedure

1. Read the specification, and any documented assumptions.
2. Read the target TB module(s) the PR changes (the `testbench.module`
   file plus any helper packages it imports).
3. Identify the DUT interface from `dut.<signal>` accesses, the spec, and
   any files explicitly in scope.
4. Identify: `@cocotb.test()` functions, golden-reference/model functions
   (and their domain), comparison logic, BFM usage, package dependencies.
5. Read [`cocotb-tb-style-guide.md`](cocotb-tb-style-guide.md).
6. Read any project-specific overlay the canary's own `CLAUDE.md` lists.
7. Review against the checklist below, applying the change-classification
   note above.
8. Report findings as normal PR review comments — one per finding, each
   citing `file:line` and tagged with the severity and confidence levels
   below.

## Severity and confidence

Same definitions as the RTL review guides — see
[`rtl-bugs.md`](rtl-bugs.md#severity-and-confidence). Prefer fewer,
higher-confidence findings — never CRITICAL/MAJOR with LOW confidence.

---

## Review checklist

### CRITICAL — false-pass risks (7 checks, must-fix)

| # | Check | Look for |
|---|-------|----------|
| 1 | **Filtered test name mismatch** | A `request.testbench.testcase` filter (or a canary's own CI test list) naming a test with no matching `@cocotb.test()` function in the module never runs and is silently absent from `tests[]` — compare any explicit testcase/test-list against the module's actual decorated functions. An unfiltered run (`testbench.testcase: null`) has no such risk, since every decorated function runs and is reported. |
| 2 | **Missing output comparison** | A test that drives stimulus but asserts nothing (or only asserts reset state); a test whose only assert is unreachable; `assert True`-shaped tautologies. |
| 3 | **Dead comparisons / non-independent expected** | Expected derived from the same DUT signal being checked (`assert dut.x.value == dut.x.value` shapes); expected computed by mirroring the RTL's own logic instead of the spec; comparison loop that iterates zero times. |
| 4 | **Sentinel prints instead of asserts** | Verdicts "reported" via `print`/`dut._log.info` (e.g. `PASSED`) with no raise. `klt functional-verification` derives `status`/`tests[]` entirely from `results.xml`'s own `<testcase>`/`<failure>` structure (see "Never trusting an exit code" in [`docs/cli/functional-verification.md`](../../cli/functional-verification.md)), so a printed sentinel is never read; every failure path must raise (assert). |
| 5 | **Swallowed exceptions** | `try/except` around checks that logs and continues; `except Exception: pass`; failures downgraded to warnings — the test returns normally and `results.xml` records a pass. |
| 6 | **Unresolved-value blindness** | Comparisons that coerce `X`/`Z` silently (e.g. `str()` compares, or `.integer` on a resolvable-only path) so an undriven DUT output "equals" the expected 0; no explicit decision on how unresolved bits should compare. |
| 7 | **Blocking sleeps / unbounded waits** | `time.sleep()`, blocking file/network I/O, or a bare `await` on a handshake that may never fire with no `with_timeout` — the run burns its whole wall-clock budget and every remaining test times out. |

### MAJOR — correctness/robustness (6 checks)

| # | Check | Look for |
|---|-------|----------|
| 8 | **Test-order coupling** | Tests share module-level mutable state, or depend on DUT state left by an earlier test (no reset/re-init in the test or shared `init()`); a batched `testbench.testcase` selection runs the selected set in ONE sim process — every test must own its bring-up. |
| 9 | **Missing timeout discipline** | Long-running loops with no bound; polling without a cycle cap; no `with_timeout` on externally-driven conditions. |
| 10 | **Hand-rolled BFMs where `cocotbext-*` exists** | A bespoke AXI/UART driver re-implementing what the widely-used `cocotbext-axi`/`cocotbext-uart` packages provide — more code to review, more false-pass surface. (SPI is the exception: as of this port there is no cocotb-2.x-compatible `cocotbext-spi` release — a vendored SPI BFM is expected.) |
| 11 | **No edge-case vectors** | Only random inputs, no deterministic boundary testing (0, max, near-overflow, identity, asymmetric). |
| 12 | **No randomized vectors** | Only hand-picked inputs; no `random`/numpy sweep to exercise the interior of the input space. Seed via `options.random_seed` (see "Reproducibility: `random_seed`" in [`docs/cli/functional-verification.md`](../../cli/functional-verification.md)) rather than a hardcoded seed that hides input-space diversity — the effective seed is always echoed back in `environment.random_seed`, so a specific failing run is reproducible either way. |
| 13 | **Insufficient stimulus diversity** | Fewer than 4 distinct input patterns, or only one operating scenario (one key for crypto, one coefficient set for a filter, one packet type for protocol). `options.coverage: true` (Verilator only) makes this measurable via `coverage.line_pct`/`toggle_pct`/`branch_pct`/`expr_pct` — see the index's evidence table. Minimum bar: ≥4 deterministic vectors spanning distinct input regions plus a randomized sweep of ≥8 iterations. |

### MINOR — style/coverage (3 checks, report only)

| # | Check | Look for |
|---|-------|----------|
| 14 | **Assertion messages lack context** | Bare `assert got == want` with no message naming the operand values, iteration, or scenario — the failure text is the only per-test evidence the JSON response's `tests[].error_message` field carries forward. |
| 15 | **Deep hierarchy pokes** | Tests reaching deep into `dut.<a>.<b>.<c>` internals instead of the port interface — couples the TB to implementation detail and breaks on refactor. |
| 16 | **Waveform writes from Python** | The TB opens VCD/trace files itself (e.g. via `pyvcd`) instead of relying on whichever mechanism owns the run's trace lifecycle — currently `options.coverage: true`'s Verilator `--trace` build arg (see "Coverage" in [`docs/cli/functional-verification.md`](../../cli/functional-verification.md)); a TB-authored dump can collide with it. |
