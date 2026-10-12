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
| `floorplan` | `read_liberty`, `read_lef` (tech+cell+macro), `read_verilog`, `link_design`, `create_clock`, `initialize_floorplan`, `place_macro -exact` per hard macro, `make_tracks`, and — **only when `request.power` is given** (issue #1091) — `tapcell`, `add_global_connection`/`global_connect`, `pdngen` | tapcell insertion/PDN generation are `request.power`-gated, not unconditional; macro-specific PDN grids remain out of scope (`docs/cli/place-and-route.md` "Power delivery"/"Out of scope") |
| `place` | `place_pins`, `set_wire_rc`, **`global_placement -density 0.6 -random_seed <seed>`** (`_GLOBAL_PLACEMENT_DENSITY`, `place_and_route.py:319-323`, a fixed constant — not a request field), `estimate_parasitics -placement`, `repair_design`, `repair_timing`, `detailed_placement` | `global_placement -timing_driven`, `global_placement -routability_driven` — neither flag is passed (`place_and_route.py:1216-1228`) |
| `cts` | `set_wire_rc`, `estimate_parasitics`, `clock_tree_synthesis -root_buf <buf> -buf_list <buf>`, `estimate_parasitics`, `detailed_placement` | **no `repair_timing -hold`** anywhere in the `cts` stage or afterward — the only post-CTS optimization is a repeated `detailed_placement` (`place_and_route.py:1229-1238`) |
| `route` | `set_routing_layers -signal <range>`, `global_route`, `detailed_route -output_drc ... -output_maze ... -or_seed <seed>`, `estimate_parasitics -global_routing`, `write_def`, and — **only when `request.power` is given** (issue #1091) — `filler_placement`/`global_connect` right after the antenna-repair loop | **no `repair_antenna`** after `detailed_route` (`place_and_route.py:1239-1258`); metal density fill and `DONT_USE_CELLS` remain documented out of scope; filler-cell (gap) insertion is `request.power`-gated, not unconditional |

Metrics are pulled from OpenROAD's own `-metrics <file>.json` channel plus a
documented stdout-scrape fallback for violation *counts*
(`_count_violations`, `place_and_route.py:1326`) — this is exactly the
recommendation the #397 survey's §6 flagged as worth confirming
(**[REPO-RUN]**, since verified true and shipped).

**Where it deliberately stops** (`docs/cli/place-and-route.md` "Out of
scope", updated for issue #1091): metal fill, `DONT_USE_CELLS`-style cell
exclusion, macro-specific PDN grids, IO-ring/footprint floorplanning, and a
second P&R engine. Tapcell insertion and power-grid generation (PDN) were
in this list too when this survey was written; issue #1091 closed that gap
via the optional `request.power` field (see `docs/cli/place-and-route.md`
"Power delivery") — omitted, the prior unconditional-exclusion behavior is
unchanged. Hard-macro placement is explicitly **caller-fixed** (`place_macro
... -exact`, never OpenROAD's automatic `rtl_macro_placer`) — a deliberate
design decision (issue #438), not a gap; this survey does not propose
touching it.

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

### 3.7 Corner-aware slew/capacitance repair — verification spike (issue #2782, part of #2734 item 3)

**Question.** The original #2734 report said library slew/capacitance
violations appear at low-voltage corners and that "no repair pass or request
control exists". Do the existing knobs address that, and is a corner-aware
repair follow-up warranted?

**Evidence tier.** Source and generated-Tcl claims below are
source-inspected (checked against `main` @ `c35b8e1c`). The violation counts
are **measured**, from live runs recorded in "Experiment" below. They are
one small, combinational design on one PDK: they establish the *mechanism*
and a reproducible procedure, not general repair effectiveness.

#### 3.7.1 Which liberty drives each pass (acceptance criterion 1)

Source: `_stage_script_lines` (`src/klayout_tools/place_and_route.py`) and
`_corner_sweep_script_lines` (`src/klayout_tools/place_and_route_sta.py`).
Generated Tcl: `<out>/.klt/place-and-route/pnr_<top>_<stage>.tcl` and
`pnr_<top>_route_corners.tcl` (excerpts from the live runs below).

| Step | Liberty loaded | Role |
|---|---|---|
| `place`: `estimate_parasitics -placement`, `repair_design`, `repair_timing` | the single deck `read_liberty <pdk.corner>` (default: the nominal corner) | **Repair.** Only one corner is ever visible to the optimiser. |
| `cts`: `repair_timing -hold` | same single `pdk.corner` deck | Repair (hold). |
| `route`: `repair_antennas` | same single deck | Antenna repair only; no slew/cap repair runs after routing. |
| `route_corners` sweep (`define_corners` + `read_liberty -corner X` per corner; per-corner `pnr_*_route_corner_<X>.tcl`) | every deck in `pdk.sweep_corners` (default: all shipped) | **Analysis only.** `estimate_parasitics -global_routing`, then `report_check_types -max_slew/-max_capacitance -violators`, once before and once after `set_max_transition`/`set_max_capacitance`. Nothing is modified and the checkpoint is not rewritten. |

`constraints.max_transition_ns` / `.max_capacitance_pf` become
`set_max_transition <v> [current_design]` / `set_max_capacitance <v> ...`
(`_design_rule_constraint_lines`), emitted before `repair_design` in each
stage script. They are one number applied to the one repair deck, and the
same number is re-applied unchanged to every swept corner during analysis.
The limit is a design-wide scalar, not per-corner, and not scaled by
supply voltage.

So the #2734 statement is partly right and partly too broad:

- **Right:** repair is single-corner. No pass repairs against more than one
  deck, and sweep results never feed back into the design.
- **Too broad:** a repair pass and request controls do exist, and
  `pdk.corner` already lets a caller aim the whole repair at a low-voltage
  deck (e.g. `ss_n40C_1v28`). This is undocumented as a repair control: the
  docs describe `pdk.corner` as a "liberty corner selector" and describe
  the sweep, but never state that it picks the repair deck.

#### 3.7.2 Experiment (criteria 2 and 3)

Environment: `openroad 26Q3-1510-g6cb3f2b704`, `yosys 0.67+post`, `klt
0.7.0`, sky130A (`sky130_fd_sc_hd`) from `~/.volare`, host single-corner
local runs, one at a time. No SPICE and no grids were launched. Design:
`tests/corpus/statime/mult8.v` (combinational 8x8 multiplier, 260 cells after
`klt synthesize`; `constraints.clock_port: p[15]`, period 6.0 ns,
`input_delay_ns`/`output_delay_ns` 1.0 ns, `utilization_pct` 38 unless
stated, seed 1, `target_stage: route`). Swept corners (`pdk.sweep_corners`):
`tt_025C_1v80`, `ss_n40C_1v28`, `ss_100C_1v40`, `ff_n40C_1v95`.
Requests differ only in the columns below. Reproduce with
`klt synthesize` on `mult8.v` (as in `scripts/place-and-route-smoke.sh`) and
then `klt place-and-route` with `PDK_ROOT`/`PDK` set; request JSON is
exactly the smoke script's `par_request.json` plus the `sweep_corners`,
delay and `constraints` fields named here.

Violation counts are `effective / library-only` per swept corner
(`corners[].max_transition_violation_count` /
`..._vs_library`). Max-capacitance counts were 0/0 at every corner in every
run, so they are omitted.

| Run | Repair deck (`pdk.corner`) | Constraint | Cells (excl. fillers) | tt | ss_n40C_1v28 | ss_100C_1v40 | ff | tt setup slack (ns) | ss_n40C_1v28 setup slack (ns) | Wirelength (um) |
|---|---|---|---|---|---|---|---|---|---|---|
| A default | tt_025C_1v80 | none | 261 | 0/0 | 0/0 | 0/0 | 0/0 | 0.41 | -24.75 | 5197 |
| B tight, tt repair | tt_025C_1v80 | max_transition 0.5 ns, max_cap 0.05 pF | 261 | 0/0 | **646/0** | **126/0** | 0/0 | 0.41 | -24.75 | 5197 |
| C low-V repair | ss_n40C_1v28 | none | 353 | 0/0 | 0/0 | 0/0 | 0/0 | 0.55 | -23.04 | 5986 |
| D low-V repair, tight | ss_n40C_1v28 | as B | -- (failed) | -- | -- | -- | -- | -- | -- | -- |
| E low-V repair, 20% util | ss_n40C_1v28 | none | 350 | 0/0 | 0/0 | 0/0 | 0/0 | 0.62 | -22.90 | 6303 |
| F low-V repair, tight, 20% util | ss_n40C_1v28 | as B | 892 | 0/0 | **314/0** | **2/0** | 0/0 | -19.30 | -157.41 | 20919 |

Run D aborted in `place` with `[ERROR DPL-0038] Utilization greater than
100%, impossible to legalize`: the repair under a tight limit grew the design
past the floorplan, so the 38%-utilization floorplan of A-C could not
absorb it. F reran D with a 20% floorplan.

Findings:

1. **The original claim is not reproducible as a library violation.** In
   every run the library-only counts (`..._vs_library`) were 0 at every
   corner, including `ss_n40C_1v28` (the lowest-voltage deck) after an
   unconstrained tt repair (A). The sky130_fd_sc_hd decks on this design do
   not break their own slew/capacitance limits at low voltage, so there was
   no library-limit gap to close here. The reported observation stays
   unverified; it may need a design with high-fanout or long-wire nets, or a
   different library. This criterion is satisfied only as "not reproduced
   on `mult8`/sky130_fd_sc_hd", not as "cannot occur".
2. **A cross-corner violation *is* reproducible, but only against a
   caller-stated limit (B).** A tt-only repair against a 0.5 ns guard-band
   passes at tt (0) and leaves 646 / 126 effective violations at the two
   slow corners, because slew grows as supply falls and the limit is one
   fixed number. Run B's cell count, timing and wirelength equal A's to the reported
   precision, consistent with `repair_design` having nothing to do at tt. This shows a single-corner
   repair gives no information about other corners. The existing response
   fields (`corners[]`, `..._vs_library`) detect it correctly.
3. **Aiming the existing knob at the low-voltage deck repairs more but is
   not bounded (C, E, F).** With no override, repairing at `ss_n40C_1v28`
   (C) added about 90 cells, improved the slow-corner setup slack by about
   1.7 ns and tt setup slack by 0.14 ns, and cost about 15% wirelength;
   reported power fell 0.50 to 0.32 mW (estimate, the sizing differs). With
   the tight guard-band (F) the same pass inflated the design to 892 cells,
   87% utilization, 4x wirelength, worst setup slack
   -157 ns (from -25), and still left 314 violations at `ss_n40C_1v28`. Setup
   timing was not met in A either (the multiplier cannot meet 6 ns at ss), so
   timing deltas here mainly show that tight slew targets at an infeasible
   corner make repair diverge, not that D/F "break" a closing design.
4. **Not measured:** the `ss_n40C_1v28` slew limit at a different tight value,
   other designs, other seeds, `max_capacitance_pf` (never violated), and the
   effect of the pass on a design that is otherwise timing-clean. No
   conclusion is drawn about repair effectiveness beyond this mult8 data.

#### 3.7.3 Recommendation (criterion 4)

**Existing knobs suffice for now; no new repair flag or optimiser is
warranted by this evidence.** Specifically:

- The library-limit gap in the original report is not reproduced (finding 1),
  so a corner-aware repair has no demonstrated failing case to fix.
- The cross-corner gap that *is* reproduced (finding 2) is already detected
  by `corners[]` and the effective/library-only violation counts.
- The existing remedies are `pdk.corner` (choose the repair deck) and
  `constraints.max_transition_ns`/`.max_capacitance_pf` (choose the target),
  plus re-running with a different `pdk.corner`. Finding 3 shows these can
  overshoot, so the work needed is **documentation** (done in this change:
  `docs/cli/place-and-route.md`), not a new mechanism.

**Reopen conditions for a scoped follow-up.** Open a follow-up only if a
reproducer shows a *library-only* (`..._vs_library` > 0) violation at a
swept corner that survives repair at the best single `pdk.corner`. If so,
the bounded shape would be (a design sketch, not a promise):

1. One extra repair-and-reanalyze round at most (no loop), triggered only
   by `..._vs_library` > 0 at a swept corner, repairing against the
   worst-violating swept deck via the existing `repair_design` command.
2. Stopping rules: stop on zero library-only violations, or when the round
   fails to reduce the violation count, or when cell count grows beyond a
   caller-given cap (finding 3, run F, and the DPL-0038 failure in D show
   why a cap is needed), or when setup WNS at the repair corner worsens.
3. Residual reporting: always report the post-round per-corner effective and
   library-only counts and the stop reason, with `null` where not measured.
   Do not promise zero violations at every corner; some corner/limit pairs
   are infeasible (F).
4. Keep the sweep analysis-only unless the round is explicitly requested.

No such follow-up is opened by this spike because the trigger condition has
not been observed.

#### 3.7.4 Limitations

- One design, one library, one seed; run B equals run A exactly, so the
  tight-limit effect at tt is a null result, not a tuned one.
- The slew limit applied to every corner is the same absolute 0.5 ns; no
  per-corner or voltage-scaled limit was tried.
- Slack figures include a design that cannot meet its clock at ss; they
  indicate repair behaviour, not timing closure.
- Raw reports and generated Tcl were kept in a scratch directory and are not
  committed.

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

## 6. Design spike: P&R iteration cost and safe checkpoint reuse (issue #2783)

Part of #2734 (original item 4). **Status: design spike, docs only. No
persistent cache is implemented.** The reporter's "~30 minutes for a
16-corner run" came from an older build on an unknown design. On current
main, a full 16-corner run of the `mult8` fixture takes 58.6 s on the host
described in 6.2. Measurements: 6.2. Recommendation: stop, with no caching
follow-up (6.4).

### 6.1 What already exists on `main` (verified by source inspection, 0e952ded)

| Capability | Where | What it gives an iterating caller |
|---|---|---|
| `pdk.sweep_corners` | `place_and_route.py` / `place_and_route_sta.py` (`_run_corner_sweep`); `docs/cli/place-and-route.md`, "Per-corner breakdown ... `pdk.sweep_corners`" | Scopes the post-route multi-corner sweep to a named subset of the library's shipped corners. Unresolvable names fail before any engine launch. The docs state an unscoped N-corner sweep pays one combined invocation plus one extra engine launch per corner. |
| `target_stage` | same | Stops after `floorplan`/`place`/`cts`/`route`; one engine process per stage. |
| Stage ODB checkpoints | `place_and_route.py` (`<top>_<stage>.odb` under `<request dir>/.klt/place-and-route/`) | Each stage `read_db`s the previous stage's checkpoint **within one request**. Nothing reads a checkpoint written by an earlier request. |
| Standalone routed-artifact STA | `klt sta` (`docs/cli/sta.md`, `pdk.corner` / `pdk.corners`, `post_route_sta.py::_validate_corners`) | Re-times an already-routed DEF (optionally with caller SPEF) at a caller-selected corner list with no placement/CTS/route. The DEF is loaded fresh per corner, so every corner sees identical geometry. |

Selected-corner recipe (already sufficient for timing iteration):

1. Run `klt place-and-route` once, with `pdk.sweep_corners` naming only the
   corner(s) the design operates at (or a single sign-off corner).
2. Keep the routed `.def`. Set `post_route_spef: true` in step 1 when the
   numbers must include real wire parasitics.
3. For every further corner or constraint-only tweak that does not move
   geometry, call `klt sta` with `pdk.corners: [...]` on that DEF. Pass
   `spef` (without it `klt sta` attaches no wire RC and is optimistic,
   see 6.2.4), and set `constraints.propagated_clock` for a sequential
   design. This never re-runs floorplan/place/CTS/route.

When this already meets the need: the question is "what does this *routed*
design do at other corners / other SDC-style constraints". It does **not**
meet the need when the iteration changes anything that feeds
implementation (utilization, floorplan, PDN, macros, placement or CTS
options, timing-driven constraints, engine build): those require a real
re-implementation, and a cache can only help if it can prove that the
unchanged *prefix* of stages is still valid.

How the two paths differ, measured in 6.2.4 rather than assumed:
(a) parasitics: the in-flow sweep re-reads the route ODB in a fresh session
and runs `set_wire_rc -layer met2` + `estimate_parasitics -global_routing`.
The route stage's own top-level numbers come from `estimate_parasitics
-global_routing` in the session that just ran `global_route`, with no
`set_wire_rc`. `klt sta` attaches no wire parasitics unless a `spef` is
supplied. (b) clock: every in-flow timing session uses an ideal clock (no
`set_propagated_clock` in any flow script), while `klt sta` propagates the
clock only when `constraints.propagated_clock` is set. (c) SDC: the clock,
input delays and output delays are the same in both when the caller passes
the same `constraints`. The flow's `set_dont_use` lines do not affect
timing. (d) a repeated route at the same request reproduced the routed ODB
byte for byte here (6.2.4). The `docs/cli/sta.md` warning that "re-running
`place-and-route` per corner produces N different placements" applies to
changing the *implementation* corner (`pdk.corner`, which drives repair and
timing-driven stages, see 3.7.2 runs A vs C). It does not apply to repeating
an identical request.

### 6.2 Measurement

#### 6.2.1 Environment and identity

- Host: Apple M3 Ultra (arm64), macOS (Darwin 27.0.0), 2026-10-12. Docker
  Engine 29.8.2.
- `klt 0.7.0+g6397bb220748`: this PR's tree, which is `origin/main`
  `ede0fdad` plus docs. Run as `<worktree>/.venv/bin/klt`. Every response's
  `provenance.klt_version` reports this sha. The globally installed
  `~/.local/bin/klt` on this host is a stale, non-editable 0.7.0 build
  (no `clock_mode` in `klt sta` responses), so it was not used for any
  number below.
- OpenROAD: `openroad` on `PATH` is `~/.local/bin/openroad`. It is not a
  native build. It is a bash wrapper (the local variant of
  `scripts/install-openroad-docker.sh`'s generated wrapper) that runs
  `docker run --rm -i --platform linux/amd64 ... openroad/orfs:latest
  .../OpenROAD/bin/openroad`, which emulates amd64 on this arm64 host. Image
  `openroad/orfs:latest` is `sha256:0f1f4f03792e4825a917f1802cb415da6c4a372f6b24bee0b403f7efe62f6d30`
  (created 2026-10-08). `openroad -version` prints `unknown`, so the image
  digest is the engine identity. The wrapper mounts `$PWD`, the resolved
  `TMPDIR`, and `$PDK_ROOT` (read-only) *only when `PDK_ROOT` is set*. Two
  consequences: every `klt` run must be started from a directory that
  contains its inputs, and `PDK_ROOT` must be exported.
- Yosys 0.69+post (`143eb14f`), host-native.
- PDK: sky130A at `~/.volare/sky130A` (`open_pdks
  c6d73a35f524070e85faff4a6a9eef49553ebc2b`), selected with
  `PDK_ROOT=~/.volare PDK=sky130A`. `klt pdk find` reports `resolved_via:
  "PDK_ROOT environment variable"`. Without these variables `find_pdk()`
  picks gf180mcuA from `~/.ciel` first, which is the documented resolution
  order and not a missing PDK. Library `sky130_fd_sc_hd`, 16 non-ccsnoise
  liberty corners.
- Fixtures: `tests/corpus/statime/mult8.v` (combinational 8x8 multiplier,
  261 cells excluding fillers after route) and
  `examples/functional-verification/gcd.v` (sequential, 504 cells excluding
  fillers). Each was synthesized with `klt synthesize` using the
  `scripts/place-and-route-smoke.sh` synth request (5.3 s and 5.5 s wall).
- Request: exactly the smoke script's `par_request.json` (utilization 38,
  aspect 1.0, margin 2.0 um, `unithd`, met3/met2 IO, seed 1, `target_stage:
  route`, `pdk.corner: tt_025C_1v80`), plus the #2782 timing fields
  `constraints.input_delay_ns: 1.0` and `output_delay_ns: 1.0` so that slacks
  are finite. Clocks: mult8 `p[15]` at 6.0 ns, gcd `clk` at 1.1 ns. Only
  `pdk.sweep_corners` and `post_route_spef` vary between runs, as named in
  the tables.
- Commands: `PDK_ROOT=~/.volare PDK=sky130A klt place-and-route par.json
  --format json`, run from the run directory, and `klt sta sta.json --format
  json` with `def: ../.klt/place-and-route/<top>.def` and the same
  `constraints`, run from the P&R run directory so that the wrapper mounts
  the DEF.
- Timing method: a pass-through shim earlier on `PATH` logged the wall-clock
  of every `openroad` invocation (script name, start, end, exit code) and
  then delegated to the wrapper. "klt wall" is the wall-clock of the whole
  `klt` process. One run per configuration unless noted, run one at a time
  on an otherwise idle host. Repeats of the same configuration varied by
  about 10% in wall-clock (the two 3-corner rows), so read differences under
  about 2 s as noise.

#### 6.2.2 Cold setup: engine launch and liberty parse (sky130, this host)

| Probe | Through the wrapper, host wall (s) | Inside the container (s) |
|---|---|---|
| `docker run ... openroad/orfs:latest true` (no OpenROAD) | 0.24, 0.25, 0.26 | -- |
| `openroad -no_init -exit` on an empty script | 0.59, 0.64, 0.72 | 0.25, 0.25, 0.26 |
| `read_liberty` of `tt_025C_1v80` (one deck) | 1.12, 1.21, 1.44 | 0.56, 0.56, 0.56 |
| `read_liberty` of all 16 `sky130_fd_sc_hd` decks in one process | -- | 4.73, 4.74, 4.79 |

Reading: each `openroad` launch through the wrapper costs about 0.6 s before
any work. Of that, about 0.35 to 0.45 s is container and Docker CLI
overhead, and about 0.25 s is the emulated OpenROAD start. Parsing a sky130
hd liberty deck adds about 0.3 s per deck in-container. Each `klt` process
also pays about 1.6 s of Python start-up (`klt --version`: 1.55, 1.64,
1.73 s) and one `openroad -version` probe of 0.5 to 1.1 s per request.

The gf180mcu probe in the first revision of this spike is kept below for the
record. **It ran through the same Docker wrapper (emulated amd64), so it
measures Docker launch plus emulated start-up as much as OpenROAD.** The
"engine start" rows are entirely launch overhead. The extra 10 s of the
15-process row over the one-process row is 14 extra launches (about 0.7 s
each), not OpenROAD work. Its "cannot read file" errors on `~/.volare` were
not a sandbox restriction. `PDK_ROOT` was unset, so the wrapper never
mounted the PDK, and the probe worked only after the files were copied
under the mounted working directory. Do not use this table for stage or
analysis cost.

| gf180mcu probe (Docker-wrapper-inclusive, 3 runs unless noted) | Wall-clock (s) |
|---|---|
| Engine start, empty script | 0.61, 0.64, 0.63 |
| Engine start, `-exit` with no script | 0.73, 0.68, 0.63 |
| `read_liberty` of one corner (`tt_025C_5v00`, ~20 MB) | 1.19, 1.21, 1.23 |
| `read_liberty` of all 15 shipped corners in **one** process | 9.83, 9.79, 10.64 |
| 15 separate processes, one corner each (1 run) | 19.8 total |

#### 6.2.3 Stage and corner-sweep cost through `klt place-and-route`

Per-invocation wall-clock (s), wrapper launch included in every cell.
"Sweep" is the combined all-swept-corner session (`pnr_<top>_route_corners.tcl`)
plus the per-corner sessions (`pnr_<top>_route_corner_<c>.tcl`, one per
swept corner when more than one is swept). "Engine sum" also includes the
`openroad -version` probe.

| Run | `sweep_corners` | floorplan | place | cts | route | `post_route_spef` engine | Sweep: combined + per-corner | Engine sum | klt wall |
|---|---|---|---|---|---|---|---|---|---|
| mult8 s0 | `[]` | 1.26 | 3.55 | 1.55 | 12.25 | -- | -- | 19.53 | 27.70 |
| mult8 s1 | tt | 1.45 | 3.56 | 1.81 | 11.86 | -- | 1.73 + 0 | 21.48 | 29.48 |
| mult8 s3 | tt, ss_n40C_1v28, ff_n40C_1v95 | 1.88 | 4.10 | 1.81 | 11.97 | -- | 3.27 + 5.61 (3) | 29.50 | 39.26 |
| mult8 s3, repeat | same | 1.50 | 3.52 | 1.62 | 11.54 | -- | 2.21 + 4.95 (3) | 26.38 | 36.70 |
| mult8 all | omitted (all 16) | 1.47 | 3.44 | 1.85 | 11.84 | -- | 6.11 + 23.05 (16) | 48.50 | 58.63 |
| mult8 s3 + SPEF | 3, `post_route_spef: true` | 1.26 | 3.53 | 1.44 | 11.62 | 1.23 | 1.86 + 3.95 (3) | 25.54 | 49.67 |
| gcd s3 + SPEF | 3, `post_route_spef: true` | 1.12 | 4.80 | 1.47 | 44.91 | 1.52 | 1.93 + 4.27 (3) | 60.73 | 124.31 |

Findings:

1. **Cold setup versus repeated work.** Every stage is a separate engine
   launch, so a request pays about 0.6 s of wrapper launch per stage, a
   liberty parse per stage, about 1.6 s of `klt` start-up and one version
   probe. That is roughly 6 s of the 27.7 s mult8 `[]` run. The
   implementation work itself is about 18 s for mult8 and about 52 s for
   gcd. Detailed routing dominates both (about 12 s and 45 s). `klt`'s
   non-engine time (klt wall minus engine sum) is 8 to 10 s without SPEF.
   `post_route_spef` adds about 14 s of host-side time for mult8
   (24.1 s versus 9.8 to 10.3 s), which is consistent with KLayout parasitic
   extraction (not separately attributed). gcd's 63.6 s host-side time with
   SPEF was not broken down.
2. **Per-corner sweep cost** is about 1.4 to 1.9 s of engine time per swept
   corner. One swept corner adds 1.7 s (one combined session). Three add 5.8
   to 8.9 s. All 16 add 29.2 s (6.1 s combined, 23.1 s in the 16 per-corner
   sessions, about 1.44 s each). Klt wall rises 27.7, 29.5, 36.7 to 39.3, and
   58.6 s for 0, 1, 3 and 16 corners. With more than one swept corner, each
   corner's liberty is parsed twice (once in the combined session, once in
   its own session), so 23 of the 29 s of a 16-corner sweep is per-corner
   re-launch and re-parse, not new analysis. That is the only repeated work
   inside a request that the data shows to be material.
3. **The 30-minute, 16-corner figure does not reproduce on mult8.** A full
   16-corner mult8 run takes 58.6 s wall, including about 31 s of sweep.
   The original design and build are unknown, so this neither confirms nor
   refutes the report for a larger design. It does show that on current
   main the per-corner cost is seconds, not minutes, for a small block.

#### 6.2.4 Routed-artifact `klt sta` versus the in-flow sweep (Test Plan item 2)

`klt sta` was run on the routed DEF of the "s3 + SPEF" runs above, with the
same `constraints` and with `pdk.corners` set to the corners named. Wall (s)
is the whole `klt sta` process. Engine (s) is the sum of the per-corner
OpenSTA sessions (one launch each), excluding the version probe.

| `klt sta` request | mult8 wall | mult8 engine | gcd wall | gcd engine |
|---|---|---|---|---|
| 1 corner (tt), no SPEF | 3.77 | 1.16 | 6.53 | 1.50 |
| 3 corners, no SPEF | 8.14 | 3.64 | 12.70 | 4.92 |
| 3 corners, `propagated_clock` | 8.77 | 3.79 | 14.70 | 4.94 |
| 3 corners, `spef` | 11.47 | 4.77 | 16.07 | 5.78 |
| 3 corners, `spef` + `propagated_clock` | 13.02 | 4.88 | 15.90 | 6.15 |
| all 16 corners, no SPEF | 41.50 | 22.30 | -- | -- |

Worst slack (ns), setup / hold, on the same routed geometry. The routed
ODB from which the sweep ran and the DEF that `klt sta` read come from the
same run. "GR estimate" means `estimate_parasitics -global_routing`.

| Source | Parasitics | Clock | tt | ss_n40C_1v28 | ff_n40C_1v95 |
|---|---|---|---|---|---|
| **mult8** route stage, top level (tt only) | GR estimate, route session | ideal | 0.491 / 2.178 | -- | -- |
| mult8 in-flow sweep (`corners[]`) | `set_wire_rc met2` + GR estimate, fresh session | ideal | 0.408 / 2.178 | -24.872 / 2.943 | 1.853 / 2.116 |
| mult8 in-flow `spef_sta` (tt only) | KLayout SPEF | ideal | 0.359 / -- | -- | -- |
| mult8 `klt sta` | none | ideal | 0.641 / 2.146 | -23.605 / 2.742 | 1.983 / 2.095 |
| mult8 `klt sta` | SPEF | ideal | 0.359 / 2.176 | -25.263 / 2.953 | 1.808 / 2.114 |
| mult8 `klt sta` | none or SPEF | propagated | identical to the ideal row with the same parasitics | | |
| **gcd** route stage, top level (tt only) | GR estimate, route session | ideal | -1.888 / 0.448 | -- | -- |
| gcd in-flow sweep (`corners[]`) | `set_wire_rc met2` + GR estimate, fresh session | ideal | -1.901 / 0.460 | -21.366 / -1.284 | -0.781 / 0.282 |
| gcd in-flow `spef_sta` (tt only) | KLayout SPEF | ideal | -1.995 / -- | -- | -- |
| gcd `klt sta` | none | ideal | -1.687 / 0.431 | -19.159 / -1.284 | -0.616 / 0.263 |
| gcd `klt sta` | none | propagated | -1.699 / 0.438 | -19.143 / -2.689 | -0.629 / 0.282 |
| gcd `klt sta` | SPEF | ideal | -1.995 / 0.469 | -22.516 / -1.284 | -0.831 / 0.287 |
| gcd `klt sta` | SPEF | propagated | -2.017 / 0.362 | -22.528 / -3.333 | -0.861 / 0.315 |

What explains the differences:

- **Parasitics set the setup order.** Within each design, setup slack
  orders by how much wire RC each path attaches. `klt sta` without SPEF
  attaches no wire parasitics and is the most optimistic (mult8 tt 0.641).
  The route session's global-route estimate gives 0.491. The sweep's fresh
  `set_wire_rc met2` estimate gives 0.408. Extracted SPEF is the most
  pessimistic (0.359). The sweep and the route stage disagree at the *same*
  corner (0.408 versus 0.491 for mult8) because their parasitic setup
  differs (6.1 (a)), not because the geometry does. Without a SPEF, `klt
  sta` is therefore not a substitute for the sweep's numbers. It is
  optimistic relative to the sweep by 0.13 to 2.2 ns of setup slack on
  these designs, and the gap is largest at the slow corner.
- **With the same SPEF, the two paths agree exactly.** `klt sta` + SPEF
  at tt reproduces the in-flow `spef_sta` value to the reported precision
  (mult8 0.35865, gcd -1.99475). This is the replay-equivalence check the
  spec in 6.5 and 6.6 asks for, and on these two designs it holds today
  without any new code.
- **The annotation verdicts disagree, though the numbers match.** For the
  same SPEF, the in-flow `spef_sta.annotation_complete` is `true`, while
  `klt sta`'s `spef_annotation.annotation_complete` is `false`. The reason
  is that `klt sta` also counts `read_spef` "net not found" warnings for the
  extractor's internal `$NNNN` nets (378 for mult8, 815 for gcd), and for
  gcd it found 3 unannotated drivers. A caller comparing the two paths
  should read the evidence fields, not the boolean. This is a gap between
  the two checks, and it is noted here only, not addressed.
- **Clock mode matters for hold on a sequential design.** Every in-flow
  session uses an ideal clock. For gcd, `propagated_clock` moves the slow
  corner's worst hold from -1.284 to -2.689 ns without SPEF (-3.333 with
  SPEF), as clock network latency reaches 1.66 to 1.79 ns at ss_n40C_1v28.
  The in-flow sweep's hold numbers therefore understate this design's
  slow-corner hold problem. mult8 has no clock sinks (`p[15]` is a
  virtual-clock output reference, and `clock_propagation.clock_sinks` is 0),
  so propagation changes nothing there, as expected.
- **A repeated route did not differ.** The routed `mult8_route.odb` was
  byte-identical (same SHA-256) across all six mult8 runs above, in six
  different directories. The DEF was identical apart from its date line.
  Slacks and wirelength (5032 um) were identical too. With an unchanged
  request, seed and image on this host, a second full route reproduced the
  first exactly. This is one design, one thread setting and one engine
  build. It is evidence toward, not proof of, the determinism that 6.6
  item 5 asks for.
- **Cost.** Re-timing 3 corners with `klt sta` took 8 to 16 s wall, versus
  37 to 50 s for re-running the whole mult8 request and 124 s for gcd with
  SPEF. For 16 corners the saving is small for mult8 (41.5 s versus 58.6 s),
  because `klt sta` pays a full launch, liberty parse and about 1.1 s of
  host-side `klt` work per corner (about 2.6 s per corner wall for mult8,
  4 to 5 s for gcd at 3 corners).

### 6.3 Which work repeats on main

- **Across requests: all of it.** A new `klt place-and-route` request
  re-runs every requested stage from the synthesized netlist. The previous
  request's ODB files are overwritten in the fixed `<request
  dir>/.klt/place-and-route/` directory and are never consumed. Measured
  cost on this host: about 18 s of implementation engine time for mult8 and
  about 52 s for gcd, dominated by detailed routing (6.2.3).
- **Within a request:** each stage is a separate engine launch that
  re-issues `read_liberty` and `create_clock` (about 0.6 s wrapper launch
  plus about 0.3 s parse per stage here). With more than one swept corner,
  the sweep runs one combined session plus one session per corner,
  re-parsing every swept deck twice (23 of 29 s of a 16-corner mult8 sweep).
- **Via `klt sta`:** only the corner analysis repeats, by design. It costs
  about 1.2 to 2.2 s of engine time and 2.6 to 5.4 s of wall per corner
  here (`klt` start-up included).

### 6.4 Recommendation

**Stop: no caching or resume follow-up is warranted on the measured
data.** The 6.6 item-0 gate was "if cold setup plus corner analysis is under
a few seconds per corner, stop: the existing recipe suffices". Measured: in
the flow, 1.4 to 1.9 s of engine time per swept corner. Through `klt sta`,
1.2 to 2.2 s of engine time and 2.6 to 5.4 s of wall per corner (`klt`
start-up included), of which about 0.6 s per launch is this host's Docker
wrapper. Both are within that bar. The selected-corner recipe in 6.1 already gives the reuse the issue
asks for. Run `place-and-route` once with a narrow `sweep_corners` (and
`post_route_spef: true` when real parasitics matter), then re-time other
corners with `klt sta` on the routed DEF, passing the SPEF and, for
sequential designs, `constraints.propagated_clock`. With the same SPEF this
reproduces the in-flow `spef_sta` number exactly (6.2.4).

Reasons not to build more now:

1. Analysis-only replay (the routed bundle with a manifest in 6.5) would
   add validation and provenance, not speed. The analysis it replays is
   already the cheap part, and `klt sta` already re-times a fixed routed
   artifact.
2. Validated checkpoint resume could at most skip floorplan, place and CTS
   (6.4 s for mult8, 7.4 s for gcd). Detailed routing, the dominant cost,
   would still re-run for any route-affecting change. That is too little
   gain to justify the invalidation surface in 6.5.
3. The 30-minute report did not reproduce at fixture scale (58.6 s for a
   full 16-corner mult8 run).

If this is reopened, analysis-only replay remains the preferred shape over
stage resume, for the reasons in 6.5. Reopen it when a measured design shows
corner analysis well above a few seconds per corner, or implementation
prefixes (floorplan to CTS) costing minutes. Cheaper levers come first,
without any cache:

- (a) avoid the doubled liberty parse in multi-corner sweeps (the combined
  session plus per-corner sessions, 6.2.3 finding 2);
- (b) document that `klt sta` without SPEF is optimistic and that the
  in-flow sweep is ideal-clock (6.2.4).

Neither is filed by this spike.

### 6.5 Specification (applies to the analysis-replay bundle; a future stage resume must meet a strict superset)

**Manifest (`schema_version`-ed, in the shared envelope's `provenance`
style):** SHA-256 of every input file consumed (RTL/netlist, constraints /
SDC text, floorplan and PDN inputs, macro LEF/lib/GDS, tech LEF, every
liberty deck loaded, the DEF/ODB bundle members); resolved `find_pdk()`
triple and PDK content digest (not just the path); engine identity
(`openroad -version` string plus binary SHA-256, since this build reports
`unknown`); `klt` version and git sha; the canonicalised request (all stage
options, `target_stage`, `sweep_corners`, seeds, thread count, platform
overrides); the stage reached; a manifest digest over all of the above; and
the list of produced artifacts with their own digests.

**Invalidation (any one mismatch means reject, never partial reuse):**

| Changed | Effect |
|---|---|
| Any input file digest (RTL, netlist, SDC/constraints) | Reject; full re-run (resume) or re-time with new constraints only if the bundle is analysis-only and the changed item is declared analysis-only (see below). |
| PDK, tech LEF, cell LEF, or liberty content | Reject. A *new corner within the same library* is allowed for replay only if that deck's digest is recorded in the replay result; the bundle's own implementation inputs must be unchanged. |
| Engine build digest or `klt` version | Reject for resume. For replay: reject unless the same engine can still read the bundle (version-checked on load), and always record both identities. |
| Floorplan, PDN, macro placement, IO placement inputs | Reject (implementation-affecting). |
| Stage options (utilization, density, CTS/route flags, seeds, thread count) | Reject for resume of that stage and every later stage; irrelevant to replay except where they alter the routed artifact, which they cannot since it is the input. |
| Constraints | A constraint is *analysis-only* only if listed in a closed allow-list (for example clock period for timing-only checks and I/O delays, once reported with clearly different semantics); anything else, including anything that the flow's timing-driven placement or repair consumed, is implementation-affecting. Default: implementation-affecting. |
| Checkpoint compatibility | Each bundle member carries a format tag (ODB/engine db version). Unknown or older/newer tag means reject. |

**Rejection behavior:** fail closed, before any engine launch where
possible, with a JSON error in the shared envelope (`docs/json-contract.md`
error shape) carrying a stable `code` (suggested: `bundle_stale`,
`bundle_incompatible`, `bundle_corrupt`, `bundle_locked`), the first
mismatching manifest field, expected vs actual digest, and the remediation
("re-run place-and-route"). No silent fallback to a full re-run unless the
caller passes an explicit opt-in flag, so a cache can never change a
verdict invisibly. Corrupt bundles (truncated member, digest mismatch,
unparsable manifest) are rejected the same way and never deleted
automatically.

**Provenance retained:** every replay result embeds the full bundle
manifest digest and the bundle's origin run (stage reached, original
request digest, original engine), plus the replay's own engine, corner
decks (digest each) and wall-clock, in the `provenance` block, with an
explicit `source: "replay"` marker so a consumer can never confuse replayed
timing with a fresh implementation run. The original run's response and
metrics files are copied into the bundle unmodified, not regenerated.

**Concurrency:** bundles are write-once and immutable. Writers create the
bundle in a temp directory beside the destination and publish with an
atomic rename, so readers see either nothing or a complete bundle. Same
destination, two writers: second publisher fails with `bundle_locked`
(no overwrite). Readers never take a write lock. The existing fixed
`.klt/place-and-route/` output directory is a separate hazard owned by
**#2770** (output-directory lock). #2770 is **open and unmerged** as of this
spike (checked 2026-10-12), so this proposal must not depend on it: the
bundle publish protocol above is self-contained. If #2770 lands first, the
producer step should take its lock around reading the run directory into
the bundle, so a concurrent run cannot interleave members.

### 6.6 Implementation and test plan (not implemented here; parked by 6.4)

This plan is **not** scheduled. Item 0 has been run (6.2), and its stop
rule fired (6.4). Items 1 to 5 are kept as the ready-made plan for the case
where a measured design reopens the question.

0. **Gate: measure. Done (6.2).** Commands, `klt` sha, engine image digest,
   PDK version, per-stage and per-corner wall-clock, `klt sta` time on the
   routed DEF, and the `klt sta` versus in-flow slack comparison, with the
   parasitic and clock differences explained, are in 6.2.1 to 6.2.4. Result:
   corner analysis costs seconds per corner, so the existing recipe
   suffices. To reopen, rerun the same measurement on the larger design and
   compare it against the reopen criteria in 6.4.
1. Add manifest + digest utilities (pure Python, no engine), with unit
   tests for canonicalisation (key order, path-independence).
2. Bundle writer (atomic publish) and reader/verifier; wire the producer
   into `place-and-route` behind an explicit request field (default off,
   so existing responses are byte-identical).
3. Analysis replay path reusing `klt sta`'s session code; envelope fields
   `source: "replay"`, manifest digest; docs in `docs/cli/sta.md` and
   `docs/json-contract.md` if the envelope grows.
4. Tests (all runnable without the engine unless marked engine-gated):
   - stale input: modify RTL/SDC/liberty byte; expect `bundle_stale` naming
     the field, no engine launched;
   - changed constraints, PDK content, engine identity, `klt` version,
     stage option: each rejected with the specific field (parametrised
     table mirroring 6.5);
   - analysis-only constraint allow-list: an allowed change replays and
     records it; a disallowed one rejects;
   - corrupt bundles: truncated ODB member, flipped digest, invalid JSON
     manifest, missing member, unknown format tag; all `bundle_corrupt` /
     `bundle_incompatible`, nothing deleted;
   - concurrent writers: two processes publishing to one destination,
     exactly one succeeds, loser gets `bundle_locked`, destination is
     never partial; reader during publish sees nothing or the full
     bundle (loop/stress test);
   - interplay with #2770: producer under an output-dir lock does not
     deadlock; skipped/xfail until #2770 lands;
   - engine-gated equivalence: replayed slack at a corner versus a fresh
     `klt sta` on the same DEF is equal within the documented tolerance,
     and both are compared (not asserted equal) to the in-flow sweep.
5. Stage resume: not filed. It would need a determinism study first (same
   inputs, same engine, repeated runs, byte-compare of checkpoints). 6.2.4
   has a first data point: six identical mult8 requests produced
   byte-identical route ODBs on one engine build. A real study would also
   vary thread count, host and image.

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
