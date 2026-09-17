# Design: staged agent design pipeline — the digital path

**Status:** design doc / proposal. This is the digital counterpart to
[`design-pipeline.md`](design-pipeline.md) (Epic #105, the analog path), filed
as [issue #1956](https://github.com/2AMLogic/klayout-tools/issues/1956). It is
the single source of truth an agent (or an orchestrator assigning agents) loads
to know what stage it is in on the **digital** path, what artifact it owes, when
it is allowed to call the stage done, and when a loop has stopped converging.
Nothing here is implementation — no `klt` subcommand, schema file, or skill is
added by this document, and no existing contract is changed by it.

Per [docs/ARCHITECTURE.md](../ARCHITECTURE.md), the digital path is one of
three peers, not a variant of the analog one:

> An agent can take a spec through one of three peer paths on an open PDK,
> unaided, with every step headless and JSON-contracted — analog (spec →
> schematic/generator → sized circuit → layout → DRC/LVS clean → extracted
> netlist → simulation-verified), digital (spec → RTL → synthesis →
> place-and-route → DRC/LVS clean → timing-closed), and mixed-signal (both
> paths plus the signoff seam between them).

That sentence names the digital stages but not how to move between them. The
digital *verbs* all ship — `klt synthesize`, `klt techmap`, `klt equiv`,
`klt functional-verification`, `klt place-and-route`, `klt sta`, `klt drc`,
`klt extract`, `klt lvs`, `klt erc`, `klt power`, `klt signoff` — and the
digital *review* bar ships ([`docs/guides/digital-review/`](../guides/digital-review/README.md),
11 guides). What did not exist before this document is the navigation layer
between them: eleven stages, the three loops, a proposed input/output contract
per stage, which class of model should run each one, and an honest accounting
of which stages `klt` already serves versus which still await tooling.

## Scope: same line, drawn the same place

This doc defines **navigation**, not **strategy within a stage** — the same
boundary [`design-pipeline.md`](design-pipeline.md) → "Scope: skills are
procedure, not strategy" draws, and it is not re-argued here. A stage's
contract says "you are in RTL authoring, your input is a microarchitecture
choice plus a block spec, your output is committed RTL whose regression passes
at D6, and here is the escalation rule if you're stuck." It does not say how to
pipeline a Blakley modular multiplier, how to trade pipeline depth against
Fmax, or which of three FIFO implementations to pick. That reasoning happens
inside the stage and is the reasoning module's lane (ROADMAP.md Phase 5), not
this document's.

One digital-specific addition to that boundary, because the seam is easy to
cross by accident: the review guides in
[`docs/guides/digital-review/`](../guides/digital-review/README.md) are
**strategy-adjacent quality bars applied to a stage's output**, not stage
contracts. `rtl-bugs.md` tells a reviewer what to look for in RTL; D5 below
tells an agent when RTL authoring is *done*. They compose — D5's exit criteria
cite the mechanical evidence those guides' own "Evidence mapping" table names —
but neither replaces the other.

## 1. Stage graph

```
 D1  design proposal
       |
 D2  architecture & budget partition (area/power/Fmax)
       |
 D3  block spec
       |
 D4  microarchitecture selection
       |
 D5  RTL authoring        <--loop C: RTL <-> functional verification--+
       |                                                              |
 D6  functional verification (cocotb) ---------------------------->---+
       |
 D7  synthesis  [+ klt equiv gate]  <--loop D: synth/P&R <-> timing--+
       |                                                             |
 D8  place & route  [+ klt equiv gate]  <--loop E: P&R <-> DRC/LVS/ERC--+
       |                                                             |  |
 D9  DRC / LVS / ERC  ------------------------------------------->---+--+
       |                                                             |
 D10 STA across corners + post-route gate-level sim  ----------->----+
       |
 D11 signoff report
```

The chain is drawn linear because that is the direction of a *successful* run,
but three loops are structural, not exceptional. An orchestrator must recognize
them as iteration rather than failure, and must also recognize when iteration
has become stuck.

### Crosswalk to the analog S1–S11

The D-numbers are **not** in one-to-one correspondence with the analog
S-numbers, and assuming they are is the fastest way to mis-navigate. Two
structural differences cause the offset:

| Digital | Analog | Relationship |
| --- | --- | --- |
| D1 design proposal | S1 | Same stage, same artifact. |
| D2 architecture & budget partition | S2 | Same stage; the budget rows differ (area/power/Fmax/latency, not area/power/noise). |
| D3 block spec | S3 | Same stage, same (missing) schema — see D3's recorded decision. |
| D4 microarchitecture selection | S4 topology selection | Same *role*, different tooling reality: S4 is a bounded `klt kb` query; D4 has no corpus to query (see §4). |
| D5 RTL authoring | *(none)* | **No analog counterpart.** Not S5's numeric optimization, not S6's mechanical transcription. |
| D6 functional verification | *(partly S10)* | Closest to S10's pre-layout Loop A passes, but a cocotb regression is a *logic-correctness* gate, not a numeric-margin measurement. |
| D7 synthesis (+ equiv gate) | S5 + S6, collapsed | Sizing and netlist elaboration are one engine invocation on the digital path: `klt synthesize` picks the gates *and* emits the netlist. |
| D8 place & route | S7 layout generation | Same stage — "produce geometry from a netlist" — with a different generator. |
| D9 DRC / LVS / ERC | S8 (+ S9 absorbed) | **S9 extraction has no free-standing digital counterpart.** Extraction is a sub-step of D9's LVS leg (`klt extract --abstract-cells --def-net-names`), and post-layout parasitics for D10 arrive as SPEF written by D8's own engine, not from a separate extraction stage. ERC/antenna joins here. |
| D10 STA + post-route GLS | S10 simulation across corners | Same role (verify the built thing across the declared corner set), two artifacts instead of one: a timing verdict *and* a functional re-verification. |
| D11 signoff report | S11 | Same stage, same aggregator — but see D11 and §4: no digital stage's own envelope is a kind `klt signoff` recognizes today. |

The freed numbers are what make the graph still eleven stages long: S5/S6
collapse into D7 and S9 is absorbed into D9, releasing exactly two slots for
D5 and D6, the two digital stages with no analog counterpart at all.

### Loop C — RTL ⇄ functional verification

- **Members:** D5 (RTL authoring) and D6 (functional verification). Early
  passes run pre-synthesis — RTL sources straight into
  `klt functional-verification`, no D7 — for a fast correctness signal before
  paying for a synthesis iteration. The *identical* testbench is re-run later
  with `sources` pointed at D7's `netlist_path` (a gate-level re-check) and
  again at D10 with `--options.sdf` back-annotation; the contract's `sources`
  field is deliberately generic enough that none of those three passes needs a
  different request shape
  ([`digital-flow-contracts-spike.md`](digital-flow-contracts-spike.md) §7).
- **Iterating:** each pass edits RTL in response to
  `klt functional-verification`'s `tests[].status`/`tests[].error_message`
  ([`docs/cli/functional-verification.md`](../cli/functional-verification.md)),
  or edits the *testbench* when the failure is a testbench defect rather than a
  design defect.
- **Exit criteria (converged)** — all four, on the same RTL revision:
  1. `status: "pass"` (exit `0`) across the full declared regression, not a
     filtered subset. A `tests[].status == "skipped"` on a test the declaration
     names is not a pass — a misspelled `testbench.testcase` filter silently
     runs nothing and `klt` does not cross-check this
     ([`docs/guides/digital-review/README.md`](../guides/digital-review/README.md)'s
     evidence table, `cocotb-tb-review.md` #1).
  2. Declared coverage floor met — `coverage.line_pct`/`.toggle_pct`/
     `.branch_pct`/`.expr_pct`. These are **Verilator-engine-only** (`null` on
     an Icarus run), so a coverage claim requires a Verilator leg, not just a
     passing Icarus leg.
  3. `klt synthesize`'s `structural.has_critical: false` on that same revision
     — no inferred latch, combinational loop, or multiply-driven net (issue
     #1588). A regression that passes on RTL that will not synthesize cleanly
     is not converged; it is converged on a design D7 is going to reject.
  4. The regression is demonstrated *able to fail*: a
     `klt functional-verification --mutations` `mutation_score` at or above the
     declared floor, or an explicitly recorded exception
     ([`docs/guides/rtl-mutation-testing.md`](../guides/rtl-mutation-testing.md),
     issues #1592/#1593). This is Loop C's equivalent of Loop A's "a stable
     candidate, not a single lucky corner set" — a green regression that cannot
     detect an injected bug is not evidence.
- **Stuck, not iterating:**
  - The same test is the failing test across N consecutive passes with no
    change in remedy.
  - A fix flips a previously-passing test to failing (regression thrash — the
    whack-a-mole case `rtl-bugs.md` names).
  - Passes alternate between an RTL edit and a testbench edit with no net
    change in `passed_count`. This is the **design-vs-bench oscillation**, and
    it has no Loop A analogue: a SPICE testbench can be wrong, but a cocotb
    coroutine can be wrong *in a way that confirms the RTL's own bug*, so the
    loop has two mutually-blaming sides instead of one measured one.
  - `passed_count` rises while `mutation_score` falls — the bench is being
    weakened to fit the design. This is the most dangerous Loop C failure
    because every other signal reads as progress.

### Loop D — synthesis/P&R ⇄ timing

- **Members:** D7 (synthesis), D8 (place & route), and D10 (STA), with two
  distinct remedy sides: a **constraint/QoR** change at D7
  (`constraints.clock_period_ns`, which reaches ABC's own `abc -D` delay target
  per issue #807) or a **physical** change at D8 (`floorplan.utilization_pct`,
  `aspect_ratio`, `core_margin_um`, `io.layer_h`/`layer_v`, `power.straps`,
  `seed`). D10 is the measurement, never a remedy.
- **Iterating:** each pass changes exactly one of those and re-measures with
  `klt sta` against the **same** DEF-per-corner discipline
  ([`docs/cli/sta.md`](../cli/sta.md) — re-running `place-and-route` per corner
  produces N different placements and is a sweep of N designs, not a
  characterization of one).
- **Exit criteria (converged)** — all four:
  1. `klt sta` reports `worst_slack_ns >= 0` for **setup and hold** at every
     corner in the declared corner set.
  2. `timing_status` is the constrained verdict, not OpenSTA's unconstrained
     sentinel. A design with no register-to-register path reports `1e+39`/`0`
     identically to a fully-clean one
     ([`docs/cli/place-and-route.md`](../cli/place-and-route.md)'s
     `worst_slack_ns` row, issue #1865) — check `timing_status` before treating
     the number as a measurement.
  3. The corner set was **declared up front and not narrowed mid-loop.**
     `klt place-and-route`'s own in-flow slack is a nominal-corner number; a
     full corner sweep on the same geometry can be tens of nanoseconds worse
     (a real observed case: −14.683 ns at a sub-1.5 V `ss_n40C` corner against
     +6.98 ns nominal, reported in
     [#1959](https://github.com/2AMLogic/klayout-tools/issues/1959)). Closing
     against the nominal corner alone is not convergence; it is a narrower
     claim wearing convergence's clothes.
  4. The parasitics source is named, and any pass claimed as a real-parasitics
     post-route measurement has `spef_sta.annotation_complete: true`
     (`design_nets_annotated == design_nets_total`, non-zero). An incomplete
     annotation makes the number not a real-parasitics measurement at all, and
     `klt sta`'s own `spef_annotation` false-positive is tracked as #1624.
  5. The post-route gate-level regression passes on the same routed geometry
     (`klt functional-verification --options.sdf`, Icarus-only). STA closes
     timing; it does not show the design still *works* at that timing.
- **Stuck, not iterating:**
  - WNS is not monotonically improving **at the binding corner** across N
    consecutive passes. Tracking an aggregate instead hides the common case
    where the aggregate improves while the binding corner worsens.
  - A timing fix pushes `area_um2`/`instance_count`/`utilization_pct` past D2's
    budget allocation — a genuine tradeoff, which re-enters D2 per the
    non-loop backtracking below, not another Loop D pass.
  - The binding corner changes every pass (corner-chasing) — evidence the
    corner set, not the design, is what needs deciding.
  - Improvement is achieved only by changing `seed`. Placement and routing are
    genuinely stochastic and `seed` is a *required* request field precisely so
    this is detectable
    ([`digital-flow-contracts-spike.md`](digital-flow-contracts-spike.md) §5) —
    a seed lottery win is not convergence, because the next seed is not
    reproducibly better.

### Loop E — P&R ⇄ DRC/LVS/ERC

- **Members:** D8 (place & route) and D9 (DRC/LVS/ERC). The remedy is **always**
  a change to D8's inputs, never a hand edit to the routed GDS: a hand edit
  breaks the reproducibility half of
  [`design-evidence-tiers.md`](../design-evidence-tiers.md) T1 item 2 ("P&R
  script/flow committed, not a one-off hand edit") *and* silently desynchronizes
  D8's `verilog_path` as-built netlist from the geometry, which turns D9's LVS
  leg into a comparison against a netlist that no longer describes the layout.
- **Iterating:** each pass changes P&R inputs in response to `klt drc`'s
  `violation_count`/`rule_counts` ([`docs/cli/drc.md`](../cli/drc.md)),
  `klt lvs`'s `error_count`/`category_counts` ([`docs/cli/lvs.md`](../cli/lvs.md)),
  `klt erc`'s antenna-ratio verdict ([`docs/cli/erc.md`](../cli/erc.md)), and
  `klt power`'s `em_verdict`/`worst_case_droop_mv`
  ([`docs/cli/power.md`](../cli/power.md)).
- **Exit criteria (converged):**
  1. `klt drc` reports `status: "clean"` (`violation_count: 0`) with the deck's
     own coverage gaps enumerated in the claim, not hidden behind "clean"
     ([`design-evidence-tiers.md`](../design-evidence-tiers.md) item 3).
  2. `klt lvs` reports `status: "match"` with `error_count: 0`, compared
     against **D8's own `verilog_path`** via `reference.form:
     "gate-level-verilog"` (issue #1336). Note carefully: the pass condition is
     `status`/`error_count`, **not** `mismatch_count: 0` — a real routed
     `sky130-modexp` run reaches `status: "match"`, `error_count: 0` with
     `mismatch_count: 4` (four `severity: "warning"` ambiguous-net-pairing
     disclosures). The analog doc's "clean device/net compare" phrasing is not
     transcribable literally here.
  3. `klt erc`'s antenna-ratio verdict passes for every gate.
  4. Separately tracked, not a T1 item: `klt power`'s `em_verdict.status:
     "pass"` and an IR-drop figure inside budget
     ([`design-evidence-tiers.md`](../design-evidence-tiers.md) → "Power/IR-drop
     + EM evidence (not yet a T1 item)"). A failing EM/IR-drop verdict's remedy
     is PDN geometry — `power.straps`/`power.connects` at D8 — so it iterates in
     this loop even though it grades nowhere at D11 yet.
- **Stuck, not iterating:**
  - Violation count is not monotonically decreasing across N consecutive
    passes, or the same rule fires after being "fixed" (a fix that moves the
    violation rather than resolving it).
  - A DRC fix breaks LVS correspondence, or vice versa — Loop B's tradeoff case
    transferred unchanged.
  - **The digital-only detector: the mismatch count is *insensitive* to the P&R
    change.** A digital gate-level LVS run with hundreds-to-thousands of
    mismatches dominated by `topology`/`net.split` is the
    extraction-methodology signature, not a connectivity defect. The canonical
    case: `sky130-modexp` once reported 1324 mismatches (888 `topology`, 434
    `net.split`, 2 `net.merged`) building its LVS reference by hand, and the
    *same design and same routed geometry* reports `status: "match"` once the
    reference comes from `klt lvs`'s native `reference.form:
    "gate-level-verilog"` converter instead
    ([`tests/corpus/sky130_modexp_canary/README.md`](../../tests/corpus/sky130_modexp_canary/README.md),
    versus the earlier attempt recorded in
    [`sky130-modexp-canary-signoff-status.md`](sky130-modexp-canary-signoff-status.md)).
    Zero P&R passes separate those two numbers. When the count does not move
    with the geometry, the loop is measuring the tool; escalate to a
    methodology decision (`klt extract --abstract-cells`/`--def-net-names`,
    `def_net_names.unresolved_single_pin_nets`), not another D8 pass.

### Non-loop backtracking (named, not modeled in detail)

Three escapes are real but out of this doc's detail level, flagged so an
orchestrator does not mistake them for a broken stage machine. All three are
**N-limited escalations to a human or to the frontier tier**, not infinite
backtracking — the same discipline the three named loops use.

- **Spec infeasibility.** D7/D8/D10 discovering D3's spec (Fmax, area, power)
  unmeetable is a legitimate outcome, not a failure of those stages. It
  re-enters at D3, or at D2 if the budget partition itself needs renegotiating.
- **Signoff rejection.** D11 finding a spec row earlier stages' own checks did
  not cover re-enters at whichever stage owns that row.
- **Microarchitecture infeasibility — digital-only.** Loop D exhausting *both*
  its remedy sides (constraint *and* physical) without closing timing is not a
  D7 or D8 failure: it is evidence the **microarchitecture** cannot hit the
  declared Fmax at this node, because the critical path is structural (pipeline
  depth, radix, memory organization). That re-enters at **D4**, not D3 — the
  spec may be perfectly feasible for a different microarchitecture. The analog
  path has no analogue, because it has no "add a pipeline stage" remedy. This
  escape is load-bearing for D7's equivalence decision below: re-entering D4 is
  the pipeline's *sanctioned* answer to a structurally-unmeetable Fmax, and
  enabling retiming inside D7 is the unsanctioned one.

## 2. Per-stage contracts

Field conventions match [`docs/json-contract.md`](../json-contract.md):
`schema_version` alongside flat top-level fields, new fields additive, unknown
fields ignored. Where a stage already has a real, shipped contract (D6–D11),
this section defers to that command's own doc rather than re-describing it.

**Recorded decision: this document proposes no new schema names.** D1–D4 reuse
[`design-pipeline.md`](design-pipeline.md) §2's own four proposals
(`klt.pipeline.proposal/1`, `klt.pipeline.architecture/1`,
`klt.pipeline.blockspec/1`, `klt.pipeline.topology/1`) verbatim, with digital
*values* in analog-neutral *fields*; D5–D11 defer to shipped command contracts.
Reason: every one of those four artifacts is path-independent in shape (a
normalized problem statement, a block list with a budget partition, a per-block
spec, a selected-structure reference plus rationale) and only its row
vocabulary differs. Minting `klt.pipeline.digital-blockspec/1` alongside
`klt.pipeline.blockspec/1` would fragment an unshipped surface into two
dialects before either has a consumer — the same
"don't design speculative contract surface" discipline
[`digital-flow-contracts-spike.md`](digital-flow-contracts-spike.md) §3 applied
to #247's metric namespace. If and when S3 gets a real schema, D3 uses that one.

### D1 — design proposal

| | |
| --- | --- |
| Input artifact | Free-form spec: prose requirements, a target function/protocol, a reference implementation or published design. |
| Output artifact | `klt.pipeline.proposal/1` (proposed, shared with S1): normalized problem statement — target function, top-level specs as known, constraints, open questions. |
| Entry criteria | Always available — the pipeline's start state. |
| Exit criteria | Every top-level spec field is either a number/range or explicitly marked "to be resolved in D2/D3"; no silent gaps. A digital proposal must additionally state whether the block is **RTL-flow** or **full-custom** digital (§5) — that choice decides which stages D5–D10 it even walks. |
| `klt` verbs | None — pure intake, out of tool scope by design. |
| Failure modes | Underspecification passed downstream as resolved; scope beyond the pipeline's target (full-chip digital P&R and timing closure are an explicit ROADMAP.md non-goal — block-level is the arena). |

### D2 — architecture & budget partition

| | |
| --- | --- |
| Input artifact | D1's normalized proposal. |
| Output artifact | `klt.pipeline.architecture/1` (proposed, shared with S2): block list with interfaces, plus a budget partition allocated per block. Digital rows are **area, power, Fmax/clock period, and latency/throughput**; the analog noise/matching rows do not apply. For a mixed-signal block this is also where the analog/digital partition boundary is decided (§5). |
| Entry criteria | D1 output has no unresolved top-level spec fields. |
| Exit criteria | Every block has a complete allocation, and allocations close against the system target rather than merely against each other. A digital partition's clock-period allocation must be a *single* number per clock domain, and every crossing between domains must be named — an unnamed crossing is a CDC defect D5 will author blind, and no `klt` verb will catch it (§4). |
| `klt` verbs | None — no optimization/partition tool exists on either path. |
| Failure modes | A partition that does not close; a block given an allocation later stages prove infeasible (routes back per §1); a clock-domain graph left implicit. |

### D3 — block spec

| | |
| --- | --- |
| Input artifact | One block's budget allocation from D2. |
| Output artifact | `klt.pipeline.blockspec/1` (proposed, shared with S3): per-block spec — functional behavior, interface/protocol and pinout, clock/reset scheme, the declared corner set, Fmax/area/power ceilings, and the declared functional-coverage and mutation-score floors Loop C converges against. |
| Entry criteria | Block has a closed budget allocation from D2. |
| Exit criteria | Complete enough to select a microarchitecture against, *and* complete enough for Loop C and Loop D to have stated targets: the corner set, coverage floor, and mutation-score floor are spec fields, not values chosen later by whoever happens to run D6/D10. A loop whose convergence threshold is decided after the first failing measurement is not converging against a spec. |
| `klt` verbs | None currently. |
| Failure modes | A spec whose corner set is narrowed later to fit a result (the Loop D corner-chasing case, made structural); a protocol described in prose with no citable source, which makes `rtl-spec.md` review unfalsifiable; missing reset/initialization semantics, which surface as a gate-level-vs-RTL divergence at D10 rather than at D6. |

**Recorded decision — the digital block-spec schema is the *same* open gap as
the analog one, not a second one.** `design-pipeline.md` §4's S3 row records
that no machine-readable block-spec schema exists, and that this is what blocks
`klt signoff` from diffing a verdict against spec fields. The digital path
inherits that hole exactly, with no digital-specific addition:

- A ratified spec lives, on both paths, in the block repo's own README under a
  `## Target specification (...)` heading, parsed by
  `scripts/ingest-canary.py`'s `parse_spec_summary()` into `spec_summary.rows[]`
  ([`design-evidence-tiers.md`](../design-evidence-tiers.md) → "Where a ratified
  spec lives").
- `parse_spec_summary()` reads column headers verbatim and does not match
  `parameter` cell values against a fixed enum — so an `Fmax`, `Coverage`, or
  `Mutation-Score` row parses today with **no code change**, the same finding
  that doc already records for its own `Area-Eff` row.
- Therefore the gap is *purely* the absence of a schema, identical on both
  paths. This document deliberately does **not** file a digital-specific
  block-spec-schema proposal, and does not claim D3 is more blocked than S3 is.
  The one digital consequence worth naming is downstream, at D11: because D3 has
  no schema *and* no digital stage's evidence is a recognized `klt signoff`
  kind, a digital block is two gaps away from a machine-checked spec verdict
  where an analog block is one — see D11 and §4.

### D4 — microarchitecture selection

| | |
| --- | --- |
| Input artifact | D3's block spec. |
| Output artifact | `klt.pipeline.topology/1` (proposed, shared with S4): the selected structure (algorithm/dataflow, pipeline depth, radix, memory organization, clock-domain structure), the rationale, and the spec fields the choice was made against. |
| Entry criteria | Block spec complete per D3's exit criteria. |
| Exit criteria | A microarchitecture is chosen, and its known limitations against the spec are recorded rather than silently absorbed — specifically its expected critical path and the Fmax that path implies, since that is the number Loop D will be measured against and the trigger for the D4 re-entry named in §1. |
| `klt` verbs | `klt kb search`/`show`/`list` ([`docs/cli/kb.md`](../cli/kb.md), shipped) exist — but see the decision below: the corpus and schema are transistor-level, so for a digital block this stage's output is a *description*, effectively never a KB `id`. |
| Failure modes | A microarchitecture chosen for familiarity rather than spec fit; an expected-critical-path claim recorded as prose with no number, which makes the D4 re-entry trigger in §1 unevaluable. |

**Why this stage's tooling story is the reverse of S4's.** S4 is the analog
path's *best*-served early stage: `klt kb` is shipped and what remains is
corpus breadth. D4 looks identically served and is not. `kb/schema/entry.schema.json`'s
own properties are `topology`, `spec_class`, `pdk_portability`,
`sizing_approach`, `layout_idioms` — a transistor-level circuit vocabulary,
with no field for pipeline depth, throughput, or gate count — and all 21
entries in `kb/entries/` are device-level circuits. A digital microarchitecture
has nowhere to land in that schema, so D4 today is first-principles judgment,
not bounded search over documented candidates. That is why §3 classes D4
*higher* than S4 rather than the same, and why §4 records it as a real
digital-only gap rather than a corpus-breadth note.

### D5 — RTL authoring

| | |
| --- | --- |
| Input artifact | D4's microarchitecture choice + D3's block spec. |
| Output artifact | Committed RTL sources — the same `sources` array both `klt synthesize` and `klt functional-verification` take as their request input ([`digital-flow-contracts-spike.md`](digital-flow-contracts-spike.md) §2/§7). No JSON envelope: RTL *is* the artifact. |
| Entry criteria | Microarchitecture selected (D4). |
| Exit criteria | Loop C's convergence criteria (§1), all four — regression pass, coverage floor, `structural.has_critical: false`, mutation-score floor. |
| `klt` verbs | None that author RTL — this stage is agent-side by construction, the digital counterpart of S5's recorded "candidate proposal stays agent-side" decision, though for a different reason (there is no numeric search space here to optimize over). Its mechanical checks live in neighbouring verbs: `klt synthesize`'s `structural` (#1588) and `warnings` (#1605) blocks, and `klt functional-verification`'s regression. |
| Failure modes | Loop C's stuck conditions (§1). Plus the three concerns with **no mechanical checker at all** — CDC/synchronizer structure, ready/valid and req/ack protocol conformance, and constant-time/side-channel behavior. [`docs/guides/digital-review/README.md`](../guides/digital-review/README.md)'s evidence table marks each `*(none yet)*`; a defect in any of them passes D6, D7, D8, D9, and D10 and is caught only by review. That asymmetry is the whole reason §3 refuses to class D5 as mechanical. |

### D6 — functional verification

| | |
| --- | --- |
| Input artifact | RTL from D5, *or* D7's `netlist_path` (gate-level re-check), *or* D8's `verilog_path` plus `spef_sta.sdf_path` (the D10 back-annotated pass) — the same request shape for all three. Plus a cocotb testbench module. |
| Output artifact | `klt functional-verification`'s shipped JSON ([`docs/cli/functional-verification.md`](../cli/functional-verification.md)): `status`, `tests[]`, `coverage`, `mutation_testing`, `environment`. |
| Entry criteria | RTL that elaborates (any Loop C pass, converged or not — this is the loop's other half). |
| Exit criteria | Loop C's convergence criteria (§1). |
| `klt` verbs | `klt functional-verification` (shipped, Epic #391 Phase 3): cocotb via the first-party `Runner` API, Icarus or Verilator. `--options.coverage` (Verilator only), `--mutations` (#1592), `--options.sdf` (Icarus only, 13.0+). |
| Failure modes | A false pass — the failure class this stage is most exposed to, and the reason `--mutations` exists. A filtered/misspelled `testbench.testcase` never runs and never appears failed; high line coverage from few vectors reads as thorough; a `skipped` test reads as absent rather than unaddressed. Coverage is unobtainable on the Icarus leg and SDF back-annotation is unobtainable on the Verilator leg, so a block needing both needs two engines, not one. |

### D7 — synthesis (+ equivalence gate)

| | |
| --- | --- |
| Input artifact | D5's RTL sources + a resolved cell library/corner + D3's clock-period target. |
| Output artifact | `klt synthesize`'s shipped JSON ([`docs/cli/synthesize.md`](../cli/synthesize.md)): `netlist_path`, `instance_count`, `area_um2`, `sequential_area_um2`, `instance_counts_by_type`, `structural`, `warnings`, `timing`, optional `baseline` QoR delta, plus the `equivalence` object when `--verify-equivalence` is given. The native-mapper path emits `klt techmap`'s `mapped_netlist_path` ([`docs/cli/techmap.md`](../cli/techmap.md), [`synth-techmap-stage-contract.md`](synth-techmap-stage-contract.md)) in the same role. |
| Entry criteria | Loop C converged at the RTL level (D5 exit criteria) — synthesizing RTL whose regression does not pass produces a netlist nothing downstream should trust. |
| Exit criteria | `structural.has_critical: false`; the equivalence gate clears (see the decision below); `area_um2`/`instance_count` inside D2's allocation, or the overrun recorded as a Loop D input rather than absorbed. |
| `klt` verbs | `klt synthesize` (Yosys + bundled ABC, shipped); `klt techmap` (native Rust mapper, #874); `klt equiv` as the gate (see below). |
| Failure modes | `timing` is ABC's own wire-free `stime -p` critical-path estimate, not STA (issue #807) — treating it as a timing verdict skips Loop D entirely. A netlist that clears `structural` but not equivalence. A QoR win claimed without the `baseline` comparison the claim needs. |

**Recorded decision — `klt equiv` binds as a *sub-gate of the transform it
guards*, never as its own stage, and it binds twice.** Of the two options
#1956 names (a D7 sub-gate versus a standalone stage), the decision is
**sub-gate**, on three grounds:

1. **It is already shipped that way.** `klt synthesize --verify-equivalence`
   and `klt techmap --verify-equivalence` wire the combinational engine in as
   the acceptance gate of the transform itself
   ([`docs/cli/equiv.md`](../cli/equiv.md), #704 Phase 1). A standalone stage
   would describe a surface that does not exist.
2. **Its input comes from one stage and goes nowhere else.** A stage in this
   doc's sense owes an artifact to a *downstream* stage. `klt equiv` consumes
   the pre- and post-transform netlists of a single transform and produces a
   verdict nothing downstream reads — it is an admission criterion, which is
   what a gate is.
3. **A stage would have to be duplicated; a gate attaches naturally to each
   transform.** D8 changes the netlist too — CTS clock buffers,
   `repair_design`/`repair_timing` resizing, `repair_antennas` diodes — and
   emits the result as `verilog_path`. So the *same* gate belongs at D8 as
   well, proving D8's as-built netlist equivalent to D7's. `klt equiv`'s
   `"yosys-sequential"` engine (#1313) exists precisely because that is the
   transform class this repo's own P&R measurably produces: buffer insertion
   and drive-strength resizing only, zero register-count change. One gate, two
   bindings; not one stage, run twice.

**Scope limits are a stage-contract fact, not a tool bug.** The combinational
engine rejects any design containing flip-flops, latches, or memories up
front. `"yosys-sequential"` requires an identical, 1:1-by-name register set on
both sides — so **retiming, register cloning, and FSM re-encoding are
unprovable**, and there is no register-remapping field analogous to `port_map`
for a design whose register names differ
([`docs/cli/equiv.md`](../cli/equiv.md) → "Out of scope"; bounded/k-induction
model checking is a later, evidence-gated phase of #707,
[`sequential-equivalence-survey.md`](sequential-equivalence-survey.md) §4.4).
The pipeline consequence, settled here rather than left to each block:

- **D7's default configuration is one whose output is provable.** Synthesis
  options that break register correspondence are off by default, because the
  pipeline's convergence story depends on the gate being available.
- **If a block genuinely needs retiming to close timing, that is a §1 D4
  re-entry first.** Loop D's sanctioned answer to a structurally-unmeetable
  Fmax is a different microarchitecture, not an unprovable netlist.
- **If retiming is nonetheless chosen, it is an explicit recorded equivalence
  waiver**, not a silent `inconclusive`. A waiver has three obligations: the
  transform is named; D6's gate-level regression (D7's `netlist_path` as
  `sources`) becomes the *only* functional evidence for that transform and its
  coverage/mutation floors apply to it; and the waiver travels into D11 as a
  coverage-honesty disclosure per
  [`design-evidence-tiers.md`](../design-evidence-tiers.md) → "Coverage
  honesty" ("a verdict's blind spots are part of the claim and travel with
  it"). A timeout is never a waiver: `klt equiv` reports `"inconclusive"` and
  never `"equivalent"` on a timeout, and an `"inconclusive"` verdict recorded
  as if it were a waiver is a false claim.

### D8 — place & route

| | |
| --- | --- |
| Input artifact | D7's `netlist_path` (the same file, no re-derivation), a resolved LEF/liberty/GDS platform set, a floorplan spec, IO layers, clock constraints, a `seed`, and — for a block instantiating a hard macro — that macro's LEF abstract (`klt lef-abstract`). |
| Output artifact | `klt place-and-route`'s shipped JSON ([`docs/cli/place-and-route.md`](../cli/place-and-route.md)): `stage_reached`, `def_path`, `gds_path`, `verilog_path` (the as-built netlist), `spef_sta` (`spef_path`/`sdf_path`), per-stage `stages[]` metrics, `power` (PDN/tapcell/filler), `layer_map`, `def_net_names`. |
| Entry criteria | A gate-level netlist that clears D7's exit criteria. |
| Exit criteria | `stage_reached: "route"` with `gds_path` and `verilog_path` populated, **and** Loop E converged (§1), **and** Loop D converged (§1). Both loops gate this stage, which is why D8 is the only stage in two loops at once. |
| `klt` verbs | `klt place-and-route` (OpenROAD's native Tcl API, one process per stage, shipped — Epic #391 Phase 4 / Epic #700); `klt lef-abstract` ([`docs/cli/lef-abstract.md`](../cli/lef-abstract.md)) for the abstract this stage consumes for a macro and emits for a parent; `klt equiv` as the as-built gate (see D7's decision). `klt layout-metrics`/`klt render`/`klt economy` for inspection, the same role they play at S7. |
| Failure modes | Loop E's stuck conditions (§1). Silent partial completion is *not* one: `target_stage` makes "how far to go" a request input, and a successful response's `stage_reached` never falls short of it. A run without `request.power` emits no PDN/tapcells/fillers at all — a legitimate choice, but one that makes a subsequent `klt power` IR-drop verdict meaningless rather than clean. Treating a `1e+39`/`0` slack sentinel as a timing pass (see Loop D criterion 2). |

### D9 — DRC / LVS / ERC

| | |
| --- | --- |
| Input artifact | D8's `gds_path` (geometry) + D8's own `verilog_path` (the LVS reference side) + an ERC stackup/antenna spec. |
| Output artifact | Four shipped reports: `klt drc` ([`docs/cli/drc.md`](../cli/drc.md)), `klt extract` ([`docs/cli/extract.md`](../cli/extract.md)) as the layout side of the compare, `klt lvs` ([`docs/cli/lvs.md`](../cli/lvs.md)) with `reference.form: "gate-level-verilog"`, and `klt erc` ([`docs/cli/erc.md`](../cli/erc.md)). `klt power` ([`docs/cli/power.md`](../cli/power.md)) runs alongside them on the same fixed geometry. |
| Entry criteria | A routed GDS exists (any D8 pass, converged or not — this is Loop E's other half). |
| Exit criteria | Loop E's convergence criteria (§1). |
| `klt` verbs | `klt drc`, `klt extract`, `klt lvs`, `klt erc`, `klt power` — all shipped. |
| Failure modes | Comparing LVS against D7's pre-P&R netlist instead of D8's `verilog_path`: CTS buffers and resizes mean D7's netlist is *expected* to mismatch, so this produces a confident false failure. Reading `mismatch_count` instead of `status`/`error_count` (warning-severity pairing disclosures are not failures). A curated deck subset passing where the full foundry deck would not — a known fidelity gap, not a pipeline bug, and one item 3 of the tier checklist requires enumerating. Power-net/well-tie correspondence is outside the gate-level compare's scope (`verilog_path` is written without `-include_pwr_gnd`), so a clean LVS here says nothing about the PDN. |

**Why there is no free-standing digital extraction stage.** Extraction appears
twice on the digital path and is a *sub-step* both times: as `klt extract
--abstract-cells 'sky130_fd_sc_hd__*' --def-net-names`, producing the layout
side of D9's gate-level LVS compare, and inside `klt power`'s own IR-drop
model. The parasitics D10 needs come from a different source entirely — SPEF
written by D8's own engine (`spef_sta.spef_path`, `post_route_spef: true`), not
from `klt extract`. So the analog S9's output artifact has no digital consumer,
and promoting it to a stage would create a stage whose exit criteria nothing
downstream reads. (Full-custom digital is the exception, and it *does* use a
real S9 — see §5.)

### D10 — STA across corners + post-route gate-level sim

| | |
| --- | --- |
| Input artifact | D8's `def_path` (the one, fixed geometry) + the declared corner set from D3 + optionally `spef_sta.spef_path`; and, for the functional leg, D8's `verilog_path` + `spef_sta.sdf_path`. |
| Output artifact | One `klt sta` report per declared corner ([`docs/cli/sta.md`](../cli/sta.md)) plus a `klt functional-verification` report with `environment.sdf` populated. Two artifacts, not one — a timing verdict and a functional re-verification at that timing. |
| Entry criteria | Loop E converged (D9 exit criteria) — timing a DRC/LVS-dirty layout measures geometry nothing downstream should trust. |
| Exit criteria | Loop D's convergence criteria (§1), all five, evaluated on the **same** DEF across every corner. |
| `klt` verbs | `klt sta` (OpenSTA on an already-implemented design, #1099; a `verilog`-mode from-scratch netlist request added by #1825); `klt functional-verification --options.sdf` (Icarus only, 13.0+). |
| Failure modes | Loop D's stuck conditions (§1). Plus three that read as success: a nominal-corner-only claim reported as corner-closed; `spef_sta.annotation_complete: false` treated as a real-parasitics measurement (#1624); an escaped-identifier `INTERCONNECT` crashing `sdf_annotate` so the functional leg never ran (#1890) — and a stage whose second artifact is missing is not a converged stage, it is a half-measured one. |

### D11 — signoff report

| | |
| --- | --- |
| Input artifact | Converged outputs of D6, D7, D8, D9 and D10 on the **same** design generation, plus D3's original block spec. |
| Output artifact | `klt signoff`'s shipped verdict ([`docs/cli/signoff.md`](../cli/signoff.md)) — either envelope-aggregation mode (one pass/fail over a bundle) or `--manifest`/`--fleet` tier-verdict mode (per-item T1 grading against [`design-evidence-tiers.md`](../design-evidence-tiers.md)). A digital block declares `kind: "digital"` and is graded against that doc's Digital column. |
| Entry criteria | Every upstream stage converged on one design generation, not a stale mix (e.g. last week's DRC pass paired with this morning's netlist) — `klt signoff` refuses (`status: "refused"`) to combine inputs whose `provenance` blocks disagree, for the kinds it recognizes. |
| Exit criteria | Every D3 spec row has a recorded verdict; no row silently unaddressed; every equivalence waiver (D7) and deck-coverage gap (D9) disclosed with the claim. |
| `klt` verbs | `klt signoff` (shipped, #309/#1321). |
| Failure modes | See the vocabulary note below — today's dominant failure mode at this stage is not a wrong verdict but an **ungradeable** one. |

**Recorded decision — this stage uses `sta` and `functional-verification` as
its envelope-kind tokens, and #1959 has not shipped.** As of this document,
`klt signoff`'s `_classify` recognizes exactly seven native kinds — `drc`,
`lvs`, `extract`, `sim`, `yield`, `pex`, `power` — plus the opt-in
self-declared `generic` (#1152), and `_ITEM_ALLOWED_KINDS` restricts T1 item 7
globally to `pex`. **None of the digital stages' own envelopes is a recognized
kind**: `klt synthesize`, `klt place-and-route`, `klt sta`,
`klt functional-verification`, `klt equiv`, and `klt erc` outputs all render
`unrecognized_envelope` in `--manifest` mode and exit `1` in
envelope-aggregation mode. The consequence, measured rather than inferred in
[#1959](https://github.com/2AMLogic/klayout-tools/issues/1959): a `kind:
"digital"` manifest citing the Digital column's *own* artifacts grades 2/10;
its honest maximum with recognized kinds is 5/10; item 7 is structurally
unreachable, so **no RTL-flow digital block can reach `tier: "T1"` today**.

[#1959](https://github.com/2AMLogic/klayout-tools/issues/1959) is **open, not
merged**, at the time this document ships. Its curated direction is to teach
`_classify` the `sta` and `functional-verification` envelope shapes as distinct
native kinds and to make item 7's restriction per-block-kind. This document
therefore names those two kinds using exactly those tokens — `sta` and
`functional-verification`, spelled as the verbs are — so the two do not
converge on incompatible vocabularies. **If #1959 lands different tokens, this
section and §4's D11 row are what to update**, and the rest of this document is
unaffected: no stage contract above depends on the spelling. Three narrower
facts that any #1959 implementation must take as given, because they are
stage-contract facts rather than signoff-side choices:

- `klt erc` emits no `status` and no `provenance` block, so a D9 ERC verdict is
  ungradeable even once its kind is recognized.
- A D10 timing citation is only meaningful together with its corner set and its
  `spef_sta.annotation_complete` verdict — a bare "slack ≥ 0" claim can be a
  nominal-corner number (Loop D criterion 3) or an unannotated one (criterion
  4).
- Item 7's digital artifact, per the Digital column's own text, is the
  functional suite re-run against the post-route gate-level netlist with
  back-annotated SDF — **not** STA-with-parasitics. Those are different
  envelopes with different producers, and D10 above deliberately owes both.

## 3. Model-class matrix

Capability classes only, per Epic #105's constraint — no vendor model names, so
this table does not need revisiting on every model release. The same three
classes as the analog matrix, ordered by capability and cost:
**frontier-reasoning**, **mid-tier**, **small-fast**.

| Stage | Class | Rationale | Escalation rule |
| --- | --- | --- | --- |
| D1 design proposal | frontier-reasoning | Identical to S1: underspecified, open-ended requirements whose misreading compounds through every later stage. | None upward; escalate to a human after N clarification rounds fail to close the open-questions list. |
| D2 architecture & budget partition | frontier-reasoning | Identical to S2's multi-objective tradeoff, with the clock-domain graph as an extra correctness-critical output: an unnamed crossing here becomes a CDC defect no verb in §4 will catch. | Escalate to a human when no partition closes the system budget after N attempts (genuine infeasibility, not search failure). |
| D3 block spec | mid-tier | Mostly disciplined transcription of a decided allocation, with judgment on the corner set and the coverage/mutation floors Loop C and Loop D will be held to. | Escalate to frontier-reasoning when an allocation is internally inconsistent or infeasible on inspection (routes back to D2). |
| D4 microarchitecture selection | **frontier-reasoning** — *not* mid-tier like S4 | S4 is bounded search over a documented corpus; D4 has no corpus. `kb/schema/entry.schema.json`'s vocabulary is transistor-level (`sizing_approach`, `layout_idioms`) and all 21 `kb/entries/` are device-level circuits, so a digital microarchitecture choice is first-principles reasoning, and its recorded expected-critical-path claim is what Loop D is later measured against. | Already the ceiling; escalate to a human when no microarchitecture's expected critical path meets D3's Fmax at this node (that is a spec/node decision, not a design one). |
| D5 RTL authoring — *authoring pass* | mid-tier, **frontier-reasoning** for a first pass on a new block and for any RTL crossing a clock domain, implementing a protocol handshake, or handling secrets | This is the stage with no analog counterpart, and its class follows from the tooling asymmetry rather than from how "hard" RTL is: the three concerns with no mechanical checker anywhere in `klt` — CDC/synchronizer structure, ready/valid and req/ack conformance, constant-time/side-channel behavior ([`digital-review/README.md`](../guides/digital-review/README.md)'s `*(none yet)*` rows) — are the concerns whose defects survive D6 through D10 untouched. Where no tool carries the judgment, the model must. | Escalate to frontier-reasoning on Loop C's design-vs-bench oscillation or on a falling `mutation_score` (§1) — both are diagnosis failures, not authoring failures. |
| D5 RTL authoring — *repair pass* | small-fast | A repair whose defect is already *named* by a structured finding — `structural.latches`, `structural.comb_loops`, `structural.multi_driven`, a specific `warnings.by_category` entry, a specific failing `tests[].error_message` — is a bounded, mechanical edit. This split is the concrete reason the stage is not classed once: authoring carries unautomatable judgment, repair carries none. | Escalate to mid-tier when the same named finding survives N repair passes (the finding is a symptom, not the defect). |
| D6 functional verification | mid-tier to **author** a testbench; small-fast to run a regression; mid-tier to diagnose a failure | The split is sharper than S10's, because a cocotb testbench is itself code that can be wrong in a self-confirming way — every false-pass pattern in `cocotb-tb-review.md` is a testbench defect that reads as design evidence. Running `klt functional-verification` and reading `status`/`tests[]` is mechanical; deciding whether a green run *means* anything is not. | Escalate to frontier-reasoning on Loop C's stuck conditions (§1), folded into D5's rule rather than duplicated. |
| D7 synthesis (+ equivalence gate) | small-fast to invoke; mid-tier to tune QoR and to judge the equivalence gate | One request document and a `structural` read is mechanical. Choosing a `clock_period_ns`/ABC delay target, reading a `baseline` QoR delta, and — critically — deciding whether an `"inconclusive"` verdict is a timeout to re-run or a scope limit requiring the §D7 waiver, are not. | Escalate to frontier-reasoning before recording any equivalence waiver: a waiver trades a proof for a disclosure and must not be a small-fast default. |
| D8 place & route | mid-tier | Mapping a netlist onto floorplan parameters (utilization, aspect ratio, IO layers, PDN strap geometry, site) is layout-idiom judgment even when the engine is mechanical — the same reasoning S7 gets. | Escalate to frontier-reasoning when no floorplan closes both loops (a floor-plan-level decision, not a parameter tweak), and to a human before accepting a seed-dependent result (Loop D). |
| D9 DRC / LVS / ERC | small-fast to fix; **mid-tier earlier than S8 escalates**, to triage | Rule-by-rule DRC fixing against an itemized report is near-mechanical, as at S8. But a digital gate-level LVS mismatch's *first* question is "is this real" — the 1324-vs-`match` case in §1 was entirely a reference-construction methodology artifact on identical geometry — and that is diagnosis, not repair. | Escalate to mid-tier on the *first* LVS mismatch whose category profile is `topology`/`net.split`-dominated, not after N passes; escalate to frontier-reasoning on Loop E's tradeoff case. |
| D10 STA + post-route gate-level sim | small-fast to invoke; mid-tier to interpret and to scope corners | Running `klt sta` per corner is mechanical. Deciding the corner set, reading `timing_status` against a `1e+39` sentinel, and checking `annotation_complete` before believing a number are judgment — and each has a shipped false-success path (§1's Loop D criteria 2–4). | See D7/D8 — Loop D's escalation lives with its remedy sides, not with its measurement. |
| D11 signoff report | small-fast | Templated aggregation of already-converged, already-structured JSON; the checks were done upstream. | Escalate to mid-tier or frontier-reasoning when aggregation surfaces a spec row with no upstream check (the signoff-rejection backtrack, §1), or when an `unrecognized_envelope` result is about to be worked around by citing a *different* kind's passing check for an item it does not actually prove — that substitution is a judgment call about claim honesty, not a formatting decision. |

## 4. Gap map

| Stage | Tool support today | Gap / tracking issue |
| --- | --- | --- |
| D1 design proposal | None — intentionally out of tool scope (free-form intake). | — |
| D2 architecture & budget partition | None. | Shared with S2; no friction issue filed. A digital-specific sub-gap: nothing records or checks the clock-domain graph this stage decides. |
| D3 block spec | None; a ratified spec lives in the block repo's README and parses through `parse_spec_summary()` with no code change. | **The same gap as S3, not a second one** — see D3's recorded decision. It blocks `klt signoff` from diffing verdicts against spec rows on both paths. |
| D4 microarchitecture selection | `klt kb` ships, but its schema and all 21 corpus entries are transistor-level — there is no digital-microarchitecture vocabulary to query. | **Real digital-only gap, no friction issue filed.** Unlike S4 (where only corpus breadth remains), D4 would need a schema extension or a sibling corpus, which is a design decision this document does not make. |
| D5 RTL authoring | Agent-side by construction. The only mechanical checks are neighbours': `klt synthesize`'s `structural` (#1588) and `warnings` (#1605). | CDC/synchronizer structure, protocol conformance, and constant-time/side-channel behavior have **no checker at all** ([`digital-review/README.md`](../guides/digital-review/README.md)'s `*(none yet)*` rows) — the widest gap on the digital path, and the reason §3 classes this stage as it does. |
| D6 functional verification | Shipped. `klt functional-verification` (cocotb + Icarus/Verilator), `--options.coverage` (#391 Phase 3), `--mutations` (#1592/#1593), `--options.sdf` (#1002). | Coverage is Verilator-only and SDF is Icarus-only, so a block needing both runs two engines. No mechanical check that a `testbench.testcase` filter names a real test. |
| D7 synthesis (+ equivalence gate) | Shipped. `klt synthesize` (Yosys + ABC), `klt techmap` (native Rust, #874), `klt equiv` as the gate (#704 Phase 1). | `timing` is ABC's wire-free estimate, not STA (#807, by design — Loop D owns timing). Sequential equivalence is register-correspondence only, so retiming/cloning/FSM re-encoding are unprovable (#1313; bounded model checking is a later phase of #707) — a stage-contract fact, settled in D7's decision, not a bug. |
| D8 place & route | Shipped. `klt place-and-route` (OpenROAD, floorplan→place→cts→route, PDN/tapcell/filler via `request.power`, `verilog_path`, SPEF/SDF export); `klt lef-abstract` for the macro seam. | — |
| D9 DRC / LVS / ERC | Shipped. `klt drc`, `klt extract` (`--abstract-cells`/`--def-net-names`), `klt lvs` (`reference.form: "gate-level-verilog"`, #1336), `klt erc`, `klt power`. | `klt erc` emits no `status` and no `provenance`, so its verdict cannot be graded at D11 even once its kind is recognized. Power-net/well-tie correspondence is outside the gate-level LVS compare's scope. |
| D10 STA + post-route gate-level sim | Shipped. `klt sta` (OpenSTA per corner on one fixed DEF, #1099; `verilog`-mode #1825); `klt functional-verification --options.sdf`. | No contract for *which* corners constitute the declared set (#1959 finding 2 — the −14.683 ns vs +6.98 ns case); `spef_annotation` false-positive (#1624); `sdf_annotate` crashes on escaped `INTERCONNECT` identifiers (#1890). |
| D11 signoff report | Partial. `klt signoff` aggregates and tier-grades `drc`/`lvs`/`extract`/`sim`/`yield`/`pex`/`power` + `generic`. | **No digital stage's own envelope is a recognized kind** — `synthesize`/`place-and-route`/`sta`/`functional-verification`/`equiv`/`erc` all render `unrecognized_envelope`, and item 7's global `pex` restriction makes T1 structurally unreachable for an RTL-flow digital block. → **#1959** (open). |

**Reading the map.** The digital path's *loops* are fully tooled: Loop C
(`klt functional-verification` + `klt synthesize`'s `structural`), Loop D
(`klt sta` + `klt place-and-route` + `klt synthesize`), and Loop E (`klt drc` +
`klt extract` + `klt lvs` + `klt erc` + `klt power`) each have every verb they
need shipped, and all three have been driven on a real block (§5's canary
walk). What remains unbuilt is the same shape as the analog path's residue:
the **bracketing** stages. At the front, D2/D3 have no tool (shared with S2/S3,
by design) and D4 has no queryable corpus (a real digital-only gap). At the
back, D11's aggregation ships but recognizes none of the digital stages'
evidence (#1959). One gap is genuinely digital-specific and genuinely wide:
D5's three uncheckable concerns — CDC, protocol, side-channel — where the
review guides are the *only* line of defense and no tool is proposed.

## 5. Variants: full-custom digital and mixed-signal

### Full-custom digital

[`design-evidence-tiers.md`](../design-evidence-tiers.md) carves out a
full-custom digital sub-case: where no compatible open standard-cell library
exists for the PDK/voltage combination in use, every logic gate is
hand-captured as a schematic and verified via SPICE + PVT sweep — no RTL, no
synthesis. That doc makes it a **sub-case of the Digital column**, not a third
column or a new block `kind` (#1190/#636), so `klt signoff`'s parser and its
`_BLOCK_KINDS` enum need no change.

**Settled: a full-custom digital block does not enter at D5. It diverges after
D4, walks the analog S5–S7 in place of D5–D8, rejoins at D9 unchanged,
substitutes S10 for D10, and rejoins at D11.** It is *not* a digital block
that skips a step; it is a digital block whose middle is the analog pipeline.
That conclusion is forced rather than chosen: the tier doc's own full-custom
substitutions for items 1, 2, 5, and 7 are, one for one, the analog column's
artifacts, and a doc that placed full-custom "at D5" would then have to
re-derive every one of them.

| Digital stage | Full-custom substitute |
| --- | --- |
| D1 – D4 | **Unchanged.** Still a digital block: proposal, budget partition, block spec, and a gate-level-structure selection at D4. |
| D5 RTL authoring | **S6 schematic/netlist** (every logic gate captured as a schematic) **+ S5 sizing** (gate drive strength / device sizing). Tier item 1's full-custom text names exactly this substitution. |
| D6 functional verification | **S10 via `klt sim`** — a logic-function testbench in SPICE. Not cocotb: there is no HDL model to drive. |
| D7 synthesis (+ equiv gate) | **No counterpart — deleted.** There is no netlist transform to prove equivalent; the captured schematic *is* the gate netlist. This is the one stage that vanishes rather than substitutes, and it is why `klt equiv` is irrelevant to this variant. |
| D8 place & route | **S7 layout generation.** Tier item 2's full-custom text: hand-drawn (or generated) layout, reproducibly produced or with documented provenance — not P&R output. |
| D9 DRC / LVS / ERC | **Unchanged, and applies as written** — DRC/LVS/ERC are geometry checks indifferent to how the geometry arrived. One addition: **S9 extraction becomes a real, free-standing stage here**, because there is no P&R engine writing SPEF (see D9's note). |
| D10 STA + post-route GLS | **S10 PVT corner-matrix SPICE**, plus the explicit per-PVT timing-margin metric tier item 5's full-custom text requires (a SPICE-measured analog of setup/hold margin), plus **`klt pex`** for item 7's post-layout leg. No STA: there is no liberty for hand-drawn gates, which is the same absence that forced full-custom in the first place. |
| D11 signoff report | **Unchanged in shape**, aggregating the analog evidence set — which is exactly why a full-custom digital block is the *one* digital block that can satisfy item 7's `pex` restriction today, while an RTL-flow block cannot (#1959). |

Two consequences worth stating, because they are easy to get wrong:

- **Loops C and D collapse into analog Loop A; Loop E is literally Loop B.**
  A full-custom block's functional and timing evidence are both measurements
  from the same SPICE corner sweep, so the RTL⇄verification and
  synthesis⇄timing loops are one sizing⇄simulation loop. Its layout⇄DRC/LVS
  loop is Loop B with an ERC leg added. None of Loop C's or Loop D's
  digital-specific stuck detectors (mutation-score inversion, corner-chasing,
  seed dependence) apply; Loop A's and Loop B's do.
- **The block still declares `kind: "digital"`.** The divergence is in the
  pipeline it walks, never in its declared kind — consistent with the tier
  doc's own structural choice, and with `signoff.py`'s `_BLOCK_KINDS`. A
  full-custom block declaring `kind: "analog"` to make its evidence fit would
  be claiming something different from what it built.

### Mixed-signal

A mixed-signal block runs **both** pipelines, one per partition — the digital
partition through D1–D11 (or the full-custom substitute above), the analog
partition through S1–S11 — meeting at exactly two places:

- **D2/S2 is the shared stage.** The partition boundary is a budget-partition
  decision, and [`design-evidence-tiers.md`](../design-evidence-tiers.md)
  requires the claim to state that boundary explicitly (which nets/pins/cells
  belong to which side) so a reviewer can tell which evidence covers which
  silicon. That statement is D2's output, not a signoff-time annotation.
- **D11/S11 is the joining stage.** A `kind: "mixed-signal"` manifest satisfies
  both columns, one per partition, and its item-7 grading applies per partition
  — the analog side needs `pex`, and the digital side needs whatever #1959
  settles on (per that issue's own edge case, a mixed-signal manifest's digital
  partition must apply the same per-block-kind item-7 rule a pure `digital`
  manifest does).

Between those two points the paths are separate, with two named seam artifacts:
`klt lef-abstract` ([`docs/cli/lef-abstract.md`](../cli/lef-abstract.md)) turns
an analog macro into an abstract D8 can place against, and cross-domain
co-simulation is **spiked but not shipped**
([`co-simulation-approach-survey.md`](co-simulation-approach-survey.md), Epic
#393) — so a mixed-signal block today verifies each partition against its own
column and has no tool for the crossing itself. Nothing in this section
contradicts the tier doc's mixed-signal carve-out; it names which pipeline each
partition walks, which that doc does not.

## 6. Where #1956's four open questions are settled

The issue that commissioned this document named four questions the doc would
have to settle rather than restate. Index:

| # | Question | Settled in | Verdict in one line |
| --- | --- | --- | --- |
| 1 | Where does `klt equiv` bind — a D7 sub-gate or its own stage? | §2, D7's recorded decision | **Sub-gate, and it binds twice** (D7's netlist transform and D8's as-built transform), never a stage. Its register-correspondence scope limit makes retiming/cloning/FSM re-encoding unprovable, which is a stage-contract fact: D7 defaults to a provable configuration, an unmeetable Fmax re-enters D4 first, and retiming requires an explicit recorded equivalence waiver with three named obligations. A timeout (`"inconclusive"`) is never a waiver. |
| 2 | Model classes per stage, including D5, which has no S5/S6 counterpart. | §3 | Full matrix. D4 is **frontier-reasoning** (not S4's mid-tier — no corpus to query). D5 **splits**: authoring is mid-tier, frontier for CDC/protocol/secret-handling and for a first pass; repair is small-fast when a structured finding names the defect. D6 splits three ways (author / run / diagnose). D9 escalates to mid-tier on the *first* `topology`-dominated LVS mismatch rather than after N passes. |
| 3 | What is the digital S3 — a schema, or the same open gap? | §2, D3's recorded decision + §4's D3 row | **The same open gap, not a second one.** Both paths' ratified specs live in the block README and parse through `parse_spec_summary()` unchanged; the gap is purely the absent schema. This doc files no digital-specific block-spec proposal and reuses `klt.pipeline.blockspec/1`. The one digital consequence is downstream: digital is two gaps from a machine-checked spec verdict (no schema *and* no recognized evidence kind) where analog is one. |
| 4 | Where does full-custom digital enter? | §5 | **Not at D5.** It diverges after D4, walks analog S5–S7 in place of D5–D8 (D7 vanishes entirely — no transform to prove), rejoins at D9 unchanged with S9 promoted to a real stage, substitutes S10 for D10, and rejoins at D11. Loops C and D collapse into Loop A; Loop E is Loop B plus ERC. The block still declares `kind: "digital"`. |

A fifth item, added by the curator rather than the original issue — that D11's
vocabulary must not diverge from #1959's — is settled in D11's recorded
decision: this doc uses the `sta` and `functional-verification` tokens, and
explicitly flags that #1959 is **open, not merged**, with D11 and §4's D11 row
named as the two places to update if it lands different tokens.

## 7. Walked against a real digital canary

The stage graph above is checked against `sky130-modexp` (an RSA-style
square-and-multiply modular-exponentiation accelerator, `WIDTH = 16`), the
canary Epic #700 names as "the cleanest digital target." Its in-repo evidence
is [`tests/corpus/sky130_modexp_canary/`](../../tests/corpus/sky130_modexp_canary/README.md)
(`run_validation.py` + a committed `results.json` snapshot) and
[`sky130-modexp-canary-signoff-status.md`](sky130-modexp-canary-signoff-status.md);
its own repo carries the design of record.

Every stage that canary actually performed maps onto exactly one stage above,
with no step left undocumented:

| Canary step | Stage | Evidence |
| --- | --- | --- |
| `rtl/modexp.v` authored, `WIDTH=16`, vendored at commit `1b38ab4` | D5 | `sources/modexp.v` |
| cocotb regression (`test_modexp.py`) | D6 | `examples/functional-verification/test_modexp.py` |
| `klt synthesize`, `sky130_fd_sc_hd`/`tt_025C_1v80` | D7 | `instance_count: 717`, `area_um2: 7144.352` |
| `klt place-and-route`, `utilization_pct: 35`, `aspect_ratio: 1`, `core_margin_um: 4`, `site: unithd`, IO `met3`/`met2`, `clock_period_ns: 10.0`, `seed: 42`, `target_stage: "route"` | D8 | `stage_reached: "route"`, `die_area_um2: 22761.8`, `utilization_pct: 37.03`, `wirelength_um: 19412` |
| `klt drc --deck sky130` on the merged routed GDS | D9 | `status: "clean"`, `violation_count: 0` |
| `klt extract --abstract-cells 'sky130_fd_sc_hd__*' --def-net-names` | D9 (LVS's layout side — *not* a stage of its own) | `net_count: 1221`, `pin_count: 753` |
| `klt lvs`, `reference.form: "gate-level-verilog"` vs. the same run's `verilog_path` | D9 | `status: "match"`, `error_count: 0`, `mismatch_count: 4` (warning-severity) |
| In-flow timing at the nominal corner | D10, **partially** | `fmax_mhz: 180.292`, `worst_slack_ns: 4.45344`, setup/hold violations `0`/`0`, antenna violations `0` |
| ORFS plain-OpenROAD ground-truth comparison | *(not a pipeline stage — a tool-validation exercise)* | [`sky130-modexp-canary-openroad-ground-truth.md`](sky130-modexp-canary-openroad-ground-truth.md) |

Four findings from the walk, all of which the graph above already accounts for
because the walk is what produced them:

1. **The absorbed S9 is right.** Extraction appears only as the layout side of
   the LVS compare, exactly as D9's note describes. Nothing consumed an
   extracted netlist as a stage output.
2. **Loop E's digital detector is the canary's own history.** The 2026-08-16
   attempt reached `status: "mismatch"` with 1324 mismatches building its
   reference by hand; the same design and geometry reach `status: "match"`
   through `reference.form: "gate-level-verilog"` (#1336). Zero P&R passes
   separate those numbers — the insensitivity detector in §1 is transcribed
   from this case, not hypothesized.
3. **`mismatch_count: 4` with `status: "match"` is why Loop E's exit criterion
   cannot say "zero mismatches."** The analog doc's "clean device/net compare"
   phrasing does not transcribe to a gate-level compare.
4. **D10 is the one stage this canary has not completed**, and the gap is
   exactly the one Loop D criterion 3 names: the recorded timing numbers are
   nominal-corner, in-flow `place-and-route` by-products, not a declared
   multi-corner `klt sta` sweep on the fixed DEF, and no `--options.sdf`
   back-annotated functional leg exists. That is a real canary gap, and the
   stage graph's job here is to make it visible as one rather than let a
   `worst_slack_ns: 4.45` reading stand in for timing closure.

**What this walk is not.** No new `klt place-and-route`, `klt drc`, `klt lvs`,
or `klt sta` invocation was performed while writing this document; the mapping
is checked against the committed `results.json` snapshot and the two design
docs above. A live re-run needs a provisioned OpenROAD + sky130A host and is
`loom:operator-only` work in the canary's own repo, per
[`sky130-modexp-canary-signoff-status.md`](sky130-modexp-canary-signoff-status.md).

## Out of scope for this doc

No `klt` subcommand, JSON schema file, dependency, or `.claude/skills/` entry
is added here, and no shipped contract is changed. Unlike the analog doc, this
one proposes **no new schema names at all** (§2's recorded decision) — D1–D4
reuse the analog doc's four proposals, which remain proposals for review rather
than reservations against a shipped registry.

Three things this document deliberately does not do:

- **It does not design the escalation mechanism.** §3 gives the per-stage rule
  an orchestrator would consult, not how a model-class handoff is invoked.
- **It does not build the per-stage skills.** Epic #105 Phase 2 built one
  agent-loadable skill per analog stage under `.claude/skills/`
  (`design-proposal-intake`, `design-block-spec`, `design-sizing`,
  `design-drc-lvs`, …). No digital equivalents exist, and creating them is a
  separate, later decision — this doc is the contract such skills would load,
  in the same relationship Phase 1 had to Phase 2 on the analog path.
- **It does not resolve the gaps in §4.** D4's missing corpus, D5's three
  uncheckable concerns, and D11's digital-kind vocabulary (#1959) are recorded
  as gaps with the reasoning that makes them gaps, not proposals to close them.

## Related

- [`design-pipeline.md`](design-pipeline.md) — the analog path (Epic #105), the
  structural template this document mirrors.
- [`digital-flow-contracts-spike.md`](digital-flow-contracts-spike.md) (#399) —
  the three digital-flow JSON contracts D6/D7/D8 are built on.
- [`synth-techmap-stage-contract.md`](synth-techmap-stage-contract.md) (#873) —
  D7's native technology-mapping stage contract.
- [`sequential-equivalence-survey.md`](sequential-equivalence-survey.md) —
  where D7's equivalence scope limit comes from, and what a later phase of #707
  would extend it to.
- [`docs/guides/digital-review/README.md`](../guides/digital-review/README.md) —
  the per-concern review bar applied to D5's and D6's outputs, and the source
  of §4's "no checker at all" rows.
- [`design-evidence-tiers.md`](../design-evidence-tiers.md) — the T1 Digital
  column D11 grades against, and the full-custom/mixed-signal carve-outs §5
  navigates.
- [#1959](https://github.com/2AMLogic/klayout-tools/issues/1959) — the open
  issue that makes D11's digital evidence ungradeable; D11's recorded decision
  names the vocabulary both should share.
- [`tests/corpus/sky130_modexp_canary/README.md`](../../tests/corpus/sky130_modexp_canary/README.md)
  and [`sky130-modexp-canary-signoff-status.md`](sky130-modexp-canary-signoff-status.md)
  — the canary walk in §7.
