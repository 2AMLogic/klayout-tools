# Survey & proposal: measurable improvements to `klt place-and-route`

**Status:** research / proposal, no implementation. Filed for issue #735, a
research-and-propose task under the re-scoped
[Epic #700](https://github.com/2AMLogic/klayout-tools/issues/700) ("place-and-route
for `klt` — synthesis→placement→routing→GDS in Rust, OpenROAD-validated").
This document is the "fresh eyes" input that later implementation issues
build from — it does **not** authorise implementation of anything below.

**What this document does not settle.** Epic #700 itself carries
`loom:operator-only`/`loom:operator-decision` and proposes eventually
replacing today's OpenROAD-orchestration command with native-Rust
placement/routing (its own Phases 1–3). This survey does not presume that
outcome. Every improvement below states its own wrap-vs-native trade-off
explicitly, as data for that decision, not as an argument already made for
Rust — several of the highest-priority items here are one- or two-line
OpenROAD flag/flow changes with no Rust content at all.

**Required prior art, read first, not re-derived here:**

- [`docs/design/openroad-invocation-survey.md`](openroad-invocation-survey.md)
  (#397) — the accepted survey of OpenROAD's own invocation surface. Its §3
  worked example (a **real** run inside `openroad/orfs:latest`, gcd against
  sky130hd) is cited directly below as the strongest available evidence for
  one of this proposal's items — see §2.4.
- [`docs/design/digital-flow-contracts-spike.md`](digital-flow-contracts-spike.md)
  (#399) — the request/response JSON contract `klt place-and-route`
  implements today; this document proposes response-field additions in a
  couple of places and calls out that each is additive, not a contract
  break, per that spike's own posture.
- [`docs/design/digital-fleet-unit-abstraction-decision.md`](digital-fleet-unit-abstraction-decision.md)
  (#400) — establishes "one candidate evaluation" (a full synth→verify→P&R
  run at one design-space point: floorplan/seed/strategy) as the fleet's
  unit of parallelism, via `digital_fleet.py` (#445). Referenced in §3.5/§3.6
  below, which build on that existing DSE machinery rather than proposing a
  new one.
- [`docs/cli/place-and-route.md`](../cli/place-and-route.md) — the command's
  own contract documentation, the ground truth for §1 below.

## Evidence-tier discipline

Following this repo's own convention (`docs/design/openroad-invocation-survey.md`'s
own tiering, `docs/design-evidence-tiers.md`'s broader ladder): this task
did not run OpenROAD or any corpus benchmark — it is pure analysis, per its
own Definition of Done. Every claim below is one of:

- **[REPO]** — read directly from this repo's own source/docs, cited by
  file and line.
- **[REPO-RUN]** — a **real** result already captured by another accepted
  document in this repo (the #397 survey's live container run), not
  re-derived from memory.
- **[LIT]** — a technique or finding from the published EDA-CAD literature
  or OpenROAD's own documented command surface, cited by name/venue/year to
  the best of this survey's ability without live network/library access in
  this task. Treat exact author lists as best-effort — verify against the
  primary source before citing in a paper trail that requires precision.
- **[PROPOSAL]** — this document's own reasoning/recommendation, not a
  claim about the world.

No item below rests solely on an uncited assertion.

## 1. Baseline: what `klt place-and-route` does today

**[REPO]**, from `src/klayout_tools/place_and_route.py` and
`docs/cli/place-and-route.md`. `klt place-and-route` is a pure orchestration
layer: it never implements a placement, CTS, or routing algorithm itself.
It generates one Tcl script per requested stage (`floorplan` → `place` →
`cts` → `route`, `STAGE_ORDER`) and runs a real `openroad` binary as a
subprocess per stage (`_run_openroad`, `place_and_route.py:1267`), chained
via `write_db`/`read_db` ODB checkpoints. All actual algorithmic work —
global placement, legalization, clock-tree synthesis, global/detailed
routing, static timing — is OpenROAD's own (RePlAce-based `gpl`, `opendp`,
TritonCTS, TritonRoute, OpenSTA). The DEF→GDS merge is the one piece of
real in-process logic this command owns, ported from ORFS's
`def2stream.py` onto `klayout.db` (`_merge_def_to_gds`,
`place_and_route.py:1422`).

**Exactly what each stage's script contains today** (`_stage_script_lines`,
`place_and_route.py:1152`):

| Stage | OpenROAD calls this command issues | Notably *not* issued |
| --- | --- | --- |
| `floorplan` | `read_liberty`, `read_lef` (tech+cell+macro), `read_verilog`, `link_design`, `create_clock`, `initialize_floorplan`, `place_macro -exact` per hard macro, `make_tracks` | tapcell insertion, PDN generation (documented out of scope, `docs/cli/place-and-route.md` "Out of scope") |
| `place` | `place_pins`, `set_wire_rc`, **`global_placement -density 0.6 -random_seed <seed>`** (`_GLOBAL_PLACEMENT_DENSITY`, `place_and_route.py:319-323`, a fixed constant — not a request field), `estimate_parasitics -placement`, `repair_design`, `repair_timing`, `detailed_placement` | `global_placement -timing_driven`, `global_placement -routability_driven` — neither flag is passed (`place_and_route.py:1216-1228`) |
| `cts` | `set_wire_rc`, `estimate_parasitics`, `clock_tree_synthesis -root_buf <buf> -buf_list <buf>`, `estimate_parasitics`, `detailed_placement` | **no `repair_timing -hold`** anywhere in the `cts` stage or afterward — the only post-CTS optimization is a repeated `detailed_placement` (`place_and_route.py:1229-1238`) |
| `route` | `set_routing_layers -signal <range>`, `global_route`, `detailed_route -output_drc ... -output_maze ... -or_seed <seed>`, `estimate_parasitics -global_routing`, `write_def` | **no `repair_antenna`** after `detailed_route` (`place_and_route.py:1239-1258`); no `-repair_pdn`/fill/`DONT_USE_CELLS`, all documented out of scope |

Metrics are pulled from OpenROAD's own `-metrics <file>.json` channel plus a
documented stdout-scrape fallback for violation *counts*
(`_count_violations`, `place_and_route.py:1326`) — this is exactly the
recommendation the #397 survey's §6 flagged as worth confirming
(**[REPO-RUN]**, since verified true and shipped).

**Where it deliberately stops** (`docs/cli/place-and-route.md` "Out of
scope", confirmed unchanged): tapcell insertion, power-grid generation
(PDN), metal fill, `DONT_USE_CELLS`-style cell exclusion, IO-ring/footprint
floorplanning, and a second P&R engine. Hard-macro placement is explicitly
**caller-fixed** (`place_macro ... -exact`, never OpenROAD's automatic
`rtl_macro_placer`) — a deliberate design decision (issue #438), not a gap;
this survey does not propose touching it.

**The net baseline characterization:** every metric this command reports
today (`wirelength_um`, `worst_slack_ns`, `setup_violation_count`,
`hold_violation_count`, `estimated_power_mw`) is bounded by (a) OpenROAD's
own algorithms' ceiling, which this command does not touch, and (b) which
of OpenROAD's own *optional* flags and post-processing steps this wrapper
actually turns on, which it controls entirely today via the Tcl it
generates. Every "flow/parameter" item below lives entirely inside (b) —
zero new code beyond `_stage_script_lines`, zero Rust, zero contract-shape
risk beyond an additive response field in one case.

## 2. External SOTA survey

**2.1 Analytic/electrostatics-based global placement.** OpenROAD's `gpl`
module already implements RePlAce (Cheng, Kahng, Kang, Wang, "RePlAce:
Advancing Solution Quality and Routability Validation in Global Placement,"
IEEE TCAD 2019 — **[LIT]**), a Nesterov-method electrostatics-based analytic
placer descended from ePlace (Lu et al., "ePlace: Electrostatics based
Placement using Nesterov's Method," DAC 2014 — **[LIT]**). This is already
the engine `klt place-and-route` invokes — the improvement opportunity is
not "adopt a better placer" but "use the placer's own documented modes,"
since RePlAce exposes both **timing-driven** and **routability-driven**
operating modes as `global_placement` flags that this command does not
currently pass (§1 table, §3.1 below). GPU-accelerated placers
(DREAMPlace, Lin et al., "DREAMPlace: Deep Learning Toolkit-Enabled GPU
Acceleration for Modern VLSI Placement," DAC 2019 — **[LIT]**) report
substantially faster wall-clock than CPU RePlAce on large designs, but a
GPU dependency directly conflicts with this project's "headless always...
runnable in CI" mandate (`CLAUDE.md`) and is not proposed here for that
reason.

**2.2 Legalization.** OpenROAD's `opendp` (invoked here via
`detailed_placement`) legalizes a global-placement solution to zero-overlap,
on-site-grid rows with minimal total displacement — the same problem shape
Abacus solves (Spindler, Johannes et al., "Abacus: Fast Legalization of
Standard Cell Circuits with Minimal Movement," ISPD 2008 — **[LIT]**), an
`O(n log n)` dynamic-programming-per-row algorithm that is the closest thing
this sub-problem has to a settled, well-bounded, independently-checkable
building block (its own correctness criterion — zero overlap, minimal
displacement from the GP solution — is a crisp objective, unlike open-ended
global placement).

**2.3 Detailed routing / rip-up-reroute.** `detailed_route` invokes
TritonRoute, OpenROAD's own sequential, panel-based detailed router with
track assignment and rip-up-reroute (Kahng et al., ISPD 2019 — **[LIT]**,
best-effort citation — this command already uses it end to end).

**2.4 Known gap, with this repo's own real evidence.** The #397 survey's
§3 worked example is the strongest available data point here
(**[REPO-RUN]**, a real `openroad/orfs:latest` container run against a real
volare `sky130A` install, `docs/design/openroad-invocation-survey.md:194-231`):
ORFS's own reference flow shows global placement utilization jumping
**60.8% → 73.5%**, explicitly annotated in that survey as "routability-driven
cell inflation" (`GPL-0019`/`GPL-1126`-equivalent lines). That is RePlAce's
own routability-driven mode visibly doing work in the reference flow this
project already validated against — and `klt place-and-route`'s own
`global_placement` call (§1 table) passes neither `-routability_driven` nor
`-timing_driven`. This is not a hypothetical SOTA gap; it is a concrete,
already-observed difference between this command's own output and the
reference flow it is meant to match, from evidence already sitting in this
repo.

**2.5 Timing-driven placement.** RePlAce's timing-driven mode (and OpenROAD's
resizer/`repair_timing` more broadly) uses net-weighting from an incremental
static timer to bias placement/buffering toward the critical path — general
technique family, not project-specific (**[LIT]**, standard practice
described across OpenROAD's own documentation and the RePlAce paper's own
extensions).

**2.6 Clock-tree synthesis.** TritonCTS (OpenROAD's CTS engine, descended
from the classic bounded-skew/zero-skew clock-tree clustering literature)
exposes sink-clustering and obstruction-aware options this command's fixed
`clock_tree_synthesis -root_buf <buf> -buf_list <buf>` call (§1 table) does
not use (**[LIT]** for the general technique family; **[REPO]** for the
current call's exact flag set).

**2.7 Post-placement/post-route signoff steps most flows treat as
mandatory, not optional.** Two OpenROAD commands exist specifically because
CTS and routing change slack/DRC state after the steps that "finished"
them: `repair_timing -hold` (hold slack is negligible before a real clock
tree exists, and materially non-zero once one does — the general reason
production flows run hold-fixing immediately after CTS, not before) and
`repair_antenna` (routing can leave long single-net wire segments on one
layer before a via down to the gate is placed, charging the gate oxide
during plasma-etch fabrication — the classic "antenna effect," fixed by
inserting antenna diodes or jumper vias post-route). Both are **[LIT]**
general EDA-flow practice; §1 confirms neither is issued by this command
today.

**2.8 Rectilinear Steiner minimal tree (RSMT) estimation.** FLUTE (Chu &
Wong, "FLUTE: Fast Lookup Table Based Rectilinear Steiner Minimal Tree
Algorithm for VLSI Design," IEEE TCAD 2008 — **[LIT]**) is the standard fast
RSMT construction used for wirelength/congestion estimation ahead of full
routing, cited here only as the algorithm family for §3.6's proposal, not
as something this command currently uses or lacks in its OpenROAD-wrapped
form (OpenROAD's own `global_route` does its own congestion estimation
internally).

## 3. Prioritized proposal

Six items, ordered by (near-term impact) ÷ (engineering/risk cost). Items
1–4 are pure flow/parameter changes to the existing OpenROAD orchestration
— no Rust, no contract-shape change beyond one additive response field
(item 3). Items 5–6 are the genuine wrap-vs-native question Epic #700 asks
about, presented as open questions with an explicit go/no-go gate rather
than a foregone conclusion.

### 3.1 Enable `global_placement -routability_driven` / `-timing_driven` (Priority 1)

- **Technique:** pass RePlAce's own `-routability_driven` and
  `-timing_driven` flags on the existing `global_placement` call
  (§2.1/§2.4), instead of the current bare `-density -random_seed` pair.
- **QoR metric:** routability_driven → fewer detailed-routing DRC
  violations / higher first-pass route completion (directly visible in this
  command's own `-output_drc` report and `wirelength_um`, since routing
  congestion typically shows up as increased final wirelength or route
  failures); timing_driven → `worst_slack_ns`/`total_negative_slack_ns`
  improvement pre-CTS, which compounds through every later stage.
- **Rust vs. flow:** **flow/parameter.** Two flag additions to
  `_stage_script_lines`'s `place` branch (`place_and_route.py:1222-1223`).
  No new code path, no new dependency — OpenROAD already implements both
  modes; this command simply never asked for them.
- **Wrap-vs-native trade-off:** none to weigh — this is strictly inside the
  existing wrap. Nothing about routability/timing-driven placement argues
  for a native reimplementation; it argues for *using more of the tool
  already invoked*.
- **Measurement plan:** A/B the same request (fixed `seed`, same netlist)
  with and without the flags across the #520 Tiny Tapeout corpus (once a
  corpus harness exists — see §4) or, immediately, the existing
  `tests/corpus/place_and_route/gcd.gds.gz` fixture's own source request
  (`tests/corpus/place_and_route/regenerate.sh`); diff `wirelength_um`,
  `worst_slack_ns`, `total_negative_slack_ns`, `setup_violation_count`,
  `hold_violation_count`, and the `route` stage's own `-output_drc` report
  violation count; confirm `klt lvs` still matches (placement changes
  affect only geometry/timing, never the netlist a routability-driven pass
  legalizes against) and `klt drc` stays clean on the merged GDS.
- **Risk:** `-timing_driven` mode requires the design already be linked
  with `create_clock` issued (already true at this point in the script) and
  adds runtime (incremental STA calls during placement) — worth measuring
  wall-clock alongside QoR, not assumed free.

### 3.2 Post-CTS hold-timing repair (Priority 2)

- **Technique:** add `repair_timing -hold` immediately after
  `clock_tree_synthesis` (before the stage's closing `detailed_placement`),
  per §2.7's general post-CTS practice.
- **QoR metric:** `hold_violation_count` at (and after) the `cts` stage —
  currently this command's own `cts`/`route` stages report whatever hold
  state CTS happened to leave behind, with **no attempt to fix it**. Since
  a real clock tree introduces non-zero skew (the #397 survey's own §3
  detailed-placement-stage snapshot, pre-CTS, already shows `0` hold
  violations under an *ideal* clock — **[REPO-RUN]** — the interesting
  number is what CTS does to that afterward, which this command currently
  never measures against a repaired baseline because it never repairs).
- **Rust vs. flow:** **flow/parameter.** One `repair_timing -hold` line,
  `_stage_script_lines`'s `cts` branch (`place_and_route.py:1229-1238`).
- **Wrap-vs-native trade-off:** none — hold-fixing is a resizer/buffer-
  insertion step OpenROAD already implements and this command already links
  against for `repair_timing`/`repair_design` in the `place` stage; this is
  the same primitive, one stage later.
- **Measurement plan:** same A/B protocol as §3.1, isolating this one flow
  change; the diagnostic metric is `hold_violation_count` specifically
  (should drop toward 0, the target every real signoff flow holds itself
  to), with `total_negative_slack_ns`/`estimated_power_mw` tracked as the
  expected cost (hold buffers add area/power) — a real trade-off to
  quantify, not assume net-positive without measuring.
- **Risk:** none to the contract — this is a bugfix-shaped correctness gap
  (a documented "known-open" flow step, not a documented "out of scope"
  one), lowest-risk item in this list.

### 3.3 Post-route antenna repair (Priority 3)

- **Technique:** add `repair_antenna` after `detailed_route`, before
  `write_def` (§2.7).
- **QoR metric:** this is a **DRC-signoff/legality** metric, not a
  wirelength/timing one — `klt lvs`/`klt drc`'s own eventual downstream
  gate on the merged GDS (antenna-rule checking is a DRC concern, not an
  LVS one: it never changes device connectivity, only wire-segment
  geometry/via placement, so `klt lvs` should be unaffected while `klt drc`
  is the metric that would actually move). Today this command reports no
  antenna-violation signal at all — a gap in the response contract, not
  only the flow, since a caller currently has no way to know whether the
  merged GDS is antenna-clean.
- **Rust vs. flow:** **flow/parameter**, but with one real added-scope
  item: `repair_antenna` needs a per-`cell_library` antenna-diode cell name
  (and pin), the same "not derivable from the resolved PDK install itself"
  problem `_CTS_BUFFER_CELLS`/`_ROUTING_LAYER_RANGE`
  (`place_and_route.py:244-317`) already solve for CTS-buffer/routing-range
  — this item is naturally a third table in that same family, sourced the
  same way (ORFS's own `platforms/<variant>/config.mk`,
  `ANTENNA_DIODE_CELL`/`ANTENNA_DIODE_PIN_NAME`, cross-checked against the
  platform's own LEF — **never guessed**, per that code's own documented
  discipline). This survey does not assert the correct sky130hd/gf180mcu
  values here (a plausible sky130 candidate,
  `sky130_fd_sc_hd__diode_2`/`DIODE`, is **[LIT]**-tier recollection only —
  the follow-on issue must re-verify it against ORFS's own config.mk and
  LEF exactly as issue #637 did for the other two tables, not take this
  document's word for it).
- **Wrap-vs-native trade-off:** none — antenna repair is a diode-insertion
  pass OpenROAD already implements.
- **Measurement plan:** run `repair_antenna`'s own report (it reports a
  violation count directly) before/after on the corpus; additive response
  field proposal: `antenna_violation_count` (integer, `null` before the
  `route` stage — same pattern `wirelength_um`/`setup_violation_count`
  already use) — an additive JSON field, not a contract break, per
  `docs/design/digital-flow-contracts-spike.md`'s own posture.
- **Risk:** needs the new per-library table (real but bounded scope,
  mirroring existing precedent exactly); diode insertion adds cells,
  measure area/wirelength delta.

### 3.4 CTS sink-clustering / obstruction-aware tuning (Priority 4)

- **Technique:** pass TritonCTS's `-sink_clustering_enable`
  `-obstruction_aware` (and evaluate `-balance_levels`) flags on the
  existing `clock_tree_synthesis` call, instead of the current bare
  `-root_buf`/`-buf_list` pair (§2.6).
- **QoR metric:** clock skew reduction (not currently a response field —
  would need `OpenROAD`'s `report_clock_skew_metric`, which the #397
  survey's own §6 already confirmed exists side-by-side with the other
  `*_metric` procs this command already reads,
  `docs/design/openroad-invocation-survey.md:373-421`, **[REPO-RUN]**) and
  the downstream effect on `hold_violation_count`/`estimated_power_mw`
  (fewer/better-placed buffers).
- **Rust vs. flow:** **flow/parameter.**
- **Wrap-vs-native trade-off:** none.
- **Measurement plan:** same A/B protocol, with a new
  `clock_skew_ns`-shaped response field proposed alongside (additive).
- **Priority note:** lower than 3.1–3.3 because the Tiny Tapeout corpus
  (#520) skews toward 1×1-tile, small designs (72% of the corpus per that
  epic's own measured shape) where clock-tree size/skew is a smaller lever
  than on a larger design — real, but a lower-yield first target than the
  routability/hold/antenna gaps above, which apply uniformly regardless of
  design size.

### 3.5 Native-Rust standard-cell legalizer — Epic #700 Phase 1's proposed first slice

> **Spiked (issue #784): No-go**, on QoR grounds — the native-Rust Abacus
> legalizer (`native/legalize/`) matched OpenROAD's own `detailed_placement`
> on legality (zero overlap, DRC-clean to the same known baseline) but was
> 21.8%/26.6% worse on HPWL and 213.8%/403.4% worse on total displacement
> across the two-design corpus slice below, both well outside any
> reasonable reading of this section's own "ship when it matches or beats"
> bar. See `native/legalize/README.md` for the full measured comparison and
> root-cause analysis (row *assignment*, not row *legalization*, is the
> identified gap). This section's own reasoning below is left unchanged as
> the historical record of the proposal that was tested.

- **Technique:** implement Abacus-style minimal-displacement row
  legalization (§2.2) in Rust, taking a global-placement solution (from
  OpenROAD's own `gpl`, or a later native global placer) and producing a
  zero-overlap, on-site-grid legal placement.
- **QoR metric:** legality (zero overlaps, matches OpenROAD's own
  `opendp` zero-violation bar exactly — a binary correctness gate, not a
  quality spectrum) plus total displacement / `wirelength_um` delta versus
  OpenROAD's own legalizer output on the identical GP input, plus
  wall-clock (this is the metric a native-Rust rewrite is actually
  expected to move, per Epic #700's own framing — throughput at fleet
  scale, not necessarily better QoR than an already-mature tool).
- **Rust vs. flow:** **native-Rust hot-loop candidate**, explicitly *not*
  a settled recommendation here. This survey proposes legalization
  specifically (over global placement or routing) as the best **first**
  candidate for three reasons, not because it is the highest-QoR-impact
  item in this document (it is not — items 3.1–3.3 are cheaper and closer
  to certain wins): (a) it is the smallest, best-bounded sub-problem in the
  whole placement pipeline, with a crisp, checkable objective (zero
  overlap, minimal displacement) rather than global placement's open-ended
  multi-objective optimization; (b) it has a direct, unambiguous oracle —
  run OpenROAD's own `global_placement` to get an identical GP solution,
  legalize it two ways, diff the result — satisfying Epic #700's own
  "reality-grounding discipline" (its issue body's own "OpenROAD as ground
  truth" acceptance bar) with no interpretation required; (c) it exercises
  the FFI/data-plumbing pattern (reading an OpenROAD ODB/DEF checkpoint or
  an equivalent internal placement-graph representation into a Rust binary,
  and back) every later, harder native component (an analytic global
  placer, then a router) will also need, so it retires that integration
  risk cheaply before spending it on a harder algorithm.
- **Wrap-vs-native trade-off, stated honestly:** OpenROAD's own `opendp`
  legalizer is mature, fast, and already produces a zero-violation result
  today — this item's honest case is **not** "OpenROAD's legalizer is
  deficient" (§1/§2 found no evidence of that); it is "a native-Rust
  legalizer is the cheapest way to validate the FFI/oracle-comparison
  harness Epic #700's later, harder phases actually need," with fleet
  throughput (removing a subprocess + Tcl-script round trip from a
  candidate-evaluation loop that #400/#445 already run at scale) as the
  only concrete QoR-adjacent win. **Go/no-go gate, per Epic #700's own
  acceptance criteria:** ship only if it matches-or-beats OpenROAD's
  legalizer on the corpus (Epic #700 body: "Ship when it matches or beats
  on the constrained standard-cell case") — this survey does not
  presuppose that outcome.
- **Measurement plan:** for each corpus design, run OpenROAD's
  `global_placement` once (fixed seed) to get one GP solution; legalize it
  both ways (OpenROAD `detailed_placement` legalizer vs. the new Rust
  legalizer); diff overlap count (must be 0 for both), total displacement,
  post-legalization `wirelength_um`, and wall-clock; confirm `klt lvs`
  still matches (legalization changes geometry only, never connectivity)
  and `klt drc` stays clean end to end through the merged GDS.

### 3.6 Native-Rust FLUTE-based congestion pre-check for the fleet DSE loop

- **Technique:** a fast RSMT/HPWL-based congestion estimator (§2.8, FLUTE
  family) run immediately after global placement, before OpenROAD's own
  (comparatively expensive) `global_route`/`detailed_route`, to flag
  obviously-congested candidates cheaply.
- **QoR metric:** not a placement/routing QoR metric directly — a **fleet
  design-space-exploration (DSE) wall-clock** metric. The existing
  `digital_fleet.py` candidate-ranking machinery (#445, built on the
  "one candidate evaluation = one full synth→verify→P&R run" unit
  `docs/design/digital-fleet-unit-abstraction-decision.md` establishes —
  **[REPO]**) already evaluates many `(floorplan, seed)` candidates per
  design-space sweep; a cheap congestion pre-check that rejects a clearly-
  bad candidate before paying for a full `global_route`/`detailed_route`
  subprocess round trip directly cuts that sweep's wall-clock, the same
  lever `digital_fleet_sizing`'s own per-candidate cost model already cares
  about.
- **Rust vs. flow:** **native-Rust hot-loop candidate.** This is a
  genuinely hot numerical loop (RSMT construction across every net in a
  design, potentially thousands of nets, run repeatedly across a DSE
  sweep) — the shape a native implementation is best justified for, versus
  §3.5's more integration-motivated case.
- **Wrap-vs-native trade-off, stated honestly:** OpenROAD's own
  `global_route` already does real congestion estimation internally as
  part of actually routing — this item does not claim to beat that
  estimate's *accuracy*; it claims a cheaper **early-reject** signal is
  worth having in a many-candidate sweep specifically, where "route it for
  real to find out" is the expensive path being avoided, not replaced.
  Whether the wall-clock saved across a real sweep justifies building and
  maintaining a second, approximate estimator is exactly the open question
  Epic #700 should decide with real DSE-sweep timing data, not this
  document's own reasoning alone.
- **Measurement plan:** correlate the Rust congestion estimate against
  OpenROAD's own actual post-route DRC-violation count (from
  `detailed_route -output_drc`, already produced by every real run this
  command does) across the corpus — the estimator is only worth shipping
  if that correlation is strong enough to reject genuinely-bad candidates
  without false-rejecting good ones; report both the correlation and the
  DSE-sweep wall-clock saved, on the same corpus, before proposing this as
  more than a research spike.
- **Priority note:** ranked last because it optimizes DSE-loop throughput,
  not any single run's own QoR — valuable, but a narrower win than items
  3.1–3.5, and the most speculative of the six (no existing evidence in
  this repo either way, unlike 3.1's §2.4 grounding).

## 4. Measurement harness — common to every item above

- **Corpus.** Epic #520 (Tiny Tapeout, 4,572 project slots, 3,169 on
  `sky130A` per that epic's own measured 2026-08-04 shape — **[REPO]**) is
  the intended benchmark corpus, but **no place-and-route-specific corpus
  harness exists yet** — `tests/corpus/place_and_route/` today holds exactly
  one machine-generated fixture (`gcd.gds.gz`, regenerated via
  `tests/corpus/place_and_route/regenerate.sh` against a real
  `openroad/orfs:latest` container, **[REPO]**), not a batch runner over
  #520. Any of the items above that want the full corpus as their baseline
  (all of them, per the issue's own item (d)) has this as a real
  prerequisite gap, not an oversight in this survey — building "run `klt
  place-and-route` over N Tiny Tapeout designs and diff metrics" is itself
  scoped implementation work, most naturally a `regenerate.sh`-style script
  generalized to loop over a corpus manifest rather than one fixture.
- **OpenROAD as oracle.** For the two native-Rust items (§3.5/§3.6), per
  Epic #700's own "reality-grounding discipline" (its issue body: "OpenROAD
  is the mature open P&R flow... each klt P&R stage is validated by
  comparing its result... against OpenROAD's on the same netlist + LEF/tech"
  — **[REPO]**), OpenROAD's own output on the identical input is the
  correctness bar, not this repo's own prior output.
- **`klt lvs` gate.** Every item above changes geometry/timing, never
  connectivity — `klt lvs` matching the netlist from item 1 of the T1
  checklist (`docs/design-evidence-tiers.md`) is the standing regression
  gate for "did this change break structural equivalence," expected to
  pass unchanged for every item in §3 (a failure would itself be a defect
  report, not an expected trade-off).
- **`klt drc` gate.** The metric that actually moves for §3.3 (antenna
  repair) specifically, and a standing sanity check for every item that
  touches routing.
- **A/B protocol.** Same request document, same `seed` (P&R is genuinely
  stochastic — `docs/cli/place-and-route.md`'s own `seed` field
  documentation — so seed must be pinned for any of these diffs to be
  meaningful), one flag/step toggled, JSON response fields diffed directly
  (`wirelength_um`, `worst_slack_ns`, `total_negative_slack_ns`,
  `setup_violation_count`, `hold_violation_count`, `estimated_power_mw`,
  plus the proposed additive `antenna_violation_count`/`clock_skew_ns`
  fields), never eyeballed from logs.

## 5. Follow-on implementation-issue sketches (top 2)

### Sketch A — close the three observed OpenROAD-default-flow gaps (§3.1–3.3)

**Title:** `place-and-route: enable routability/timing-driven global placement, post-CTS hold repair, post-route antenna repair`

**Why bundled:** all three are the same shape of change (one to a few new
Tcl lines in `_stage_script_lines`, no new dependency, no contract-shape
risk beyond one additive field), all three close a gap this survey found
concrete evidence for (not speculative), and bundling them means one A/B
corpus run measures all three deltas together — cheaper than three separate
review/measurement cycles for changes of this size.

**Scope:**
1. `global_placement -density <fixed> -random_seed <seed> -routability_driven -timing_driven` in the `place` stage (§3.1).
2. `repair_timing -hold` appended to the `cts` stage, before its closing `detailed_placement` (§3.2).
3. A new `_ANTENNA_DIODE_CELLS: dict[str, str]` table (mirroring `_CTS_BUFFER_CELLS`/`_ROUTING_LAYER_RANGE`'s exact sourcing discipline — real, verified-against-ORFS's-own-`config.mk`-and-LEF values, never guessed) plus `repair_antenna` in the `route` stage before `write_def`, plus a new additive `antenna_violation_count` response field (§3.3).
4. Update `docs/cli/place-and-route.md`'s request/response tables for the new field; no other contract change.

**Acceptance criteria:**
- Existing `tests/test_place_and_route.py` stubbed-OpenROAD tests updated to assert the new Tcl lines appear in the generated scripts for stages ≥ `place`/`cts`/`route` respectively.
- A/B run (real OpenROAD, at minimum the existing `gcd` fixture's own request, ideally an initial small corpus slice) showing the four target metrics move in the expected direction (`worst_slack_ns`/`total_negative_slack_ns` improve or hold, `hold_violation_count` drops toward 0, `antenna_violation_count` reports 0 post-fix), captured in the PR description per this repo's "no claim without a runnable check" discipline.
- `klt lvs`/`klt drc` unchanged-pass on the corpus slice used.
- Every new per-library table entry cites its ORFS `config.mk`/LEF source exactly as issue #637 did for the existing two tables.

**Not in scope:** the full #520 corpus harness (§4's own noted prerequisite gap) — this issue may use a small hand-picked slice; building the general corpus runner is separable follow-on work, referenced but not required here.

### Sketch B — native-Rust legalizer spike, oracle-gated (§3.5)

**Title:** `place-and-route: spike a native-Rust standard-cell legalizer against OpenROAD's opendp, oracle-gated`

**Framing:** explicitly a **spike**, not a commitment to ship — per Epic
#700's own acceptance bar ("Ship when it matches or beats on the
constrained standard-cell case") and this survey's own §3.5 "stated
honestly" trade-off. The deliverable is a go/no-go decision backed by real
corpus numbers, consumable as Epic #700 Phase 1's first data point, not a
production legalizer merged on faith.

**Scope:**
1. Read one real OpenROAD-produced GP solution (via ODB checkpoint or DEF, whichever this repo's own tooling can already parse without new dependencies — `_merge_def_to_gds` already reads DEF via `klayout.db`, a natural starting point) into an in-repo Rust crate.
2. Implement Abacus-style row legalization (§2.2) — zero-overlap, minimal-displacement, on-site-grid.
3. Legalize the same GP solution both ways (existing OpenROAD `detailed_placement` legalizer vs. the new Rust legalizer) across a small corpus slice.
4. Report overlap count (must be 0 both ways), total displacement delta, `wirelength_um` delta, and wall-clock, per §3.5's measurement plan — as a comparison table in the PR/spike doc, not asserted from memory.

**Acceptance criteria (go/no-go, decided by the numbers this spike produces, not assumed here):**
- **Go** (proceed toward integrating as `klt place-and-route`'s own legalization step, or toward Epic #700's `klt par` verb) only if: 0 overlaps on every corpus design tested, `wirelength_um`/displacement within a documented tolerance of OpenROAD's own legalizer output, and a measured wall-clock or integration (no-subprocess) win that justifies the added maintenance surface.
- **No-go** (park the spike, document why) if legality can't be matched, or QoR degrades beyond tolerance, or the wall-clock win doesn't materialize once real FFI/data-marshalling overhead is measured (not assumed away).
- Either outcome is a valid, reportable result for Epic #700 per its own "no claim without a runnable check" discipline — this spike is designed to produce evidence either way, not to presuppose "Go."

**Not in scope:** global placement or routing (later, harder phases per Epic #700's own phase ordering); wiring this into the actual `klt place-and-route`/`klt par` request/response contract (a separate, larger issue once/if this spike says "Go").

## References

- Cheng, C.-K., Kahng, A. B., Kang, I., Wang, L. "RePlAce: Advancing
  Solution Quality and Routability Validation in Global Placement." IEEE
  TCAD, 2019.
- Lu, J. et al. "ePlace: Electrostatics based Placement using Nesterov's
  Method." DAC, 2014.
- Lin, Y. et al. "DREAMPlace: Deep Learning Toolkit-Enabled GPU
  Acceleration for Modern VLSI Placement." DAC, 2019.
- Spindler, P., Johannes, F. M. et al. "Abacus: Fast Legalization of
  Standard Cell Circuits with Minimal Movement." ISPD, 2008.
- Kahng, A. B. et al. "TritonRoute: The Open-Source Detailed Router" (best-
  effort citation — OpenROAD's own project bibliography is the primary
  source to verify exact title/venue/authors against). ISPD, 2019.
- Chu, C., Wong, D. F. "FLUTE: Fast Lookup Table Based Rectilinear Steiner
  Minimal Tree Algorithm for VLSI Design." IEEE TCAD, 2008.
- `docs/design/openroad-invocation-survey.md` (#397) — this repo's own
  real, live-verified OpenROAD invocation survey; §3's worked example is
  the direct evidentiary basis for §2.4/§3.1 above.
- `docs/design/digital-flow-contracts-spike.md` (#399) — the request/
  response contract this document proposes additive fields against.
- `docs/design/digital-fleet-unit-abstraction-decision.md` (#400) — the
  candidate/DSE framing §3.6 builds on.
- Epic #700 (this document's own parent) and Epic #520 (the Tiny Tapeout
  corpus this document's measurement plan targets).
