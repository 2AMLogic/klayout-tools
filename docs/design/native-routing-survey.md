# Survey & proposal: native-Rust detailed routing for `klt place-and-route`

**Status:** research / proposal, no implementation. Filed for issue #934, a
research-and-propose task under [Epic #700](https://github.com/2AMLogic/klayout-tools/issues/700)
Phase 2 ("place-and-route for `klt` — synthesis→placement→routing→GDS in
Rust, OpenROAD-validated"). This document plays the same role for Phase 2
that
[`docs/design/place-and-route-improvements-survey.md`](place-and-route-improvements-survey.md)
(#735) played for Phase 1 — it is the "fresh eyes" input a later Champion
pass decomposes into dispatchable sub-issues, and it does **not** authorise
implementation of anything below.

**Phase 1's own result is load-bearing context for this document, not
background colour.** Phase 1 shipped three sub-issues: #783 (CTS
sink-clustering, shipped), #785 (a native-Rust FLUTE/RUDY congestion
pre-check, shipped as a research spike — validated library, not wired into
the fleet gate), and **#784 (a native-Rust standard-cell legalizer,
No-go)** — QoR (HPWL, total displacement) 21.8–403.4% worse than OpenROAD's
own `detailed_placement` on the two-design corpus slice, root-caused to
row-assignment search depth, documented in `native/legalize/README.md`.
Epic #700's own Champion tracking comment (2026-08-12) left the direction
question open — "retry native placement" vs. "proceed to native routing on
top of OpenROAD-placed layouts" — and created this issue to produce the
survey needed to decide, rather than assuming an answer. **This document's
own recommendation (§4) is: proceed to routing, but stage it far more
cautiously than Phase 1 staged placement**, precisely because the one
native-Rust P&R sub-problem attempted so far missed its QoR bar even though
it was, by design, the *easiest* sub-problem in the whole pipeline (§2.2 of
the Phase 1 survey called this out explicitly in advance: "the smallest,
best-bounded sub-problem... with a crisp, checkable objective"). Detailed
routing has none of those three properties — it is the least-bounded,
least-crisp-objective sub-problem in this pipeline. This document treats
that as a warning about scope, not a reason not to proceed.

**Required prior art, read first, not re-derived here:**

- [`docs/design/place-and-route-improvements-survey.md`](place-and-route-improvements-survey.md)
  (#735) — Phase 1's own survey; §2.3 already named TritonRoute as the
  detailed router this command wraps, and §1's baseline table is the direct
  ancestor of §1 below (re-verified against the current tree, not
  re-derived from memory — the route stage has changed materially since
  #735 was written, see §1's own diff note).
- `native/legalize/README.md` (#784) — the Phase 1 spike whose failure
  mode (§2.2/§4 below) most directly informs this document's staging
  recommendation.
- `docs/design/flute-congestion-precheck-results.md` (#785) — the Phase 1
  spike whose *packaging* pattern (pyo3 crate, validated-but-not-wired
  research-spike verdict) this document's §4 items reuse, and whose own
  correlation study already produced the one piece of real evidence this
  survey has for a detailed-routing QoR/runtime gap (§3.4 below).
- [`docs/design/openroad-invocation-survey.md`](openroad-invocation-survey.md)
  (#397) — the accepted survey of OpenROAD's own invocation surface.
- [`docs/cli/place-and-route.md`](../cli/place-and-route.md) — the
  command's own contract documentation, ground truth for §1.
- [`docs/design-evidence-tiers.md`](../design-evidence-tiers.md) — the
  evidence-tier discipline this document follows.

## Evidence-tier discipline

Following this repo's own convention (`docs/design-evidence-tiers.md`;
the Phase 1 survey's own tiering): this task did not run OpenROAD or any
corpus benchmark of its own — it is pure analysis, per its own Definition
of Done, reusing real measured data already captured by #785's accepted
correlation study where relevant. Every claim below is one of:

- **[REPO]** — read directly from this repo's own source/docs, cited by
  file and line, re-verified against the current tree (commit
  `5cdb88a`, 2026-08-12 — see the worktree's own base commit) rather than
  assumed unchanged from the Phase 1 survey.
- **[REPO-RUN]** — a **real** result already captured by another accepted
  document in this repo, not re-derived from memory.
- **[LIT]** — a technique or finding from the published EDA-CAD literature
  or OpenROAD's own documented command surface, cited by name/venue/year to
  the best of this survey's ability without live network/library access in
  this task. Treat exact author lists as best-effort — verify against the
  primary source before citing in a paper trail that requires precision.
- **[PROPOSAL]** — this document's own reasoning/recommendation, not a
  claim about the world.

No item below rests solely on an uncited assertion.

## 1. Baseline: what `klt place-and-route`'s `"route"` stage does today

**[REPO]**, `src/klayout_tools/place_and_route.py:1375-1413`
(`_stage_script_lines`'s `route` branch), re-read against the current tree
rather than assumed unchanged from the Phase 1 survey — **it has changed
materially since #735**: the Phase 1 survey's own §1 table described a
`route` stage with no antenna repair; #759 (Phase 1 survey §3.3) has since
shipped `repair_antennas`. The stage today issues, in order:

```
set_routing_layers -signal <routing_range>      # e.g. "met1-met5" (sky130hd)
global_route
detailed_route -output_drc <rpt> -output_maze <log> -or_seed <seed>
repair_antennas <diode_cell>
detailed_route -output_drc <rpt> -output_maze <log> -or_seed <seed>   # re-route after diode insertion
check_antennas
estimate_parasitics -global_routing
```

- **`global_route`** is OpenROAD's FastRoute-descended global router (`grt`
  module) — congestion-aware Steiner-tree-based net assignment to a coarse
  routing grid, producing guides `detailed_route` then honours.
- **`detailed_route`** is TritonRoute (Kahng et al., ISPD 2019 — **[LIT]**,
  best-effort citation, already cited by the Phase 1 survey's §2.3/
  references) — a sequential, panel-based detailed router: it processes
  the design in horizontal panels, assigns tracks, and performs
  rip-up-reroute within and across panels to resolve DRC violations and
  unrouted guides. This command already invokes it end to end and reports
  its own `-output_drc`/`-output_maze` artifacts, but does not parse
  per-violation detail from them beyond the aggregate count `_count_
  violations` recovers (`place_and_route.py:1555`).
- **The one routing-adjacent addition since #735**: `repair_antennas` +
  a second `detailed_route` pass (#759) — a **local**, targeted rip-up: a
  diode instance is inserted on each antenna-violating net and that one
  net is re-routed/legalized, not the whole design. This is the closest
  precedent already in this codebase to "targeted local rip-up-reroute
  driven by a specific violation class," a shape §4.2 below builds on.
- **What is not issued**: no timing-driven-specific flag on either
  `global_route` or `detailed_route` — **not independently re-verified
  live in this pass** (unlike issue #783's own `info body
  clock_tree_synthesis` introspection against a real `openroad/orfs`
  container, this survey did not run OpenROAD; whether either command
  exposes a simple timing-driven on/off switch the way `global_placement`
  does is an open question this document flags for §4.1's own proposed
  audit, not an asserted fact); no iterative
  `MAX_REPAIR_ANTENNAS_ITER_DRT`-style repeated repair loop (deliberately
  single-pass, per the code's own comment at `place_and_route.py:1394-1397`);
  no `-verbose`/report-only introspection into per-net or per-panel routing
  detail beyond the aggregate violation count.
- **Reported metrics**: `wirelength_um` is documented
  (`docs/cli/place-and-route.md`'s own response table) as **HPWL** —
  bounding-box half-perimeter wirelength, the same placement-stage-style
  estimate used at every stage, not the actual routed wire length
  TritonRoute produced — worth flagging for §4's measurement plan, since a
  routing-quality change (e.g. §4.3's guide generator) is not fully
  characterised by a metric that never reflects real routed geometry.
  `antenna_violation_count` (post-repair, from `check_antennas`),
  and the generic `setup_violation_count`/`hold_violation_count`/
  `worst_slack_ns` timing fields, now RC-annotated from
  `estimate_parasitics -global_routing`. There is **no `detailed_route`
  DRC-violation-count field in the JSON response at all** — the
  `-output_drc <rpt>` report is written to disk but never parsed into a
  response field (unlike `antenna_violation_count`, which *is* parsed from
  `check_antennas`'s stdout). A caller today has to open the DRC report
  file itself to know whether the merged GDS is DRC-clean from routing's
  own violations — a real, closed-form gap this survey flags in §5 as
  in-scope for a "close the reporting gap" sketch, orthogonal to the
  native-Rust question this issue is actually about.

## 2. Grounding the metal-stack requirement (Epic #700 Phase 2's own dependency)

Epic #700's Phase 2 description names `#666` (sky130 conductor-stack
extraction) as the shared met1–met5 stack requirement. **#666 is closed**
(**[REPO]**, `gh issue view 666`), but its actual scope is narrower than
"defines the metal stack a router would consume" — worth stating precisely
rather than assuming:

**What #666 actually shipped** (issues #619/#620, merged 2026-08-08,
unreleased as of PyPI `0.2.0` per #666's own Curator note): it extended
`klt extract`'s **connectivity** conductor stack
(`src/klayout_tools/decks/sky130.py`'s `EXTRACTION_DECK.metals`/`.vias`)
from stopping at met2 to covering the full li1→met5 stack, plus a matching
`--abstract-cells` cell-level extraction mode. This is a **correctness
fix for post-route netlist extraction** (so a routed design's connectivity
extracts correctly), not new geometry/design-rule data for routing itself.

**What is genuinely available for a native router to consume, already in
this repo, as of this survey (`src/klayout_tools/decks/sky130.py`,
**[REPO]**):**

- **The connectivity stack itself**: `EXTRACTION_DECK.metals` — `li1`
  (`67/20`), `met1` (`68/20`), `met2` (`69/20`), `met3` (`70/20`), `met4`
  (`71/20`), `met5` (`72/20`) — and `.vias` — `mcon` (`67/44`, li1↔met1),
  `via`/`via1` (`68/44`, met1↔met2), `via2` (`69/44`, met2↔met3), `via3`
  (`70/44`, met3↔met4), `via4` (`71/44`, met4↔met5). This is exactly the
  layer/via naming and stack ordering a router's own layer-index scheme
  needs, and it is already independently verified against a real sky130A
  install (the module's own provenance notes).
- **Per-layer DRC width/spacing rules** (`SKY130_DRC_DECK`,
  `src/klayout_tools/decks/sky130.py`, threshold values in DBU/µm),
  covering exactly the rules a detailed router's legality checker needs:

  | Layer | Min width | Min spacing |
  | --- | --- | --- |
  | li1 | 0.17 µm | 0.17 µm |
  | met1 | 0.14 µm | 0.14 µm (0.28 µm wide-metal exception **not modelled**) |
  | met2 | 0.14 µm | 0.14 µm (same wide-metal caveat) |
  | met3 | 0.30 µm | 0.30 µm (0.4 µm wide-metal exception **not modelled**) |
  | met4 | (mirrors met3's rule family) | 0.30 µm |
  | met5 | (mirrors met3/4's rule family) | 1.6 µm |
  | mcon/via/via2/via3/via4 | — | 0.19/0.17/0.2/0.2/0.8 µm respectively |

  Each row's provenance is a specific `sky130.lydrc`/`sky130A_mr.drc` rule
  ID, transcribed in the deck's own comments — **[REPO]**, not re-derived
  here.
- **Per-layer parasitics** (`PARASITICS.metals`, `src/klayout_tools/
  decks/sky130.py:1499-1547`, **[REPO]**): sheet resistance (li1 12.8,
  met1/met2 0.125, met3/met4 0.047, met5 0.029 Ω/sq) and area/perimeter
  capacitance per level, plus `metal_overlaps` — adjacent-level vertical
  plate-coupling coefficients (li1↔met1 0.1142, met1↔met2 0.13386, …,
  met4↔met5 0.06833 fF/µm²). This is real RC data a timing-driven router
  (§3.3/§4.3 below) would need for net-weighting, already sourced from
  `sky130.tech`'s own `resist`/`defaultareacap`/`defaultperimeter`/
  `defaultoverlap` blocks (nominal corner only — no min/max RC corner
  axis, an explicitly documented non-goal of the current parasitics
  deck).
- **The routing-layer range table this command already uses**
  (`_ROUTING_LAYER_RANGE`, `place_and_route.py:318`, **[REPO]**):
  `sky130_fd_sc_hd` → `met1-met5`, `gf180mcu_fd_sc_mcu9t5v0` →
  `Metal2-Metal5` — the exact signal-routing range OpenROAD's own
  `set_routing_layers` is told to honour today, sourced from each
  platform's ORFS `config.mk`.

**What is genuinely *not* available yet, and would be new scope for
whichever Phase 2 sub-issue needs it (not asserted to exist, not
guessed):**

- **Routing-grid track pitch and offset per layer** (the data
  `make_tracks` derives from the tech LEF and OpenROAD keeps internally).
  `klt place-and-route` reads the tech LEF (`_resolve_lef`,
  `place_and_route.py:1113`) but never parses `LAYER ... TYPE ROUTING
  DIRECTION ... PITCH ...`/`OFFSET` sections into Python — a native router
  needs this and it is a straightforward LEF-parsing addition (the same
  shape `native/legalize/src/lef.rs` already reads a subset of), not a new
  research question.
- **Minimum-area rules** (`m*.area`-family, e.g. sky130's `m1.4`/`m2.4`
  minimum-area) — the DRC deck above models width/spacing only; minimum
  area is a distinct rule class this deck does not currently carry for any
  metal level (**[REPO]**, absence confirmed by grep — no `check="area"`
  DRC rule exists for any `met*`/`li1` layer in `sky130.py` today).
- **End-of-line (EOL) spacing and wide-metal spacing exceptions** — every
  wide-metal exception (`m1.3ab`, `m2.3ab`, `m3.3cd`) is explicitly
  documented as *not modelled* in the current DRC deck (§ table above); a
  router relying on this deck for legality would under-constrain wide
  wires exactly where the real fab rule is stricter, a correctness gap
  worth flagging rather than silently inheriting.
- **Per-layer via resistance** — `PARASITICS` models metal sheet
  resistance but names no via resistance term at all (**[REPO]**, grep
  confirms no `via`-keyed entry in `ParasiticsDeck`); a timing-driven
  router weighing via count against wire length has no via-R term to
  weigh against today.
- **gf180mcu's equivalent tables** — this section cites sky130 because it
  is the more completely populated deck; gf180mcu's `EXTRACTION_DECK`/
  `PARASITICS`/DRC-deck coverage above met1 was not independently
  re-verified in this pass and should not be assumed identical in
  completeness before a gf180mcu-targeting sub-issue starts.

**Net assessment**: Phase 2's "shared met1–met5 stack requirement" is
**substantially available already** for the layer topology, DRC width/
spacing, and RC/coupling data a native router needs — but track-pitch/grid
data, minimum-area rules, EOL/wide-metal exceptions, and via resistance
are real, named, bounded gaps, not a blanket "no data exists" situation.
None of these gaps is itself hard; each is a config-table-style addition
in the same family `_CTS_BUFFER_CELLS`/`_ROUTING_LAYER_RANGE`/
`_ANTENNA_DIODE_CELLS` already established, sourced from the tech LEF
(track pitch) or the PDK's own DRC deck (area/EOL rules) — never guessed.

## 3. External SOTA survey

**3.1 Maze/grid-based routing, the classical foundation.** Lee's algorithm
(Lee, C. Y. "An Algorithm for Path Connections and Its Applications." IRE
Transactions on Electronic Computers, 1961 — **[LIT]**) is the original
breadth-first maze router — guaranteed shortest path on a routing grid,
at the cost of full-grid expansion per net. Every modern detailed router,
including TritonRoute, still uses a maze-search variant as its inner-loop
primitive for single-net/single-panel routing, layered under track
assignment and panel decomposition that keep the search space bounded.

**3.2 Gridless / line-search routing.** Hightower's line-probe algorithm
(Hightower, D. W. "A solution to line-routing problems on the continuous
plane." DAC, 1969 — **[LIT]**) avoids the fixed-grid memory/runtime cost
of Lee-style maze search by searching along escape lines from source and
target instead of expanding a full grid. TritonRoute itself is
gridless/track-based rather than a fixed Manhattan grid (its own ISPD 2019
paper's framing, cited above) — this command already invokes a gridless
router; a native-Rust alternative competing on the same axis would need to
match that design choice, not regress to a naive fixed-grid maze router
(which would both use more memory and struggle with sky130's own
sub-track-pitch geometry at met3-met5's wider pitches).

**3.3 Negotiated-congestion rip-up-reroute.** PathFinder (McMurchie, L.,
Ebeling, C. "PathFinder: A Negotiation-Based Performance-Driven Router for
FPGAs." FPGA, 1995 — **[LIT]**) introduced the now-standard technique for
resolving routing congestion without hard rip-up: each resource's cost
grows with the number of nets currently using it (a "history cost" term),
so repeated global rip-up-reroute passes converge toward a legal,
low-congestion solution instead of oscillating. Originally FPGA-specific
(fixed routing resources), the same negotiated-congestion principle
generalises to ASIC global routing (e.g. FastRoute's own congestion-driven
net ordering, which `global_route`/`grt` already implements internally —
this command already benefits from this technique family without any new
code, the same "already using the mature technique, just via the existing
wrap" pattern the Phase 1 survey found repeatedly for placement).

**3.4 Known gap, with this repo's own real evidence.** The #785 spike's
correlation study (`docs/design/flute-congestion-precheck-results.md`,
**[REPO-RUN]**, a real `openroad/orfs:latest` run) is the strongest
available data point specifically about *this command's own* routing
runtime behaviour: a `gcd` design at 68% floorplan utilization **did not
converge** in `detailed_route` — killed after 2460 seconds, versus 71–146
seconds for the same design at 38%/60% utilization. This is not a
hypothetical SOTA gap; it is a concrete, already-observed instance of
TritonRoute's own runtime/convergence cliff at high density, on a design
already in this repo's own corpus. It directly motivates §4.1/§4.2 below
(a native congestion-aware *pre-router* and a targeted local repair tool)
over an attempt to replace the whole detailed router outright.

**3.5 Timing-driven detailed routing.** Net-weighted, timing-aware
detailed routing (biasing track assignment/via minimization toward
critical-path nets, using a similar net-weighting mechanism to timing-
driven placement) is a documented technique family in modern detailed
routers (general practice, **[LIT]**, not attributable to one paper with
confidence in this pass — verify against TritonRoute's own
documentation/source before citing precisely). This command's `route`
stage runs no timing-driven detailed-routing flag today (§1) — the same
"use more of the tool already invoked" opportunity the Phase 1 survey
found repeatedly, and worth checking as a flow-only item (no Rust) before
any native work, per this document's own §4.1.

**3.6 GPU-accelerated global/detailed routing.** Recent academic work
(e.g. GPU-accelerated global routers built on CUDA, broadly following
DREAMPlace's precedent of moving EDA inner loops onto GPU hardware —
**[LIT]**, best-effort category citation only, no single paper name
asserted with confidence without network access to verify) reports large
wall-clock wins on global routing specifically. As with DREAMPlace in the
Phase 1 survey (§2.1 there), a GPU dependency directly conflicts with this
project's "headless always... runnable in CI" mandate (`CLAUDE.md`) and is
**not proposed here** for that reason — noted for completeness of the SOTA
survey, not as a candidate.

**3.7 DRC-driven local repair, as distinct from full rip-up-reroute.**
Production flows increasingly separate "route the whole design" from
"repair the residual N violations a completed route left behind" —
exactly the shape `repair_antennas`+re-route (§1, issue #759) already
established in this command for one specific violation class (antenna).
The general technique — take a completed (possibly imperfect) route,
identify specific DRC violations from a report, and perform bounded local
rip-up-reroute only around each one — is standard signoff practice
(**[LIT]**, general EDA-flow knowledge, not one specific citable paper)
and is the shape §4.2 below proposes as this survey's own best first
native-Rust candidate, for reasons parallel to (but stronger than) why the
Phase 1 survey picked legalization as its own first candidate.

## 4. Prioritized proposal

Five items. Item 1 is a pure flow/parameter change (no Rust) that should
happen regardless of whether native routing proceeds — it costs one PR
and directly informs how much QoR headroom native work is even chasing.
Items 2–4 are a **staged native-Rust plan**, ordered from narrowest/safest
to broadest/riskiest, explicitly informed by the legalizer spike's failure
mode: this survey does **not** propose jumping straight to "replace
TritonRoute," the routing-equivalent of what #784 attempted for
legalization and missed. Item 5 is the reporting-contract gap noted in §1,
included because it is a prerequisite for honestly measuring any of items
2–4 (a native item's DRC-violation delta cannot be scored against the
current baseline if the baseline itself never reports a DRC-violation
count).

### 4.1 Enable/evaluate available timing-driven and repair-iteration routing flags (Priority 1, flow-only)

- **Technique:** audit `detailed_route`/`global_route`'s own documented
  flag surface (via `info body detailed_route`/`global_route` against a
  real `openroad/orfs` container, the same live-introspection method
  issue #783 used for `clock_tree_synthesis`'s flags) for a timing-driven
  or congestion-tuning mode not currently passed, and evaluate
  `repair_antennas`'s optional iteration count
  (`MAX_REPAIR_ANTENNAS_ITER_DRT`-equivalent, currently single-pass per
  `place_and_route.py:1394-1397`'s own comment) as a bounded multi-pass
  option.
- **QoR metric:** `antenna_violation_count` (should stay at/near 0 with an
  iteration bump, if any residual exists after the first pass on the
  corpus); the currently-unreported `detailed_route` DRC-violation count
  (§4.5 makes this measurable) as the primary target metric.
- **Rust vs. flow:** **flow/parameter**, zero Rust — mirrors Phase 1
  survey §3.1/§3.4's own highest-priority items exactly in shape.
- **Measurement plan:** A/B on the existing `tests/corpus/place_and_route/
  gcd.gds.gz` fixture's own source request plus the `modexp` design
  already present in `tests/corpus/legalize/modexp/` (regeneratable
  through the `place` stage per that corpus's own `regenerate.sh`
  pattern) and, once available, `mult8` (present in the `techmap`/
  `statime` corpora already — **[REPO]** — but not yet in a
  `place-and-route`-shaped fixture; generating one is itself a small
  prerequisite, not a blocker, since `tests/corpus/place_and_route/
  regenerate.sh` already generalises to any RTL source). This three-design
  set (`gcd`/`modexp`/`mult8`) matches the issue's own named #520 corpus
  proxy set exactly.
- **Risk:** none — this is entirely inside the existing OpenROAD wrap.

### 4.2 Native-Rust local DRC-violation repair tool, oracle-gated (Priority 2 — this survey's recommended first native slice)

- **Technique:** given an already-completed OpenROAD `detailed_route`
  output (DEF + its own `-output_drc` report) with a small number of
  residual violations (this survey originally cited the sky130 corpus's own
  documented baseline `diff.enclosing.licon.1` "filler-gap" violations as
  the worked example, but #995 showed those were a `klt drc` false positive
  on touching-but-unmerged same-layer shapes — a real example has to come
  from OpenROAD's own `-output_drc` report instead), implement a narrow
  Rust tool that reads the violation report, identifies the offending net/
  segment, and performs a bounded local repair (a jog, a spacing nudge, or
  a rip-up-and-local-reroute of just that segment) — never touching the
  rest of the design's routing.
- **QoR metric:** violation count before/after (target: 0 new violations
  introduced, the targeted violation resolved), scoped local wirelength
  delta (should be small — this is a repair, not a re-route), wall-clock
  for the repair pass itself.
- **Why this, over a broader router, as the *first* native slice:** this
  mirrors the three reasons the Phase 1 survey picked legalization as its
  own first candidate (§3.5 there), but with a materially **smaller**
  blast radius than legalization turned out to need: (a) it is a
  narrower, more bounded problem than legalization was — legalization
  still had to reason about every cell in the design; this tool only
  reasons about the handful of nets/segments a violation report names;
  (b) it has the same direct, unambiguous oracle §4.4's harness already
  gives every item here — before/after violation count and connectivity
  diff, no interpretation required; (c) it exercises the same DEF-round-
  trip FFI pattern `native/legalize/src/def.rs` already established
  (reusable, not reinvented) while working on a problem shape closer to
  what actual detailed routing needs (segment-level geometry manipulation
  under DRC constraints) than legalization's row-assignment problem was.
  **Explicitly informed by #784's failure**: legalization's QoR miss came
  from an under-explored search space on a *whole-design* optimization
  (row assignment for every cell). A local-repair tool sidesteps that
  failure mode by construction — its search space is one violation's
  local neighbourhood, not the whole design, so "did it find a good
  enough answer" is a much easier bar to clear honestly.
- **Wrap-vs-native trade-off, stated honestly:** OpenROAD's own
  `detailed_route`, run twice (§1), already resolves the overwhelming
  majority of violations on this repo's own corpus (the sky130
  gcd/modexp fixtures' only residual violation is one pre-existing,
  already-documented filler-cell gap, per `native/legalize/README.md`'s
  own baseline table) — this item's honest case is **not** "OpenROAD's
  detailed router leaves many violations unrepaired" (no evidence of that
  in this repo's own corpus); it is "a fast, narrowly-scoped native repair
  tool is a low-risk way to validate segment-level DEF-geometry
  manipulation in Rust — the FFI/data-plumbing pattern harder, broader
  native routing work (§4.3) would also need — on a problem small enough
  that a QoR miss is cheap to detect and does not block anything." If the
  corpus genuinely has near-zero violations to repair once the deck gaps
  in §2 are closed, this item may find little to do — a valid, reportable
  "nothing to fix" outcome per this repo's own evidence-tier discipline,
  not a reason to skip measuring.
- **Measurement plan:** run OpenROAD's own `detailed_route` on each corpus
  design (fixed seed); for any design with ≥1 residual violation, repair
  it both ways — a second OpenROAD `detailed_route` pass (the existing
  `repair_antennas`-style re-route, generalised) vs. the new Rust repair
  tool — and diff violation count, local wirelength delta, and wall-clock;
  confirm `klt lvs` still matches (a repair changes geometry only, never
  connectivity — the same structural guarantee `native/legalize/src/
  def.rs` already proves for legalization, reusable directly for a repair
  tool that also never touches `NETS`/`PINS`) and `klt drc` stays clean.
- **Complexity/risk:** medium. Smaller in scope than #784's own attempt,
  but genuinely new territory (DRC-report parsing + segment-level DEF
  geometry edits, neither of which any existing native crate does today);
  the real risk is that the sky130/gf180 corpus simply has too few
  residual violations to give this tool a meaningful test population —
  worth checking corpus violation counts (a cheap, near-zero-risk first
  step) before committing engineering time to the repair logic itself.

### 4.3 Native-Rust congestion-aware pre-router (global-routing-level), building on #785 (Priority 3)

- **Technique:** extend #785's already-shipped RUDY/RSMT congestion
  estimator (`native/congestion/`) from a read-only pre-check into an
  actual **global routing guide generator** — a negotiated-congestion
  (§3.3) net-ordering + coarse-grid assignment pass that produces routing
  guides OpenROAD's own `detailed_route` then consumes directly (replacing
  `global_route`, not `detailed_route` — narrower scope than replacing the
  whole `route` stage).
- **QoR metric:** guide quality proxy (congestion-grid overflow, the same
  metric `native/congestion/src/grid.rs` already computes) versus
  OpenROAD's own `global_route` output on the identical placement input;
  downstream `detailed_route` wall-clock and violation count when fed
  native guides vs. OpenROAD's own guides (the real test — a "better"
  congestion estimate is only useful if it demonstrably helps the
  downstream detailed router, directly addressing §3.4's documented
  non-convergence-at-high-density finding).
- **Rust vs. flow:** **native-Rust hot-loop candidate**, building directly
  on existing, already-validated code (`native/congestion/`) rather than
  starting from zero — this is the item in this document closest in shape
  to Phase 1's own successful §3.6 (FLUTE congestion pre-check), extended
  one step further (estimate → guide) instead of a new sub-problem.
- **Wrap-vs-native trade-off, stated honestly:** OpenROAD's own
  `global_route`/FastRoute-descended `grt` module is mature and already
  congestion-aware (§3.3) — this item's case is specifically the §3.4
  evidence: a documented convergence cliff at high utilization that a
  cheaper, purpose-built native pre-pass (already proven fast — #785's own
  0.02–0.07s/trial measurement) might help avoid by feeding
  `detailed_route` better-conditioned guides at exactly the density range
  where OpenROAD's default flow already struggles on this repo's own
  corpus. This is a real, evidence-backed hypothesis, not a foregone
  conclusion — the measurement plan is designed to falsify it cheaply.
- **Measurement plan:** run the full corpus (gcd/modexp/mult8, plus the
  #785 study's own util38/util60/util68 gcd density sweep specifically,
  since that is where the only known convergence problem lives) with (a)
  OpenROAD's own `global_route` → `detailed_route` (today's baseline) and
  (b) native guides → `detailed_route`; report `detailed_route` wall-clock
  and convergence (did it finish, and in how long) as the primary metric,
  violation count and `wirelength_um` as secondary; `klt lvs`/`klt drc`
  gates as always.
- **Complexity/risk:** high. Requires OpenROAD to actually accept
  externally-generated routing guides (needs live verification against a
  real `openroad/orfs` container — not yet confirmed possible in this
  survey pass, a **prerequisite fact-check**, not an assumed-solved
  integration point) and a real negotiated-congestion net-ordering
  algorithm beyond #785's read-only estimator. This is explicitly framed
  as a **spike**, not a commitment, mirroring #784's own go/no-go framing.

### 4.4 Full native-Rust detailed router — explicitly deferred, not proposed now (Priority 4, informational)

- **Technique:** a from-scratch Rust implementation of gridless/track-
  based detailed routing with rip-up-reroute (the shape TritonRoute
  itself is, §3.2/§1) — the "replace the whole `detailed_route` call"
  outcome Epic #700's own Phase 2 framing gestures at in prose ("maze
  router, DRC-driven ripup-reroute").
- **Why not proposed as a near-term item**: this is the single hardest,
  least-bounded sub-problem in the entire P&R pipeline — harder than
  legalization (#784, already a QoR miss on a much simpler problem),
  harder than global routing (§4.3, itself deferred to spike status).
  TritonRoute represents years of mature, actively-maintained open-source
  engineering; a from-scratch Rust detailed router competing on QoR
  against it is a materially larger undertaking than anything Phase 1
  attempted, with the least evidence in this document that it is even the
  right lever (§3.4's own convergence-cliff finding is about *global*
  routing feeding *detailed* routing badly-conditioned guides at high
  density — nothing in this survey's evidence base points at
  `detailed_route`'s own core algorithm as the bottleneck, as distinct
  from what it's asked to route).
- **Recommendation**: do not fund this directly. If items 4.2/4.3 both
  succeed (repair tool ships useful, congestion pre-router demonstrably
  improves convergence), *then* revisit whether a full native detailed
  router is still warranted — by that point there would be two additional
  real data points (a working segment-level DEF-editing FFI path, and
  real evidence about where in the route pipeline this repo's own corpus
  actually struggles) informing that decision instead of zero.

### 4.5 Add a `detailed_route` DRC-violation-count response field (Priority 2, prerequisite for measuring 4.2/4.3 honestly)

- **Technique:** parse `-output_drc <rpt>`'s own violation count (the
  report `detailed_route` already writes to disk every run, per §1) into
  a new response field, mirroring exactly how `antenna_violation_count`
  is already parsed from `check_antennas`'s stdout
  (`_count_antenna_violations`, `place_and_route.py:1571`) — same
  pattern, one new field, e.g. `route_drc_violation_count`.
- **QoR metric:** this item *is* the metric — every other item in this
  document (4.1–4.3) needs "did the `detailed_route` violation count
  change" as a measured JSON field, not a value someone has to `grep` out
  of a report file by hand for every corpus run.
- **Rust vs. flow:** **flow/parameter**, zero Rust, one additive response
  field (per `docs/design/digital-flow-contracts-spike.md`'s own additive-
  field posture, the same pattern §3.3 of the Phase 1 survey already used
  for `antenna_violation_count`).
- **Measurement plan:** unit test asserting the field is populated/`null`
  correctly at each `stage_reached`, cross-checked against a real
  `detailed_route -output_drc` report's own violation count on the
  existing `gcd` corpus fixture.
- **Risk:** none — this closes a reporting gap identified in §1, does not
  change any existing field's meaning.

## 5. Measurement harness — common to every item above

- **Corpus.** The issue's own named #520 corpus proxy — `gcd` (already
  present, `tests/corpus/place_and_route/gcd.gds.gz` +
  `tests/corpus/legalize/gcd/`), `modexp` (already present,
  `tests/corpus/legalize/modexp/`), and `mult8` (present in the
  `techmap`/`statime` corpora as RTL/netlist fixtures — **[REPO]** — but
  not yet regenerated through `place_and_route`'s own pipeline; doing so
  is a small, mechanical prerequisite using the existing
  `tests/corpus/place_and_route/regenerate.sh` recipe, not new research).
  As Phase 1's survey already noted, no corpus harness exists yet that
  loops over the full #520 Tiny Tapeout set (4,572 project slots) — this
  three-design proxy is the same practical substitute Phase 1 used, named
  explicitly by this issue's own body.
- **OpenROAD as oracle.** Every native-Rust item (4.2/4.3/4.4) is scored
  against OpenROAD's own output on the identical input, per Epic #700's
  own "reality-grounding discipline" — never against this repo's own
  prior native output.
- **`klt lvs` gate.** Every item above changes routing geometry, never
  connectivity — the standing regression gate for "did this change break
  structural equivalence." A repair/reroute item touching `NETS`/`PINS`
  parsing (unlike legalization, which structurally never could) needs
  this gate to actually run, not merely be assumed to pass by
  construction the way `native/legalize/src/def.rs`'s design let it.
- **`klt drc` gate.** The primary correctness gate for every routing-
  adjacent item — a DRC regression on the merged GDS is a defect report
  for any of 4.1–4.4, never an accepted trade-off.
- **A/B protocol.** Same request document, same `seed` (routing is
  genuinely stochastic — `-or_seed`, `docs/cli/place-and-route.md`'s own
  `seed` field), one change toggled at a time, JSON response fields
  diffed directly (`wirelength_um`, `antenna_violation_count`, the new
  `route_drc_violation_count` from §4.5, `worst_slack_ns`, wall-clock),
  never eyeballed from logs.

## 6. Follow-on implementation-issue sketch

### Sketch — close the reporting gap + establish the corpus prerequisite (§4.5 + the `mult8` fixture)

**Title:** `place-and-route: add detailed_route DRC-violation-count field, regenerate mult8 corpus fixture`

**Why this is the right *first* Phase 2 issue, not 4.2/4.3 directly:**
every later item in this document (4.1, 4.2, 4.3) needs a measurable
violation-count field and the full three-design corpus to be scored
honestly — shipping this first is the same "cheapest, most certain-value
item first" ordering the Phase 1 survey used for its own §3.1–3.3, and
unlike 4.2/4.3 it carries **zero** open questions (no live-verification
prerequisite the way 4.3 has, no "will there be enough violations to
repair" open question the way 4.2 has).

**Scope:**
1. Parse `-output_drc <rpt>`'s violation count into a new
   `route_drc_violation_count` response field (§4.5), mirroring
   `_count_antenna_violations`'s exact pattern.
2. Regenerate a `mult8` place-and-route corpus fixture via
   `tests/corpus/place_and_route/regenerate.sh`'s existing recipe (real
   `openroad/orfs:latest` + volare sky130A), completing the gcd/modexp/
   mult8 trio this issue's own body names.
3. Update `docs/cli/place-and-route.md`'s response table for the new
   field; no other contract change.

**Acceptance criteria:**
- Existing `tests/test_place_and_route.py` stubbed-OpenROAD tests updated
  to assert the new field is populated/`null` correctly by `stage_reached`.
- The new field's value cross-checked against a real `detailed_route
  -output_drc` report's own violation count on the `gcd` fixture (real
  OpenROAD, not stubbed).
- `mult8.gds.gz`/checkpoint fixtures committed under
  `tests/corpus/place_and_route/mult8/`, generated via a documented,
  reproducible `regenerate.sh` invocation (never a one-off hand edit).
- `klt lvs`/`klt drc` clean on the new `mult8` fixture.

**Not in scope:** 4.1's flag audit, 4.2's repair tool, and 4.3's guide
generator are separate, larger follow-on issues that consume this
sketch's own output (the violation-count field and the completed corpus)
as their measurement substrate — deliberately not bundled here.

## References

- Lee, C. Y. "An Algorithm for Path Connections and Its Applications."
  IRE Transactions on Electronic Computers, 1961.
- Hightower, D. W. "A solution to line-routing problems on the continuous
  plane." DAC, 1969.
- McMurchie, L., Ebeling, C. "PathFinder: A Negotiation-Based
  Performance-Driven Router for FPGAs." FPGA, 1995.
- Kahng, A. B. et al. "TritonRoute: The Open-Source Detailed Router"
  (best-effort citation, OpenROAD's own project bibliography is the
  primary source to verify exact title/venue/authors against). ISPD,
  2019.
- Spindler, P., Johannes, F. M. "Fast and Accurate Routing Demand
  Estimation for Efficient Routability-driven Placement" (RUDY). DATE,
  2007 — cited here as it was in `docs/design/
  flute-congestion-precheck-results.md`, the accepted document already
  using this technique in this repo.
- `docs/design/place-and-route-improvements-survey.md` (#735) — Phase 1's
  own survey; this document's direct structural and evidentiary ancestor.
- `native/legalize/README.md` (#784) — the Phase 1 native-placement
  spike whose No-go verdict and root-cause analysis most directly inform
  this document's staging recommendation (§4.2's rationale).
- `docs/design/flute-congestion-precheck-results.md` (#785) — the Phase 1
  native-congestion spike this document's §4.3 builds directly on, and
  the source of this document's only real (non-literature) evidence of a
  detailed-routing convergence gap (§3.4).
- `docs/design/openroad-invocation-survey.md` (#397) — this repo's own
  real, live-verified OpenROAD invocation survey.
- Epic #700 (this document's own parent) and Epic #520 (the Tiny Tapeout
  corpus this document's measurement plan targets, via the gcd/modexp/
  mult8 proxy named in issue #934's own body).
