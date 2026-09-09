# Digital review guides

The analog canaries have a written review bar a Judge can apply directly:
[design-evidence tiers](../../design-evidence-tiers.md), ratified spec
tables, and `klt drc`/`lvs`/`sim` records. This directory is the digital
equivalent — per-concern RTL and testbench review guides for the digital
canaries (`sky130-modexp`, `sky130-usb2-phy`, `gf180-usb2-phy`,
`sky130-fpga`, `sky130-gcedram`), so a Judge reviewing a Verilog PR has
more than general software-review instincts to work from.

**Provenance.** Every guide in this directory is ported, with attribution,
from [boldaxolotl/booley](https://github.com/boldaxolotl/booley)
(Apache-2.0) — see [`NOTICE`](NOTICE) for the full attribution record and
each file's own provenance header. This issue (#1587) owns only the `klt`
side of the port; wiring these guides into each digital canary's own
`CLAUDE.md` review-bar text is left to one follow-up issue per canary (see
the parent issue body).

## When each guide applies

| Guide | Applies to | Scope |
|---|---|---|
| [`rtl-bugs.md`](rtl-bugs.md) | Any RTL PR | Functional bugs, synthesis hazards (latches, comb loops, multi-driven nets), conditional-compilation defects |
| [`rtl-protocol-cdc.md`](rtl-protocol-cdc.md) | Any RTL PR with module-boundary handshakes or more than one clock domain | Ready/valid & req/ack protocol violations, clock domain crossings |
| [`rtl-code-style.md`](rtl-code-style.md) | Any RTL PR | Comments, naming, readability, assertion placement — cites [`rtl-style-guide.md`](rtl-style-guide.md) |
| [`rtl-optimization.md`](rtl-optimization.md) | Any RTL PR | Timing/area/power PPA findings and provably dead RTL, with a synthesized-hardware proof requirement |
| [`rtl-security.md`](rtl-security.md) | RTL PRs touching security-sensitive IP (keys, secrets, safety-critical logic — `sky130-modexp` today) | Side-channel leakage, data exposure, fault injection, error handling |
| [`rtl-spec.md`](rtl-spec.md) | Any RTL PR with a written spec (issue/PR description, `docs/design/*.md`, or canary README) | Behavioral spec compliance only — no code quality, no synthesis metrics |
| [`tb-review.md`](tb-review.md) | Hand-written SystemVerilog testbench PRs (not cocotb) | False-pass detection, coverage gaps, Icarus-specific TB pitfalls |
| [`cocotb-tb-review.md`](cocotb-tb-review.md) | cocotb (Python) testbench PRs — the kind `klt functional-verification` actually runs | False-pass detection, coverage gaps, cocotb-specific pitfalls |
| [`rtl-style-guide.md`](rtl-style-guide.md) | Reference for `rtl-code-style.md` and RTL authors | The canonical RTL coding-standards table |
| [`tb-style-guide.md`](tb-style-guide.md) | Reference for `tb-review.md` and SV TB authors | The canonical SystemVerilog TB style guide |
| [`cocotb-tb-style-guide.md`](cocotb-tb-style-guide.md) | Reference for `cocotb-tb-review.md` and cocotb TB authors | The canonical cocotb TB style guide |

**Which testbench guide to use:** `klt functional-verification` (see
[`docs/cli/functional-verification.md`](../../cli/functional-verification.md))
only drives cocotb testbenches — `request.testbench.module` is always a
Python module. Use [`cocotb-tb-review.md`](cocotb-tb-review.md) /
[`cocotb-tb-style-guide.md`](cocotb-tb-style-guide.md) for any testbench a
canary asks `klt functional-verification` to run. [`tb-review.md`](tb-review.md)
/ [`tb-style-guide.md`](tb-style-guide.md) (SystemVerilog, sentinel-driven)
remain useful for a legacy or hand-invoked SV testbench a canary still
carries outside that path, and their false-pass patterns generalize across
testbench languages even where the SV-specific mechanics (sentinels,
clocking blocks, `dut` instance naming) do not apply.

Also see [`rtl-mutation-testing.md`](../rtl-mutation-testing.md) (issue
#1593, a sibling of this port) — a mutation-*proposer's* guide for
`klt functional-verification --mutations` (issue #1592), which measures
whether a testbench would actually catch a given class of RTL bug rather
than reviewing the RTL or the testbench directly. It complements this
directory rather than belonging in it: these guides are read by a
*reviewer*, that one is read by whoever *authors* a mutation proposal.

## Evidence mapping: which `klt` verb backs a checklist item

Every guide above is read-and-judge — a reviewer applying human/Judge
analysis, not something `klt` itself runs. Where a checklist item asks the
reviewer to *check the RTL/testbench against tool evidence* rather than to
reason from first principles, this table names the `klt` verb and response
field that supplies it, or says none exists yet and names the tracking
issue.

| Checklist item | Guide | `klt` verb | Response field | Notes |
|---|---|---|---|---|
| Inferred latches | `rtl-bugs.md` §B | `klt synthesize` | `structural.latches` / `structural.unexpected_latches` | Issue #1588. `structural.expected_latches` in the request whitelists intentional latches. |
| Combinational loops | `rtl-bugs.md` §B | `klt synthesize` | `structural.comb_loops` | Issue #1588. |
| Multi-driven nets | `rtl-bugs.md` §B | `klt synthesize` | `structural.multi_driven` | Issue #1588. |
| Any-of-the-above pass/fail | `rtl-bugs.md` §B | `klt synthesize` | `structural.has_critical`, exit code `3` | Issue #1588. |
| General Yosys warnings (undriven wires, etc.) | `rtl-bugs.md`, `rtl-optimization.md` | `klt synthesize` | `warnings.total` / `warnings.by_category` / `warnings.representatives` | Issue #1605. Bounded, deduplicated-by-category summary of the run log. |
| PPA claim (area/timing win or regression) | `rtl-optimization.md` | `klt synthesize` | `baseline` QoR-delta block | Issue #1605. Requires a prior run's response as the comparison baseline — see "`baseline`" in `docs/cli/synthesize.md`. |
| Synthesis metrics / area reduction targets (explicitly out of scope for the reviewer) | `rtl-spec.md` | `klt synthesize` | `structural`, `warnings`, `baseline` | Same fields as above — named here because `rtl-spec.md` tells the reviewer *not* to judge these themselves. |
| Gate-level netlist == source RTL (no accidental behavior change from synthesis) | `rtl-bugs.md`, `rtl-spec.md` | `klt equiv` (or `klt synthesize --verify-equivalence`) | top-level `status` / `equivalence` object | See `docs/cli/equiv.md`. Combinational equivalence today; sequential (`"yosys-sequential"` engine) per issue #1313. |
| Overall functional pass/fail, per-test detail | All testbench guides | `klt functional-verification` | `status`, `tests[]` (`.status`, `.error_message`) | See "Response" in `docs/cli/functional-verification.md`. |
| Filtered/misspelled test name (false "never ran") | `cocotb-tb-review.md` #1 | `klt functional-verification` | `request.testbench.testcase`, `tests[].status == "skipped"` | A misspelled filter name never runs and never appears `"passed"` — compare the filter list against the module's decorated functions by hand; `klt` does not cross-check this today. |
| Insufficient stimulus diversity / weak coverage | `tb-review.md` #15, `cocotb-tb-review.md` #13 | `klt functional-verification --options.coverage` | `coverage.line_pct` / `.toggle_pct` / `.branch_pct` / `.expr_pct` | Verilator engine only; `null` on Icarus runs. High coverage with few vectors is exactly the false-positive these checks name. |
| Icarus-specific TB pitfalls (##19–21) | `tb-review.md` | `klt functional-verification` | `engine` request field, `environment.engine_version` | These are source-level patterns to catch by inspection; `klt` does not detect them mechanically. |
| Testbench genuinely catches injected bugs (not just "passes today") | All testbench guides | `klt functional-verification --mutations` | `mutation_testing.mutation_score`, `mutation_testing.results[]` | Issue #1592/#1593. See `docs/guides/rtl-mutation-testing.md` for the proposer-side guide and `docs/cli/functional-verification.md` "Mutation testing" for the full contract. This is the mechanical version of every "does this TB actually check anything" false-pass check above. |
| Reproducibility of a specific run (seed, environment) | All testbench guides | `klt functional-verification` | `environment.random_seed`, `environment.engine_version`, `environment.results_xml` | See "Reproducibility: `random_seed`" in `docs/cli/functional-verification.md`. |
| Post-route timing-annotated re-verification | `rtl-spec.md`, `rtl-protocol-cdc.md` (pipeline latency claims) | `klt functional-verification --options.sdf` | `environment.sdf` | See "SDF back-annotation" in `docs/cli/functional-verification.md`; Icarus only, 13.0+. |
| Block-level maturity claim ("this digital block is T1") | All guides, taken together | `klt signoff` | per [`docs/design-evidence-tiers.md`](../../design-evidence-tiers.md) T1 checklist | These guides feed the human/Judge review step; they are not themselves a `klt signoff` input. |
| Security/side-channel findings (`rtl-security.md`) | `rtl-security.md` | *(none yet)* | — | No `klt` verb analyzes constant-time behavior or fault-injection resistance today; this remains a fully manual review pass. |
| Spec-quoted behavioral claims (`rtl-spec.md`) | `rtl-spec.md` | *(none yet)* | — | Spec-to-RTL/TB traceability is a manual read; no `klt` verb parses a prose spec. |
| CDC structural issues (missing synchronizers, generated clocks) | `rtl-protocol-cdc.md` §C | *(none yet)* | — | No CDC linter is wired into `klt` today; flagged by inspection only. |

Items marked "*(none yet)*" are read-only for now — flag them in review
prose. If a future `klt` verb starts producing evidence for one of these
(a CDC lint pass, a constant-time analysis, etc.), update this table and
the citing guide together.
