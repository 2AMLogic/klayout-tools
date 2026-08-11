# Design: staged agent design pipeline

**Status:** design doc / proposal. This is Phase 1 of
[Epic #105](https://github.com/2AMLogic/klayout-tools/issues/105) — the
single source of truth an agent (or an orchestrator assigning agents) loads
to know what stage it is in, what artifact it owes, and when it is allowed
to call the stage done. Phase 2 of the epic builds one agent-loadable skill
per stage under `.claude/skills/`, each a thin loader of the contract
defined here; Phase 3 proves the whole thing by driving one sky130 block
through it end to end. Nothing here is implementation — no `klt` subcommand,
schema file, or skill is added by this document.

Per [docs/ARCHITECTURE.md](../ARCHITECTURE.md):

> An agent can take a spec → schematic/generator → sized circuit → layout →
> DRC/LVS clean → extracted netlist → simulation-verified, on an open PDK,
> unaided — with every step headless and JSON-contracted.

That sentence names the stages but not how to move between them. This
document is the decomposition: eleven stages, the loops between them, a
proposed input/output contract per stage, which class of model should run
each one, and an honest accounting of which stages `klt` already serves
versus which still await tooling.

## Scope: skills are procedure, not strategy

This doc — and the skills it will feed — defines **navigation**: what
artifact a stage consumes and produces, when a stage is entered and exited,
which `klt` verb to call, and what a stuck loop looks like versus a
converging one. It does not define **strategy within a stage** — how to
choose a compensation scheme for an amplifier, how to trade area against
power in a budget partition, how to decide which of three converging sizing
candidates is best. That is the job of the LLM reasoning module named in
ROADMAP.md's Phase 5 ("MCP server exposing the toolkit; LLM reasoning
module for layout decisions (strategy in the model, geometry in the
tools)") and, per [docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "In-house
prior art," the sibling that already draws this same line in kicad-tools.

Concretely: a stage's skill tells an agent "you are in the sizing stage,
your input is a topology + spec, your output is a sized netlist consumable
by `klt sim`, your exit criterion is N consecutive corner-clean sim runs,
and here is the escalation rule if you're stuck." It does not tell the
agent *how* to size a two-stage Miller-compensated OTA — that reasoning
(possibly KB-assisted, possibly model-native) happens inside the stage, is
out of scope here, and is exactly the seam the reasoning module fills. A
skill that starts prescribing circuit strategy has drifted out of its lane
and into the reasoning module's.

## 1. Stage graph

```
 S1  design proposal
       |
 S2  system architecture & budget partition
       |
 S3  block specs
       |
 S4  topology selection (KB-assisted, `klt kb`)
       |
 S5  sizing            <--loop A: sizing <-> simulation--+
       |                                                  |
 S6  schematic/netlist                                    |
       |                                                  |
 S7  layout generation  <--loop B: layout <-> DRC/LVS--+   |
       |                                                |  |
 S8  DRC/LVS  --------------------------------------->--+  |
       |                                                   |
 S9  extraction                                            |
       |                                                   |
 S10 simulation across corners  ------------------------->-+
       |
 S11 signoff report
```

The chain is drawn linear because that is the direction of a *successful*
run, but two loops are structural, not exceptional — an orchestrator must
recognize them as iteration, not failure, and must also recognize when
iteration has become stuck:

### Loop A — sizing ↔ simulation

- **Members:** S5 (sizing) and S10 (simulation across corners), with S6-S9
  as the pass-through path between them (a sized netlist must still be
  elaborated, laid out, and extracted before it can be corner-simulated
  post-layout — but the *first* passes of this loop legitimately run
  schematic-level, S5 → S6 → S10 directly, skipping S7-S9, to get a fast
  pre-layout sizing signal before paying for a layout iteration).
- **Iterating:** each pass adjusts device sizes/biasing in response to
  which `klt sim` measurements (`docs/design/spice-corner-runner-spike.md`
  → "Proposed JSON contract," `measurements[].status`) failed and by how
  much (`margin`).
- **Exit criteria (converged):** every declared measurement's `status` is
  `pass` across the full declared corner matrix (`klt sim`'s aggregate
  `status: "pass"`), for a stable candidate — i.e. the same sizing produces
  a clean sweep, not a single lucky corner set.
- **Stuck, not iterating:** margins are not monotonically improving across
  N consecutive passes (oscillating or worsening), the same measurement is
  the worst-case offender across N passes with no change in remedy, or a
  sizing change fixes one measurement while regressing another by a larger
  margin (a genuine tradeoff, not a bug) — this is the named escalation
  trigger in the model-class matrix below (§3).

### Loop B — layout ↔ DRC/LVS

- **Members:** S7 (layout generation) and S8 (DRC/LVS).
- **Iterating:** each pass edits geometry (or generator parameters) in
  response to `klt drc`'s violation list (`docs/cli/drc.md`) and `klt
  lvs`'s netlist-mismatch reports (`docs/cli/lvs.md`).
- **Exit criteria (converged):** `klt drc` reports zero violations and LVS
  reports a clean device/net compare — the literal "DRC/LVS clean" clause
  of the vision sentence.
- **Stuck, not iterating:** violation count is not monotonically
  decreasing across N consecutive passes, the same rule fires after being
  "fixed" (a fix that moves the violation rather than resolving it), or a
  DRC fix breaks LVS correspondence (or vice versa) — the two checks
  fighting each other is the layout-stage analogue of Loop A's tradeoff
  case.

### Non-loop backtracking (named, not modeled in detail)

Two escapes are real but out of this doc's detail level, flagged so an
orchestrator does not mistake them for a broken stage machine:

- **Spec infeasibility.** Sizing (S5) or layout (S7) discovering the block
  spec (S3) is unmeetable is a legitimate outcome, not a failure of S5/S7 —
  it re-enters at S3 (or S2, if the budget partition itself needs
  renegotiating), with the discovered constraint as new input.
- **Signoff rejection.** S11 finding a corner or a metric that earlier
  stages' own checks didn't catch (e.g. a system-level spec S3 never
  encoded) re-enters at whichever stage owns that spec.

Both are **N-limited escalations to a human or to the frontier tier**, not
infinite backtracking — the same discipline the two named loops use.

## 2. Per-stage contracts

Field conventions match `docs/json-contract.md`: `schema_version` alongside
flat top-level fields, new fields additive, unknown fields ignored. The
schema names below (`klt.pipeline.<stage>/1`) are **proposals for review**,
the same status the spike gives its own contract in
`docs/design/spice-corner-runner-spike.md` — none of them are shipped, and
none is authorized by this document. Where a stage already has a real,
shipped contract (S8's `klt drc`, S10's `klt sim`), this section defers to
that command's own doc rather than re-describing it.

### S1 — design proposal

| | |
| --- | --- |
| Input artifact | Free-form spec: prose requirements, a target application, a reference to a KB entry or published design. |
| Output artifact | `klt.pipeline.proposal/1` (proposed): normalized problem statement — target function, top-level specs as known, constraints, open questions. |
| Entry criteria | Always available — the pipeline's start state. |
| Exit criteria | Every top-level spec field is either a number/range or explicitly marked "to be resolved in S2/S3"; no silent gaps. |
| `klt` verbs | None — this stage is pure intake, out of tool scope by design. `klt kb search`/`show` ([docs/cli/kb.md](../cli/kb.md), shipped) may inform what's achievable. |
| Failure modes | Underspecification passed downstream as if resolved (the single most expensive failure to catch late); scope too broad for the pipeline's target (full-chip digital — an explicit ROADMAP.md non-goal). |

### S2 — system architecture & budget partition

| | |
| --- | --- |
| Input artifact | S1's normalized proposal. |
| Output artifact | `klt.pipeline.architecture/1` (proposed): block list with interfaces, and a budget partition (power, area, noise, and other system specs allocated per block) that sums to the system-level target. |
| Entry criteria | S1 output has no unresolved top-level spec fields. |
| Exit criteria | Every block has a complete budget allocation; allocations are internally consistent (they close against the system spec, not just against each other). |
| `klt` verbs | None currently — no optimization/partition tool exists (see §4). |
| Failure modes | Partition that doesn't close (allocations sum past the system budget); a block given an allocation later stages prove infeasible (routes back per §1's non-loop backtracking). |

### S3 — block specs

| | |
| --- | --- |
| Input artifact | One block's budget allocation from S2. |
| Output artifact | `klt.pipeline.blockspec/1` (proposed): per-block spec document — electrical specs, interface/pinout, process/environment targets, area/power ceiling. |
| Entry criteria | Block has a closed budget allocation from S2. |
| Exit criteria | Spec is complete enough to select a topology against — every field S4 needs is present or explicitly deferred to S4's own judgment. |
| `klt` verbs | None currently. |
| Failure modes | Spec silently narrower than the KB entries it will be matched against (mismatched units, missing corner/PVT range); spec copied from S2 without resolving system-level assumptions (e.g. an implicit supply voltage) into block-local terms. |

### S4 — topology selection (KB-assisted)

| | |
| --- | --- |
| Input artifact | S3's block spec. |
| Output artifact | `klt.pipeline.topology/1` (proposed): selected topology reference (a KB `id` when matched, or a description when none matches), rationale, and the spec fields the choice was made against. |
| Entry criteria | Block spec is complete per S3's exit criteria. |
| Exit criteria | A topology is chosen and its known limitations against the spec (if any) are recorded, not silently absorbed. |
| `klt` verbs | `klt kb search`/`show`/`list` ([docs/cli/kb.md](../cli/kb.md), shipped — Epic #102 Phase 2, #110, closed) query the KB corpus this stage matches a topology against; the remaining work is corpus breadth, not the query surface. |
| Failure modes | No KB entry matches and the agent proceeds on an unvalidated topology without flagging it; a topology chosen for KB familiarity rather than spec fit. |

### S5 — sizing

| | |
| --- | --- |
| Input artifact | S4's topology + S3's block spec. |
| Output artifact | `klt.pipeline.sizing/1` (proposed): device parameter set (W/L, multiplier, bias currents, passive values) bound to the topology, plus the last sim result that produced it. |
| Entry criteria | Topology selected (S4). |
| Exit criteria | Loop A's convergence criterion (§1): every declared measurement passes across the declared corner matrix for a stable candidate. |
| `klt` verbs | `klt sim` for corner feedback (schematic-level passes of Loop A, §1); `klt size` (Epic #705 Phase 0, issue #721) for the single-device gm/Id sub-case — solving one device's width from a gm/Id target and current budget, `ngspice`-scored. A general multi-device parameter-optimizer verb still does not exist — the #310 decision below (agent-side, not by omission) stands for that broader case; #705 is that decision's re-trigger for the narrower single-device lookup, not a reversal of it. |
| Failure modes | Loop A's stuck condition (§1: non-monotonic margins, unresolved tradeoff, oscillation); local optimum that clears every declared measurement but is fragile to a corner not swept (an incompleteness in S3, surfacing here). |

**Recorded scope decision (#310, 2026-08-09): candidate proposal stays
agent-side.** Of the three options weighed — a generic parameter-optimizer
verb (`klt optimize`, mirroring kicad-tools' CMA-ES placement optimizer),
an S5-specific sizing helper, or remain-manual — the decision is
**remain-manual for now**. The only real Loop A run in this repo (the
sky130 5T OTA canary, Epic #153 phase 4;
[`examples/design-pipeline/05-sizing.json`](../../examples/design-pipeline/05-sizing.json)'s
`loop_a_history`) converged in two passes and changed **no device sizes**:
pass 1's `gain_db` failure was a testbench-construction artifact, diagnosed
and fixed in S6. A numeric proposer handed that pass's negative margin
would have searched device sizes against an artifact — the wrong answer,
arrived at faster. Loop A has so far been *diagnosis*-bound, not
*search*-bound, and an optimizer only pays off in the search-bound regime;
building one now would also mean designing its cost contract with no real
cost function to validate it against.

**Re-trigger — spike an optimization epic** (per
[`docs/ARCHITECTURE.md`](../ARCHITECTURE.md) → "How capabilities arrive")
when a real block's Loop A reaches its escalation N (3-5 passes, per
`.claude/skills/design-sizing/SKILL.md`) with **every** pass actually
changing device parameters and the stuck-loop checklist's diagnosis-side
causes ruled out — i.e. demonstrated search thrash, not a mis-characterized
measurement. Attach that `loop_a_history` to #310; it is the evidence the
epic would be scoped from. Two notes for whoever scopes it: the cost
signal already exists in `klt sim`'s response (per-measurement `margin`,
signed, rolled up to `worst_case` across the corner matrix), and a *generic*
optimizer verb would want to land as an adopter of Epic #253 Phase 3's
generic job type rather than inventing a second fan-out shape.

### S6 — schematic/netlist

| | |
| --- | --- |
| Input artifact | S5's sized device parameter set. |
| Output artifact | A SPICE netlist (schematic-level, pre-layout) — the same `netlist` reference field `klt sim`'s request contract already expects (`docs/design/spice-corner-runner-spike.md` → "Request"). |
| Entry criteria | Sizing candidate produced by S5, even if not yet Loop-A-converged (schematic-level sim passes need a netlist to run against). |
| Exit criteria | Netlist elaborates cleanly (parses, every device model resolves against the target PDK) and its topology matches S4/S5's intent (no missing/extra elements). |
| `klt` verbs | None currently — schematic capture (xschem) is out-of-repo per current scope; the export/staleness-check gap is #55 (see §4). |
| Failure modes | Netlist silently diverges from the sized schematic it claims to represent (the staleness problem #55 names directly); a block netlisted as a testbench rather than an includable subcircuit (also #55). |

### S7 — layout generation

| | |
| --- | --- |
| Input artifact | S6's netlist + S5's sized parameters (a generator needs both topology/connectivity and device sizes). |
| Output artifact | GDSII/OASIS layout stream, plus a structured report (ports, bounding box, DRC-relevant metadata) — the shipped shape documented in `docs/cli/gen.md`/`gen-compose.md`. |
| Entry criteria | Netlist elaborates cleanly (S6 exit criteria met). |
| Exit criteria | Loop B's convergence criterion (§1): zero DRC violations and clean LVS. |
| `klt` verbs | `klt gen`/`klt gen-compose` ([docs/cli/gen.md](../cli/gen.md), [docs/cli/gen-compose.md](../cli/gen-compose.md), shipped — #104, closed) generate/compose layout from a netlist + parameters; `klt render` to visually inspect a generated/edited layout; `klt layout-metrics` for area/utilization. |
| Failure modes | Loop B's stuck condition (§1); a generator producing DRC-clean geometry whose connectivity doesn't match S6 (an LVS failure masquerading as a DRC pass). |

### S8 — DRC/LVS

| | |
| --- | --- |
| Input artifact | S7's layout stream (+ S6's netlist, for the LVS half). |
| Output artifact | DRC: `klt drc`'s shipped JSON report (`docs/cli/drc.md`). LVS: `klt lvs`'s shipped device/net compare report (`docs/cli/lvs.md`). |
| Entry criteria | A layout stream exists (any pass of S7, converged or not — this is the loop's other half). |
| Exit criteria | Loop B's convergence criterion (§1). |
| `klt` verbs | `klt drc` ([docs/cli/drc.md](../cli/drc.md), shipped); `klt lvs` ([docs/cli/lvs.md](../cli/lvs.md), shipped — phase 3 of Epic #153, #54 closed). |
| Failure modes | DRC-clean but LVS-dirty (or the reverse, once LVS exists) — Loop B's tradeoff case (§1); a curated DRC deck subset (`docs/cli/drc.md` → "Coverage") passing while the full foundry deck would not — a known fidelity gap, not a pipeline bug. |

### S9 — extraction

| | |
| --- | --- |
| Input artifact | S8-clean layout stream. |
| Output artifact | Extracted netlist (devices + connectivity, optional RC parasitics via `--parasitics`) — the artifact that makes the S10 pass here "post-layout" rather than schematic-level. Contract documented in `docs/cli/extract.md`. |
| Entry criteria | Loop B converged (S8 exit criteria met) — extracting a DRC/LVS-dirty layout produces a netlist nothing downstream should trust. |
| Exit criteria | Extracted netlist elaborates cleanly and its device/net topology matches the S6 netlist it was extracted from (this is itself an LVS-shaped check, and per the spike's own open question, `environment` should record which side of the loop — schematic vs. extracted — a given netlist represents). |
| `klt` verbs | `klt extract` ([docs/cli/extract.md](../cli/extract.md), shipped — Epic #153; extraction and LVS share the `pya.LayoutToNetlist`/`pya.NetlistComparer` engine, hence the common lineage with `klt lvs`). `--parasitics` adds RC extraction (#216/#217, closed). |
| Failure modes | Parasitics that flip a measurement's pass/fail relative to the schematic-level S5 result — the entire reason S10 must run again post-extraction rather than trusting the pre-layout sizing pass. |

### S10 — simulation across corners

| | |
| --- | --- |
| Input artifact | A netlist — schematic-level (S6, for Loop A's fast pre-layout passes) or extracted (S9, for the post-layout pass that actually verifies the built layout). |
| Output artifact | `klt sim`'s shipped JSON report (`docs/design/spice-corner-runner-spike.md` → "Response"; shipped contract documented in `docs/cli/sim.md`). |
| Entry criteria | A netlist that elaborates cleanly (S6 or S9's exit criteria, depending on which pass of Loop A this is). |
| Exit criteria | Loop A's convergence criterion (§1), evaluated at whichever netlist provenance this pass used — a schematic-level pass converging does not retire the stage; the pipeline is not simulation-verified per the vision sentence until a post-extraction pass converges. |
| `klt` verbs | `klt sim` (shipped). Sequence/waveform measurements (jitter, TIE, period) beyond scalar `.meas` reductions are blocked on #56 (see §4). |
| Failure modes | Loop A's stuck condition (§1); a schematic-level pass reported as final without a post-extraction re-run (silently downgrading "simulation-verified" to "simulated"); a `.meas`-only measurement set missing a sequence-shaped metric a real spec needs (#56). |

### S11 — signoff report

| | |
| --- | --- |
| Input artifact | Converged outputs of S8 (DRC/LVS clean) and S10 (post-extraction sim pass), plus S3's original block spec. |
| Output artifact | `klt.pipeline.signoff/1` (proposed): pass/fail against every S3 spec field, with the corner/measurement/violation evidence each verdict rests on, and provenance hashes for every input artifact (mirroring `klt sim`'s `environment` block's reproducibility discipline). |
| Entry criteria | Both S8 and S10 converged on the same layout/netlist generation (not a stale mix of an old layout's DRC pass and a newer netlist's sim pass). |
| Exit criteria | Every S3 spec field has a recorded verdict; no field is silently unaddressed. |
| `klt` verbs | `klt signoff` (shipped, #309): aggregates `klt drc`/`klt lvs`/`klt extract`/`klt sim` JSON outputs into one pass/fail verdict, refusing (`status: "refused"`) to combine inputs whose `provenance` blocks disagree. Does not yet diff against S3's spec fields — S3 has no machine-readable schema (see §4) — so the "pass/fail against every S3 spec field" half of this stage's output artifact remains agent-assembled via the `design-signoff` skill. |
| Failure modes | A spec field with no corresponding check anywhere upstream, discovered only here (the "signoff rejection" backtrack in §1); provenance mismatch between the layout and netlist being signed off (stale artifact pairing — `klt signoff`'s provenance-consistency check now catches this mechanically for the drc/lvs/extract/sim inputs it aggregates). |

## 3. Model-class matrix

Capability classes only, per the epic's explicit constraint — no vendor
model names, so this table doesn't need revisiting on every model release.
Three classes, ordered by capability (and cost): **frontier-reasoning**
(hardest available judgment, most expensive), **mid-tier** (competent
general reasoning, the default workhorse), **small-fast** (cheap, high
throughput, best at well-specified mechanical transformations).

| Stage | Class | Rationale | Escalation rule |
| --- | --- | --- | --- |
| S1 design proposal | frontier-reasoning | Underspecified, open-ended requirements; the cost of a misunderstood proposal compounds through every later stage. | None upward (already the ceiling); escalate to a human after N clarification rounds fail to close the open-questions list. |
| S2 system architecture & budget partition | frontier-reasoning | Multi-objective tradeoffs (power/area/noise/yield) across blocks with no single correct partition — exactly the "frontier reasoning models are wasted on mechanical work, small models fail at architecture partitioning" case the epic names. | Escalate to a human when no partition closes the system budget after N attempts (a genuine infeasibility, not a search failure). |
| S3 block specs | mid-tier | Mostly disciplined transcription of an already-decided budget allocation into a complete per-block spec, with some judgment on filling gaps S2 left open. | Escalate to frontier-reasoning when a block's allocation is internally inconsistent or clearly infeasible on inspection (routes back to S2 per §1). |
| S4 topology selection (KB-assisted) | mid-tier | A structured KB query plus fit-to-spec matching — bounded search over documented candidates, not open-ended design. | Escalate to frontier-reasoning when the KB returns no matching entry, or multiple entries tie and the choice needs first-principles judgment the KB doesn't encode. |
| S5 sizing | mid-tier | Iterative numeric optimization against simulator feedback — mechanical for a converging loop. **This is the epic's own worked example**: "sizing runs mid-tier, escalates to frontier after N failed corner iterations." | Escalate to frontier-reasoning after N consecutive Loop-A passes (§1) show non-monotonic margins or an unresolved measurement tradeoff. |
| S6 schematic/netlist | small-fast | Mechanical instantiation of already-sized devices into netlist syntax; no open design decision remains. | Escalate to mid-tier when elaboration errors persist across N passes (a topology/connectivity mismatch a small model can't diagnose). |
| S7 layout generation | mid-tier | Mapping sized devices onto generator parameters (grid, matching, guard rings) requires layout-idiom judgment even when the generator itself is mechanical. | Escalate to frontier-reasoning when no generator primitive fits the block's topology (a floorplan-level decision, not a parameter tweak). |
| S8 DRC/LVS | small-fast | Rule-by-rule violation fixing against a structured, itemized report is close-to-mechanical for a converging loop. | Escalate to mid-tier, then frontier-reasoning, after N consecutive Loop-B passes (§1) show non-monotonic violation counts or a DRC/LVS tradeoff. |
| S9 extraction | small-fast | Tool invocation plus a structural netlist-match check; no design judgment in a converging case. | Escalate to mid-tier when the extracted netlist mismatches the pre-layout netlist unexpectedly (debugging a topology discrepancy, not re-running the tool). |
| S10 simulation across corners | small-fast to invoke; mid-tier to interpret failures | Running `klt sim` and reading its structured pass/fail is mechanical; diagnosing *why* a corner failed (feeding Loop A) needs more judgment — hence the split, folded into S5's escalation rule above rather than duplicated here. | See S5. |
| S11 signoff report | small-fast | Templated aggregation of already-converged, already-structured JSON (`klt drc`/`klt sim` outputs) into a report; the checks were already done upstream. | Escalate to mid-tier or frontier-reasoning when aggregation surfaces a spec field with no corresponding upstream check (the "signoff rejection" backtrack, §1) — that gap needs judgment, not templating. |

## 4. Gap map

Cross-referencing the friction issues named in the epic and the
layout-generator spike issue.

| Stage | Tool support today | Gap / tracking issue |
| --- | --- | --- |
| S1 design proposal | None — intentionally out of tool scope (free-form intake). | — |
| S2 system architecture & budget partition | None. | No friction issue filed yet; a candidate future optimization/partition capability per `docs/ARCHITECTURE.md`'s "optimization" scope line, not yet demanded loudly enough to spike. |
| S3 block specs | None. | No friction issue filed yet — currently absorbed into S1/S2's human/agent judgment. |
| S4 topology selection (KB-assisted) | Shipped. `klt kb list`/`show`/`search`/`validate` (`docs/cli/kb.md`) query the KB corpus (`kb/`); Epic #102 (corpus + query surface) and its sub-issues (#106–#110) are closed. What remains is corpus *breadth*, grown incrementally, not the query surface. | — |
| S5 sizing | Partial, **by decision**. `klt sim` (shipped, #96) gives the feedback signal Loop A needs; next-candidate proposal is fully agent-side and stays that way until the re-trigger in §2's S5 recorded decision fires. | #310 (sizing candidate proposer — decision recorded, see §2 S5) |
| S6 schematic/netlist | None in `klt`. Schematic capture and netlist export live outside the repo today (xschem); the specific staleness/testbench-vs-block/config gaps a shared `klt netlist` (or documented helper) would close are filed. | #55 |
| S7 layout generation | Shipped. `klt gen`/`klt gen-compose` (`docs/cli/gen.md`, `docs/cli/gen-compose.md`) generate and compose layout from a netlist + parameters; `klt render` (visual check) and `klt layout-metrics` (area/utilization) support inspection. #104 closed; `klt gen-compose` was driven end-to-end against a real sky130 5T OTA (Epic #153 phase 4, #164/#196). | — |
| S8 DRC/LVS | Shipped. `klt drc` (`docs/cli/drc.md`) runs a curated sky130/gf180mcu deck subset (see that doc's "Coverage" section for fidelity caveats); `klt lvs` (`docs/cli/lvs.md`) runs a device/net compare (phase 3 of Epic #153). #54 closed. | — |
| S9 extraction | Shipped. `klt extract` (`docs/cli/extract.md`) produces a schematic-equivalent netlist (devices + connectivity, sky130/gf180mcu) and, with `--parasitics`, RC-parasitic extraction (#216 decision / #217 implementation, both closed); see `.claude/skills/design-extraction/SKILL.md`. | — |
| S10 simulation across corners | Mostly shipped. `klt sim` (#96) covers scalar `.meas`-based corner sweeps per the accepted spike. Sequence/waveform measurements (jitter, cycle-to-cycle, TIE) beyond scalar reduction are not covered. | #56 (waveform post-processing) |
| S11 signoff report | Partial. `klt signoff` (`docs/cli/signoff.md`, #309) aggregates `klt drc`/`klt lvs`/`klt extract`/`klt sim` JSON into one pass/fail verdict with provenance-consistency refusal. Does not diff against S3 spec fields (S3 has no schema — see the S3 row above) or walk the full T1 checklist's non-JSON items (design-hygiene, testbenches shipped). | No tracking issue — the remaining gap is the S3 block-spec schema (row above), not a signoff-side gap. |

**Reading the map:** both structural loops now have their core tooling
shipped. Loop A (sizing ↔ simulation) is served by `klt sim` (feedback) and
`klt kb` (topology query), with only the sizing-*candidate* proposer still
agent-side — deliberately, per §2's S5 recorded decision (#310). Loop B (layout ↔ DRC/LVS) has been driven end to end for
the sky130 5T OTA canary — `klt gen-compose` → `klt extract` → `klt lvs` →
`klt sim` closing the loop against a real block (Epic #153 phase 4, #164),
including the follow-on friction it surfaced (#199/#200/#201 —
obstacle-unaware routing, missing net labels, a spurious LVS device-class
mismatch — all closed). The stages that remain unbuilt are not loop
tooling but the *bracketing* stages: S2/S3 spec-and-partition work (no
tool, by design), the S5 sizing proposer (#310 — also no tool by design,
pending its re-trigger), S6 netlist export (#55), and S10 waveform
post-processing (#56); S11's mechanical drc/lvs/extract/sim aggregation now
ships as `klt signoff` (#309).
Epic #105's Phase 3 worked example can now drive a genuine closed-loop run
through Loop B rather than stubbing S7/S8/S9, with those bracketing stages
the remaining explicit gaps.

## Out of scope for this doc

No `klt` subcommand, JSON schema file, or `.claude/skills/` entry is added
here. The schema names in §2 (`klt.pipeline.<stage>/N`) are proposals for
the eventual contracts, not reservations against a shipped registry — a
Phase 2 skill or a later implementation issue may reshape them entirely.
This doc also does not attempt to design the escalation *mechanism*
(how a model-class handoff is actually invoked by an orchestrator like
Loom) — only the per-stage rule an orchestrator would consult.
