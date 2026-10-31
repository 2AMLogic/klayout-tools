# Survey: OPERA, an expert-taught gm/ID sizing and layout-generator agent flow

**Status:** research spike for #4644. Output is a decision document, not code.
No format, schema, CLI verb, skill edit or KB entry is proposed for
implementation here; follow-ups are filed unlabeled per
[`../library/README.md`](../library/README.md) §1.

**Evidence tiers.** Every external claim carries one tag:

- **[fetched]**: read from the source on **2026-10-30** at the URL given.
- **[inferred]**: my reading of what a fetched fact implies, or a gap in what
  was fetched. Not claimed to be in the source.
- **[repo]**: read from this repository at `origin/main` `7e59b6e73`.

The paper was read from its arXiv HTML rendering
(<https://arxiv.org/html/2610.39395v1>), through a summarizing fetch rather
than a raw text dump. Section numbers below are the paper's. Where the
summarizer reported that the paper does not state something (the intent-file
field schema, the Tian method detail, a code-availability statement), that is
recorded as **not stated in what was fetched**, not as proof of absence. Figures
and tables are not reproduced; everything is in this document's own words.

## 1. The paper

Saxena and Mandal, "OPERA: OPamp Design by an Expert-Taught Reasoning Agent,
from Specifications to Layout", arXiv:2610.39395 v1, submitted 2026-10-29
(cs.AR). [fetched]

**Idea.** A general-purpose Claude model, with no fine-tuning, is *taught* by a
designer in two phases and stores what it learned as reloadable skill files.
(Teaching used Opus 5.5; the iterative loops used Sonnet 5.5, §II.) [fetched]

### 1.1 Sizing method (§II-A, §III)

- **gm/ID tables (§III-A).** Characterized for SKY130 low-Vt devices at the
  planned finger widths (3.11 µm NMOS, 2.48 µm PMOS), with gm/ID as the axis.
  Tabulated quantities: current density, fT, gm·ro and capacitances. Several
  channel lengths, five process corners. The specific lengths were not stated
  in what was fetched. [fetched]
- **Six-step procedure (§III-B).** [fetched, paraphrased]
  1. Specs to gm and currents: gm1 from the unity-gain frequency (against CL,
     or Cc for two-stage); phase margin sets gm2; slew rate sets a minimum
     tail current; output and folding branches have their own minimum-current
     rules; a slew-to-bandwidth ratio caps first-stage gm/ID.
  2. gm/ID per device role: high for the input pair, low for mirrors and
     current sources (matching), moderate to high for cascodes (headroom),
     capped where fT sets a signal-path pole. Copied currents share gm/ID and L.
  3. Channel length from the gain budget (gm·ro against gm/ID), traded against
     fT and matching.
  4. Sizing: Id = gm / (gm/ID); W = Id / (current density), rounded to whole
     fingers.
  5. Checks: saturation headroom (about 2/(gm/ID)) and pole estimates first,
     then simulation.
  6. Trade-offs: design charts yield two or three candidates for a designer gate.
- **Searched knobs.** Per-role gm/ID, branch currents, and for two-stage
  designs m = gm2/gm1, Cc and Rz. The paper says about a dozen continuous
  widths, lengths and currents collapse to these few. [fetched]
- **Iteration rule.** One knob per spec, judged at the worst corner; secant
  updates for continuous knobs, bisection for integer knobs (finger counts).
  Hand equations predicted gain and power to about 2 % but overestimated phase
  margin by about 18°, so stability is closed in simulation. [fetched]

### 1.2 Layout method (§IV)

- Layout practice taught to the agent: 2-D common-centroid input pair,
  interdigitated mirrors with dummies, a guard ring per stage, high packing
  (the issue states at least 70 %; that figure was **not confirmed** in the
  fetch). [inferred from issue text]
- The agent writes a reusable generator on top of gLayout, driven by the
  reference netlist plus a **layout-intent file** recording the designer's
  decisions on unit sizes, matched-set patterns, passive placement and pin
  edges (§IV-C). The field-level schema was not stated in what was fetched.
  [fetched]
- **One-command PASS gate (§IV-C).** PASS requires all of: Magic and KLayout
  DRC clean; Netgen LVS match; every intent-file item satisfied; matching and
  packing checks pass; the *extracted* netlist meets the targets across the
  corners. [fetched]
- **gLayout defect (§IV-A).** gLayout's layer names were offset by one from
  SKY130. The agent found it through DRC on small pieces and worked around it
  with an explicit layer map; every net is routed as a Manhattan path with via
  stacks. [fetched]
- **Approval gates (§II-A, §IV-B).** Schematic side: per-role gm/ID choices,
  sizing changes, accepted corner failures. Layout side, numbered: gate 1 unit
  fingering, gate 2 floorplan, gate 3 full layout and verification. Each is
  presented as two or three simulated options. [fetched] The paper is
  therefore not autonomous; it is designer-gated.

### 1.3 Results (§V)

- Scope: a shared bias plus six single-ended opamps/OTAs (5T, folded-cascode,
  telescopic, three two-stage variants), four specs each, SKY130 only. [fetched]
- Verification: 15 PVT corners (tt, ff, ss, fs, sf by 0, 27, 70 °C; 1.8 V
  supply, 0.9 V common mode), measured on the **extracted** netlist, all DRC and
  LVS clean. Stability by a loop-gain bench citing Tian et al.; the method
  detail was not stated in what was fetched. [fetched]
- Efficiency (§V-C): OPERA passed in 30 to 54 runs (Protocol A) and 53 to 59
  (Protocol B) against a 500-run cap for random search and TPE. TPE medians
  under A were 45 to 204 runs, random 35 to 281, and neither optimizer passed
  every seed on two of the designs under B. Three seeds (five for the 5T). The
  optimizers were given the first-campaign spec without a gain target, which the
  paper itself flags as a fairness caveat. [fetched]
- Extraction cost (§V-D): UGF down 2 to 7 %, phase margin down 1 to 8°, DC
  gain moved at most 0.1 dB. Three designs missed the 60° PM target on first
  extraction (57.2°, 53.3°, 59.4°) and were fixed by shortening a channel
  length (two cases) or setting Rz (one case). [fetched]
- Margins (§V-B): one weak cascode sat 23 to 63 mV into triode at fs/70 °C in
  five designs. [fetched]
- Limitations stated: single designer, one generator family, one PDK, Class-A
  output stages, four specs, human gates. [fetched]

**Reading the efficiency claim.** The run counts compare an agent *with a
hand-taught analytic prior* against optimizers with none. That is evidence that
the prior is valuable, not that LLM proposal beats search. [inferred]

## 2. Gap map against klt

| Technique | Status | Where / why |
| --- | --- | --- |
| Persisted gm/ID charts (Id/W, fT, gm·ro, C versus gm/ID over L and corners) | **Gap** | `klt size` re-sweeps one device per request with ngspice as the in-loop evaluator and reports a confirmed operating point ([`docs/cli/size.md`](../cli/size.md) "Method"). Nothing emits a reusable characterization table. `klt characterize` is the digital NLDM verb ([`docs/cli/characterize.md`](../cli/characterize.md)), so a table emitter needs a different name. |
| Spec to gm to role mapping, six-step procedure | **Partial** | [`.claude/skills/design-sizing/SKILL.md`](../../.claude/skills/design-sizing/SKILL.md) owns Loop A procedure; `kb/entries/{five-transistor,folded-cascode,two-stage-miller}-ota.json` carry a prose `sizing_approach` (the 5T one already says to size the input pair from a gm/ID design point, and to use long mirror L). What is missing is the *numeric* chain (gm from UGF, gm2 from PM, tail from slew, headroom check) and a per-role gm/ID table. Per [`design-pipeline.md`](design-pipeline.md) "skills are procedure, not strategy" this belongs in skill and KB content, not a verb. No telescopic or cascode-bias KB entry exists (`ls kb/entries`). |
| One-knob-per-spec worst-corner iteration, secant/bisection | **Shipped by decision, agent-side** | `klt sim` returns signed per-measurement `margin` rolled up to `worst_case` across the corner matrix, and `klt size` has a worst-corner margin objective (#769). Next-candidate proposal stays agent-side per the #310 decision ([`design-pipeline.md`](design-pipeline.md) S5). See §2.1. |
| Layout-intent file | **Partial, close** | `klt.layout_plan.request/1` ([`docs/cli/layout-plan.md`](../cli/layout-plan.md), schema `docs/schemas/layout-plan-request.schema.json`) is already a declarative plan of grouping, matching, ordering and abutment, library-level with no `klt plan` verb. Unit finger sizes and pin edges should be compared field by field; I did not have OPERA's schema (not stated in fetch), so the comparison is on the paper's four named categories only. |
| Intent-compliance, matching, packing checks | **Partial** | `klt extract --matched-group` ([`docs/cli/extract.md`](../cli/extract.md) "Matched-device geometry check", the §2.4 gap in [`analog-resource-survey.md`](analog-resource-survey.md), now shipped); `klt layout-metrics` ([`docs/cli/layout-metrics.md`](../cli/layout-metrics.md)); the packing rubric in `.claude/skills/economy-review/SKILL.md`. **Gap:** no check that a finished layout *complies with the plan document*, and no single verdict that folds DRC, LVS, plan compliance, matching and corner-swept extracted performance. `klt signoff` ([`docs/cli/signoff.md`](../cli/signoff.md)) aggregates the envelopes but has no plan-compliance input. |
| Approval gates with 2 to 3 simulated options | **Skill content, nothing new** | Maps to the S5/S7 escalation points in [`design-pipeline.md`](design-pipeline.md). klt already returns the evidence for each option; presenting options is agent behavior. Not an `eval` gate kind. [inferred] |
| Loop-gain / PM measurement | **Parity (by inspection)** | `klt sim` `measurements[].tian` and the `dual_injection` requirement ([`docs/cli/sim.md`](../cli/sim.md), #3160/#3161). OPERA cites Tian but gives no method detail in the fetch, so "parity" means same family, not same bench. |
| Per-corner saturation margin | **Partial** | `klt sim --op-lint` reports per-device `triode` findings with a corner-aware margin and the applied thresholds ([`docs/cli/sim.md`](../cli/sim.md) "Operating-point lint"). OPERA's weak-cascode finding (23 to 63 mV into triode at fs/70 °C) is exactly the class this flags, but lint runs at *one* chosen corner (`--op-lint-corner`). A margin-per-corner table would be needed to compare with AMPICBench's `corners.json`, and I have not seen that file's schema (§5). |
| Extracted-netlist, corner-swept verification | **Shipped** | `klt pex` re-runs the testbenches on the parasitic-annotated netlist and reports the schematic-versus-extracted delta per corner ([`design-extraction` skill](../../.claude/skills/design-extraction/SKILL.md)). Matches OPERA's most important discipline. |

### 2.1 Is OPERA evidence for or against re-triggering #310?

The #310 re-trigger asks for demonstrated *search thrash*: a real block whose
Loop A reaches its escalation count with every pass changing parameters and
diagnosis-side causes ruled out ([`design-pipeline.md`](design-pipeline.md)).
OPERA does not meet it. Its proposer is the agent, its knob set is small
(per-role gm/ID, currents, m, Cc, Rz), and it converges in 30 to 59 runs. That
**agrees with #310**: the leverage came from a taught analytic prior that
shrinks the search space, not from a numeric optimizer. The two things klt can
supply, a persisted table and a worst-corner scalar, both already exist or are
cheap. Verdict: **evidence against building a generic optimizer verb now,
evidence for the table emitter** (§6). [inferred]

One caution for #310: OPERA's own finding that hand equations overestimate PM by
about 18° is a second reason to keep simulation, not equations, as the final
arbiter, which is what `klt size` already does.

## 3. Reproducibility walk (bounded)

**Circuit:** the sky130 5T OTA, which is already driven end to end in
[`examples/design-pipeline/`](../../examples/design-pipeline/) (proposal,
architecture, block spec, topology, sizing, layout, extract, LVS).

**Honesty note.** This was a **desk walk** of docs and committed artifacts, not
an executed run. Executing it would have meant a multi-corner `klt sim`
campaign on a shared dispatch host for a research spike; the issue asks for
the blocked and manual steps, and those are visible from the contracts. Treat
the verdicts below as [repo] plus [inferred], and re-check them if a follow-up
executes the walk.

| OPERA step | Walk with klt verbs only | Status |
| --- | --- | --- |
| Characterize gm/ID tables per role, L, corner | No verb emits a table. Closest: repeated `klt size` requests, one device and one bias point each. | **Blocked / manual.** Agent would issue many requests and keep its own table. |
| Spec to gm, tail current, role gm/ID | `design-sizing` skill plus KB `sizing_approach`. No numeric chain is written down. | **Manual.** Agent derives equations itself. |
| Per-device sizing | `klt size`, single device or coupled diff-pair/mirror/tail, with PVT corner sets (#729) and a worst-corner margin objective (#769). | **Works.** |
| Close PM/UGF/gain across corners | `klt sim` corner matrix with `tian` measurements; agent proposes next knob. | **Works**, proposer agent-side by decision. |
| Generate layout | `klt gen` primitives and `klt gen-compose` (5T OTA is the original case, #164). | **Works** for 5T; no common-centroid 2-D input pair or per-stage guard-ring recipe was verified in this walk. [inferred] |
| Capture layout intent | `klt.layout_plan.request/1`, library-level only. | **Partial**, no verb. |
| DRC, LVS | `klt drc`, `klt lvs`. | **Works.** |
| Intent compliance | None. | **Gap.** |
| Matching check | `klt extract --matched-group`. | **Works** for geometry equality. |
| Extracted corner-swept performance | `klt extract --parasitics` then `klt pex`. | **Works**, with the first-order RC fidelity caveat in [`extract-fidelity-roadmap.md`](extract-fidelity-roadmap.md). |
| One verdict | `klt signoff` aggregates envelopes. | **Partial**, no plan-compliance input. |

**Friction to file, not fix:** (1) table emitter, (2) numeric procedure in skill
and KB, (3) plan-compliance check. Nothing else blocked the walk.

## 4. gLayout as an engine

The issue says `layout-generator-spike.md` "surveyed gLayout/OpenFASoC".
**Correction [repo]:** it does not. That spike surveys BAG3/xbase, laygo2,
gdsfactory, ALIGN, MAGICAL and KLayout PCells; gLayout appears in this
repository only as a mention in the "Glayout opamp" row of
[`verified-design-library-survey.md`](verified-design-library-survey.md) (a
GF180 generator under OpenFASoC). So there is no earlier gLayout verdict to
overturn.

Does OPERA change the pya-reference-implementation decision? **No.**

- The decision rests on headless, JSON-contracted, in-repo generation
  ([`layout-generator-spike.md`](layout-generator-spike.md) §2, §3). OPERA is
  an existence proof that an agent can *write a generator*, which supports the
  direction (generators as agent output) without favoring gLayout.
- The paper's one concrete gLayout fact is negative: a layer-map offset against
  SKY130 that had to be worked around, plus Manhattan-only routing with via
  stacks supplied by the agent. [fetched] That is a maintenance risk for
  adopting it as an engine, not a reason to.
- I did not audit gLayout's license, maintenance status or API in this spike.
  [inferred, not checked] If a future spike reopens the question it should
  fetch those first.

A transferable idea: running DRC on *small pieces* early surfaced the layer-map
bug. `klt drc` already supports this; it is a skill-level practice for the
generator-authoring loop. [inferred]

## 5. Released artifacts and licensing

- **AMPICBench** (<https://github.com/AMPICLab/AMPICBench>), named by the
  paper as the release for benchmark testbenches and netlists (abstract, §V-A,
  §VII). [fetched] Its `LICENSE` file, read directly at
  <https://raw.githubusercontent.com/AMPICLab/AMPICBench/main/LICENSE> on
  **2026-10-30**, is **Creative Commons Attribution-NonCommercial 4.0
  International (CC BY-NC 4.0)**. [fetched, from the license file, not from the
  README]
- The issue additionally lists sky130A ngspice netlists, per-corner op data
  (`corners.json`), LVT characterization decks and stb testbenches as repo
  contents. I did not open the repo tree in this spike, so that inventory is
  the issue author's, not mine. [not verified]
- **Agent code, skill files, layout generator:** the paper has **no
  availability statement covering them** in what was fetched, and the fetched
  text names only AMPICBench as released. [fetched; absence not proven]

**Consequence.** CC BY-NC is incompatible with vendoring into an MIT repo, and
"NonCommercial" is a poor fit even for a CI fixture that ships in a public
package. Nothing from AMPICBench may be copied here, and per the clean-room
rule used in `op_sanity.py` nothing should be *adapted* from it either; ideas
from the paper only.

**As an external, non-redistributed oracle?** Possible in principle:
a developer fetches AMPICBench themselves, an opt-in test points at a local
path, and results are compared but never committed. That keeps it out of the
repository and the wheel. Costs: nobody else can reproduce the run, CI cannot
use it, and a noncommercial license may constrain *use* by a company, not only
redistribution. That last point is a legal question I am not qualified to
settle. [inferred] Recommendation: **do not depend on it**; treat it as a
pointer, and revisit only if the operator explicitly approves the use posture.

**Clean-room equivalent, for #4596 recommendation (b) and #1719-style
benchmarking.** A verification record (corner set, specs, measured values,
evidence tier, tool/PDK versions, re-runnable by `klt sim`) for klt's *own* 5T,
folded-cascode and two-stage designs, generated by klt. The 15-corner matrix
(5 process by 3 temperatures) is a published convention, a fact rather than
expression, and `docs/cli/corner-matrix.md` already expresses matrices of that
shape. [inferred]

## 6. Recommendation

Ranked, for the operator. Scope is rough and every item is a *candidate*;
nothing here authorizes implementation.

1. **Numeric gm/ID procedure as skill and KB content.** Extend
   `design-sizing` and the three OTA `sizing_approach` fields with the
   spec-to-gm-to-role chain and headroom check, citing OPERA as source of the
   practice. *Type:* skill/KB content. *Scope:* small, docs only. Highest
   value for lowest cost, and it is the part of OPERA's gain that came from
   the taught prior.
2. **Add a telescopic OTA KB entry** (OPERA covers it; we do not). *Type:* KB
   content. *Scope:* small. Needs sim evidence per KB conventions, so it is not
   free.
3. **Persisted gm/ID characterization tables.** A way to emit and reload
   Id/W, fT, gm·ro and C versus gm/ID over L and corners for a PDK device.
   *Type:* new verb or `klt size` mode. *Scope:* medium. Must not reuse the
   name `klt characterize`. Needs a design spike for the table contract first,
   per `docs/ARCHITECTURE.md` "How capabilities arrive".
4. **Layout-plan compliance check.** Verify a finished layout against its
   `klt.layout_plan.request/1` document, and let `klt signoff` consume the
   result. *Type:* `klt` check. *Scope:* medium. This is the real gap in the
   OPERA PASS gate and builds on shipped pieces.
5. **Per-corner saturation-margin table from `--op-lint`.** One run reporting
   worst Vds-Vdsat margin per corner, which also makes our output comparable to
   AMPICBench-style data without touching its files. *Type:* `klt` check.
   *Scope:* small to medium.
6. **Skill note: lint small generator pieces early** (the gLayout layer-map
   lesson) and **present 2 to 3 simulated options at the S5/S7 gates**.
   *Type:* skill content. *Scope:* trivial. Fold into item 1.

**Not worth adopting.**

- **A generic optimizer verb (`klt optimize`).** OPERA supports #310: the win
  was the analytic prior. Revisit only on the existing re-trigger.
- **gLayout as an engine.** No verdict change (§4); a layer-map defect and a
  routing workaround are a cost, not an argument.
- **Vendoring or adapting AMPICBench.** License (§5).
- **A new `eval` gate kind for approval gates.** The gates are agent
  behavior over evidence klt already returns.
- **Treating the 30 to 59 run count as a klt target.** It is one author, one
  PDK, four specs and a mildly unfair baseline (§1.3).

**Open questions.**

- Does OPERA's layout-intent schema match `klt.layout_plan.request/1`?
  Unanswerable from what was fetched; needs the TeX source
  (<https://arxiv.org/src/2610.39395>) or a re-read of §IV-C.
- Is the walk in §3 still desk-only correct when actually executed?
- Operator's posture on use (not redistribution) of CC BY-NC material.

## Sources, checked 2026-10-30

- OPERA paper, abstract page: <https://arxiv.org/abs/2610.39395>; HTML:
  <https://arxiv.org/html/2610.39395v1>. Read via summarizing fetch.
- AMPICBench `LICENSE`:
  <https://raw.githubusercontent.com/AMPICLab/AMPICBench/main/LICENSE>.
- Companion survey: [`verified-design-library-survey.md`](verified-design-library-survey.md)
  (#4596).
- Repository paths cited above, all present at `7e59b6e73`.
