# Testbench review guide: false-pass detection & coverage (SystemVerilog)

> **Ported content, Apache-2.0.** Ported from
> [boldaxolotl/booley](https://github.com/boldaxolotl/booley)'s
> `src/booley/data/refs/code_review/testbench/tb-review.md`
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

This guide applies to hand-written **SystemVerilog** testbenches driven by
printed pass/fail sentinels rather than cocotb — not the cocotb (Python)
testbenches `klt functional-verification` actually runs (see
[`docs/cli/functional-verification.md`](../../cli/functional-verification.md)).
For a cocotb testbench, use [`cocotb-tb-review.md`](cocotb-tb-review.md)
instead; that guide's opening note is the reciprocal of this one. This
guide remains useful for any legacy or hand-invoked SV testbench a canary
still carries, and the false-pass patterns below (dead comparisons,
missing timeouts, insufficient stimulus diversity) generalize to any
testbench language.

Your primary job is **false-pass detection** — finding tests that appear
green but don't actually verify correctness. Secondary: coverage gaps and
robustness.

A canary's own `CLAUDE.md` may layer additional project-specific overlays
on top of this checklist; apply them when reviewing that canary's PRs.

## Inputs

Everything you need is the PR/issue and the TB sources:

- **Specification** — the PR/issue description, or the external spec file
  it points at. This is your source of truth for what the DUT is supposed
  to do: the numbers checks 9 and 18 look for, the modes check 5 looks
  for, the domain check 4 looks for. Read it before the checklist.
- **Documented assumptions** — decisions the RTL/TB author recorded for
  points the spec leaves open, when any exist.
- **Change classification** — if the PR describes itself as a narrow
  bugfix vs. a new feature vs. a refactor, that can affect the severity of
  the coverage-expansion checks below (a one-line bugfix PR is not
  expected to add net-new edge-case vectors the way a new module would).
  Apply judgment; this repo has no formal classification field for this.
- **Simulation contract** — if the canary declares custom pass/fail
  sentinel wording (e.g. in its own README or `CLAUDE.md`) instead of the
  `[SIM_RESULT] PASSED`/`[SIM_RESULT] FAILED` default below, that
  configured wording overrides the generic default; do not flag a
  testbench merely for differing from the default when a documented
  custom contract exists.
- **Diff boundary** — when the PR/issue restricts review to a specific
  line range (a partial-increment PR against a larger family/epic issue,
  using `Part of #N`), only its added/modified lines may anchor a finding;
  unchanged baseline code is context, not a finding target.
- The TB file(s) the PR changes, plus the TB's own packages and helpers.

If the spec is absent, skip the checks that depend on it rather than
guessing — say nothing rather than inventing a requirement.

**Do not read RTL source.** You are checking the testbench against the
spec, not against the implementation: a TB that agrees with buggy RTL is
exactly the false pass you exist to catch. Trace signals *within* the TB
and its packages freely, but stop at the DUT boundary. Identify the DUT
instance and its interface from the TB's own connection list and the
spec.

## Procedure

1. Read the specification, and any documented assumptions.
2. Read the target TB file(s) the PR changes.
3. Identify the DUT instance and interface from the TB connections, the
   spec, and any files explicitly in scope.
4. Identify: test tasks, golden ref functions (and their domain), config
   coverage, comparison logic, package dependencies.
5. Read [`tb-style-guide.md`](tb-style-guide.md).
6. Read any project-specific overlay the canary's own `CLAUDE.md` lists.
7. Review against the checklist below, applying the change-classification,
   simulation-contract, and diff-boundary notes above.
8. Report findings as normal PR review comments — one per finding, each
   citing `file:line` and tagged with the severity and confidence levels
   below.

## Severity and confidence

Same definitions as the RTL review guides — see
[`rtl-bugs.md`](rtl-bugs.md#severity-and-confidence). Prefer fewer,
higher-confidence findings — never CRITICAL/MAJOR with LOW confidence.

---

## Review checklist

### CRITICAL — false-pass risks (8 checks, must-fix)

| # | Check | Look for |
|---|-------|----------|
| 1 | **Missing sim sentinel** | TB never prints the pass/fail sentinel named by the canary's own simulation contract, or `[SIM_RESULT] PASSED` / `[SIM_RESULT] FAILED` when no custom contract exists. Configured custom wording is fully supported and must not be reported merely for differing from the default. |
| 2 | **Missing output comparison** | DUT output signals never in `if (actual != expected)`; `run_test` increments `total_passed` unconditionally or on `!timed_out` alone. |
| 3 | **Dead comparisons** | Expected derived from same signal as actual; expected array never populated; comparison in unreachable branch; loop iterates zero times. |
| 4 | **Wrong golden ref** | Golden reference function called with wrong arguments, wrong domain, or missing post-processing (e.g., domain reduction). |
| 5 | **Incomplete config coverage** | Missing `ifdef` for a config the DUT supports; hybrid/multi-mode never exercises all modes; no protection-variant tests when DUT supports it. |
| 6 | **Off-by-one iteration** | `< N-1` instead of `< N`; wrong array element count for current config; ceiling division error. |
| 7 | **Uncontracted `$dumpfile`/`$dumpvars`** | TB contains `$dumpfile(...)` or `$dumpvars(...)` calls but the canary does not document these as project-owned trace artifacts. Hand-authored dump calls can collide with whatever trace/waveform mechanism the flow that runs this testbench owns (a future `klt wave` build, per `docs/design/waveform-query-contract-spike.md`, epic #1585 — or a cocotb `--trace` run's own dump path when this TB is invoked through a cocotb wrapper) — flag unless the canary's own docs establish this as deliberate policy. |
| 8 | **Multiple DUT instances directly under TB top** | TB instantiates the DUT module more than once directly under the TB top (e.g. `dut_aff0` and `dut_aff1` side-by-side, or `dut #(.MODE(0)) inst0(...); dut #(.MODE(1)) inst1(...)`). [`tb-style-guide.md`](tb-style-guide.md) §12 expects exactly one DUT instance per testbench — hierarchy-based coverage/trace tooling assumes a single scope. **Fix:** wrap the instances in a single `dut_wrap` module declared inside the TB file and instantiate `dut_wrap` once under the TB top. Example: `module dut_wrap(...); my_dut #(.MODE(0)) inst0(...); my_dut #(.MODE(1)) inst1(...); endmodule` — then `tb_top` instantiates `dut_wrap dut(.*)`. Sub-instances *inside* the DUT (or inside the wrapper) are fine; the rule applies only to what sits directly under the TB top. |

### MAJOR — correctness/robustness (10 checks)

| # | Check | Look for |
|---|-------|----------|
| 9 | **Expected model is not independent** | Golden-reference function or expected-value logic derives expected outputs from the same implementation assumptions as the stimulus/check path instead of from the spec. A shared mistake in encoding tables, state transitions, or field packing passes both — the TB is testing internal consistency, not correctness against the spec. Look for: expected values built from actual outputs, shared helper logic between stimulus and expected paths, duplicated packing assumptions without literal sentinel vectors, or expected values that change when the TB plumbing changes. |
| 10 | **Timeout missing/ineffective** | No `fork/join_any/disable fork`; timeout >1M cycles; test reports PASS after timeout (missing `timed_out` guard). |
| 11 | **Randomization domain** | Values outside legal range; off-by-one on range max; missing rejection sampling for large ranges. |
| 12 | **Error counter ignored** | `error_cnt` incremented but never used in PASS/FAIL verdict. |
| 13 | **No edge-case vectors** | Only random inputs, no deterministic boundary testing (0, max, near-overflow, identity, asymmetric). Every TB must include explicit edge-case vectors — random alone cannot reliably hit spec-critical boundaries. |
| 14 | **No randomized test vectors** | Only deterministic/hand-picked inputs, no `$urandom` or equivalent. Every TB must include randomized vectors to exercise the interior of the input space and catch unexpected interactions that fixed vectors miss. |
| 15 | **Insufficient stimulus diversity** | Fewer than 4 distinct input patterns, or only one operating scenario (one key for crypto, one coefficient set for a filter, one packet type for protocol). `klt functional-verification`'s own `options.coverage` (Verilator-only, see the index's evidence table) makes this measurable: a run with `coverage.line_pct`/`toggle_pct`/`branch_pct`/`expr_pct` all high despite only 1-3 vectors is exactly the false-positive this check exists to catch. Minimum bar: ≥4 deterministic vectors spanning distinct input regions plus a randomized sweep of ≥8 iterations. |
| 16 | **No reset-mid-operation test** | DUT has multi-cycle or stateful behavior but reset is never asserted while a transaction is in-flight. Look for: reset only in the initial block or only between completed test tasks. |
| 17 | **Same-edge DUT output sampling race** | TB checks registered DUT outputs immediately after `@(posedge clk)`, `@(posedge rst)`, or inside `always @(posedge clk or posedge rst)` without a clocking-block input skew or small settle delay. RTL nonblocking assignments settle in the NBA region, so same-edge TB checks can see old values and falsely fail. Reset monitors must wait `#1` or use an equivalent sampling scheme, then confirm reset is still asserted before checking reset outputs. |
| 18 | **Spec numeric contract not asserted** | Spec-to-TB traceability: every numeric contract the spec states — latency in cycles, cycle/throughput budgets, field and parameter widths, counter limits, and worked example input→output vectors — must appear as an explicit TB assertion or expected-value check. Look for: spec-stated numbers absent from the TB entirely; a latency or width that is only implicit in loop bounds or `#delays` instead of asserted; spec example vectors missing from the deterministic test cases. A TB that never encodes the spec's own numbers can only test self-consistency. |

### MAJOR — Icarus Verilog compatibility (3 checks, only when the
testbench runs under `klt functional-verification`'s `engine: "icarus"` —
see [`docs/cli/functional-verification.md`](../../cli/functional-verification.md)
"Engines")

| # | Check | Look for |
|---|-------|----------|
| 19 | **`always_comb` with loop-var-indexed array bit/part-selects** | `for(int i …)` loops inside `always_comb` that do bit- or part-selects on array elements: `arr[i][j][7]`, `{arr[i][j][6:0], 1'b0}`, or part-selects into wide vectors with loop-derived offsets like `vec[N-i*W-1-:W]`. iverilog emits `sorry: constant selects in always_* processes` and makes the block sensitive to ALL bits of the array/vector, causing cascading re-evaluations that freeze simulation. **Fix:** move the computation to `generate` + `assign` blocks where `i`/`j` are genvars — continuous assignments don't have sensitivity lists and are unaffected. |
| 20 | **`$isunknown` on concatenated signal bundles** | `$isunknown({sig_a, sig_b, bus_c, ...})` in monitors or reset checks when the simulator is Icarus/iverilog. Icarus can false-report a wide/mixed concatenation as unknown even when each member signal is individually known. **Fix:** use per-signal `$isunknown(sig)` checks and print the exact signal name, value, and time. |
| 21 | **Ternary string expression in display/sentinel calls** | Conditional selection between string literals inside `$display`, `$write`, `$sformatf`, or sentinel/result emission, for example `$display((total_failed == 0) ? "[SIM_RESULT] PASSED" : "[SIM_RESULT] FAILED");`. Icarus can treat the selected string literal as a packed integer and print a large decimal instead of the text, silently breaking the pass/fail sentinel a caller greps for. **Fix:** use explicit branches: `if (total_failed == 0) $display("[SIM_RESULT] PASSED"); else $display("[SIM_RESULT] FAILED");`. Also avoid ternary-selected strings in formatted diagnostics; compute separate displays or assign to a `string` variable before formatting. |

### MINOR — style/coverage (3 checks, report only)

| # | Check | Look for |
|---|-------|----------|
| 22 | **Insufficient `$display`** | Missing config/test name/iteration in pass/fail messages. |
| 23 | **No stall re-run** | DUT has backpressure interface but tests run once without stall injection. |
| 24 | **Hardcoded magic numbers** | Literal constants instead of package parameters; literal array sizes; config-dependent counts that break with different parallelism. |
