# Survey & proposal: native-Rust detailed routing for `klt place-and-route`

**Status:** research / proposal, no implementation. Filed for issue #934, a
research-and-propose task under [Epic #700](https://github.com/2AMLogic/klayout-tools/issues/700)
("place-and-route for `klt` — synthesis→placement→routing→GDS in Rust,
OpenROAD-validated"), Phase 2. This document plays the same role for
Phase 2 that
[`docs/design/place-and-route-improvements-survey.md`](place-and-route-improvements-survey.md)
(#735) played for Phase 1: its prioritized items became #745/#746/#759 (the
three OpenROAD-default-flow gaps, all now shipped) and #783/#784/#785 (the
1a/1b/1c native-Rust/flow spikes). This document does **not** authorise
implementation of anything below — it is the "fresh eyes" input Phase 2's
own decomposition still needs, per Champion's repeated finding on Epic #700
that Phase 2 (unlike Phase 1) arrived with no grounding survey to decompose
from.

**What this document does not settle.** Epic #700 itself proposes
eventually replacing today's OpenROAD-orchestration command with native-Rust
placement/routing. This survey does not presume that outcome for routing
any more than #735 did for placement — and Phase 1's own most direct data
point on the question (§0 below) argues for real caution, not confidence,
about how far "native beats OpenROAD" generalizes.

**Required prior art, read first, not re-derived here:**

- [`docs/design/place-and-route-improvements-survey.md`](place-and-route-improvements-survey.md)
  (#735) — Phase 1's survey; §2.1–§2.8 already cover global placement,
  legalization, CTS, and (briefly, §2.3/§2.8) detailed routing and RSMT
  estimation at the level Phase 1 needed. This document goes one level
  deeper on the routing-specific literature Phase 1 only touched in
  passing.
- [`docs/design/openroad-invocation-survey.md`](openroad-invocation-survey.md)
  (#397) — the accepted survey of OpenROAD's own invocation surface,
  including the routing-stage scripts (`5_*`, "not reached" in that
  survey's own live run) this document's §1 now characterizes fully, since
  the `route` stage has since shipped.
- [`native/legalize/README.md`](../../native/legalize/README.md) (issue
  #784) — **the single most important input to this document's risk
  posture.** A real, oracle-gated native-Rust legalizer spike returned a
  **No-go** verdict: legality matched OpenROAD's `opendp` exactly, but HPWL
  was 21.8–26.6% worse and total displacement 213.8–403.4% worse. See §0.
- [`native/statime/README.md`](../../native/statime/README.md) (issue
  #809, Epic #704 Phase 1c) — the repo's other native-engine spike, and
  its opposite: a **Go** verdict for a native-Rust static-timing engine,
  1.34% (accuracy) / 1.5–2.1× (cold-regime latency) against OpenSTA. Cited
  in §4.3 as a building block a routing-side proposal can reuse in-process.
- [`docs/design/flute-congestion-precheck-results.md`](flute-congestion-precheck-results.md)
  (issue #785, Epic #700 Phase 1 §3.6) — the repo's third native-engine
  spike (Go, ship-the-estimator-not-the-gate verdict), and the source of
  this document's single strongest piece of real evidence for a routing
  QoR/runtime gap: a real `sky130A` route-stage run that failed to converge
  in 41 minutes on a 384-instance design (§2.5).
- [`docs/cli/place-and-route.md`](../cli/place-and-route.md) — the
  command's own contract documentation, the ground truth for §1 below.
- Issue #666 (closed) — the sky130 conductor-stack extraction fix this
  document's §3 grounds against, per Epic #700's own Phase 2 description
  naming it as the shared met1–met5 stack requirement.

## Evidence-tier discipline

Following `docs/design-evidence-tiers.md`'s ladder and #735's own precedent:
this task did not run OpenROAD or any corpus benchmark — it is pure
analysis, per its own Definition of Done (no implementation). No `openroad`
binary is available in this environment (`which openroad` → not found), so
every claim below is one of:

- **[REPO]** — read directly from this repo's own source/docs, cited by
  file and line.
- **[REPO-RUN]** — a **real** result already captured by another accepted
  document in this repo (the #666/#735/#759/#784/#785/#809 documents cited
  above), not re-derived from memory.
- **[LIT]** — a technique or finding from the published EDA-CAD literature
  or OpenROAD's own documented command surface, cited by name/venue/year to
  the best of this survey's ability without live network/library access.
  Treat exact author lists as best-effort — verify against the primary
  source before citing in a paper trail that requires precision. This is
  the same hedge #735's own §"Evidence-tier discipline" used, repeated here
  because it still applies with no change in tooling access.
- **[PROPOSAL]** — this document's own reasoning/recommendation, not a
  claim about the world.

No item below rests solely on an uncited assertion.

## 0. The lesson Phase 1 already taught this document, stated up front

Epic #700's Champion-recorded phase-decision comment (2026-08-12, on issue
#700 itself — **[REPO]**) already flags the central tension this survey has
to write around: Phase 1's placement spike (#784) hit **legality** (zero
overlap, DRC-clean) but missed **QoR** by a wide margin, on the smallest,
best-bounded sub-problem in the whole P&R pipeline — row legalization has a
crisp, single-objective, textbook (Abacus) target, and the native
implementation still lost by 200–400% on displacement. Detailed routing is
categorically harder: multi-net simultaneous resource contention across
five metal layers, a legality condition that is itself expensive to check
(DRC, not a simple overlap test), and — per §2 below — OpenROAD's own
router (TritonRoute) is the product of a specific research group's
multi-year, ISPD-contest-tested effort, not a general algorithm family a
first native implementation can expect to match on QoR quickly.

This document's proposal is accordingly **not** "spike a full native
detailed router and see." It is staged the way Epic #700's own Phase 1
retrospectively should have been read as staged: cheap, low-risk,
non-algorithmic groundwork first (§4.1–§4.2); a narrow, bounded-scope
native routing capability with a crisp oracle next (§4.3); and the
open-ended "replace TritonRoute" bet deliberately deferred to its own
future go/no-go spike (§4.4), not committed here. This is the same
reasoning #735's own §3.5 used to justify legalization as Phase 1's first
native candidate ("smallest, best-bounded... before spending [integration
risk] on a harder algorithm") — except this document, unlike #735, gets to
cite that the first attempt at exactly that reasoning did not clear the
QoR bar, which is exactly why Phase 2's own first native item (§4.3 below)
is scoped narrower than routing itself.

## 1. Baseline: what `klt place-and-route`'s `route` stage does today

**[REPO]**, from `src/klayout_tools/place_and_route.py` (`_stage_script_lines`,
the `route` branch, lines ~1373–1401) and `docs/cli/place-and-route.md`.
Unlike when #735 was written (the `route` stage's own script was not yet
characterized there beyond the §1 table's placeholder row), the stage now
ships the full sequence Phase 1's own items 3.1–3.3 (issues #745/#746/#759)
added:

```
set_routing_layers -signal <met1-met5 | Metal2-Metal5>   # per-cell_library, _ROUTING_LAYER_RANGE
global_route
detailed_route -output_drc <report> -output_maze <log> -or_seed <seed>
repair_antennas <diode_cell>                              # per-cell_library, _ANTENNA_DIODE_CELLS
detailed_route -output_drc <report> -output_maze <log> -or_seed <seed>   # re-run, legalizes new diodes
check_antennas                                            # -> antenna_violation_count
estimate_parasitics -global_routing
```

Every routing algorithm this sequence exercises — `global_route`'s own
congestion-driven global router (FastRoute-descended, OpenROAD's `grt`
module), and `detailed_route`'s TritonRoute (§2.2) — is OpenROAD's own,
never this repo's. This command's own contribution is entirely
orchestration: which flags to pass (`set_routing_layers`, `-or_seed` for
reproducibility), when to re-run (`repair_antennas`'s reroute pass), and
turning the DRC/maze report *paths* it requests into two things it reads
back today: (a) `antenna_violation_count`, from `check_antennas`'s own
metric, and (b) the generic setup/hold violation-*count* stdout-scrape
fallback shared with every other stage (`_count_violations`,
`place_and_route.py:1326`, per #397's own finding). **Two real, machine-
readable artifacts this command already asks TritonRoute to produce and
then never reads at all**: the `-output_drc <report>` file itself (a
structured, per-violation report — this command only ever passes its
*path*, never opens it) and the `-output_maze <log>` file (a maze-routing
debug trace). Both are named, unused inputs sitting on disk after every
real `route` stage — the first concrete "where a native pass would plug
in" candidate this document identifies, expanded in §4.2.

**Where a native-Rust routing pass would plug in.** Four distinct
insertion points exist in this sequence, in increasing order of
algorithmic ambition:

1. **Before `global_route`** — a native pre-router pass that consumes the
   post-placement DEF (the same artifact `native/congestion/` already
   reads, per issue #785) and produces routing hints (a congestion map,
   already shipped; a net-priority ordering, §4.3) without touching
   OpenROAD's own routing calls at all.
2. **Between `global_route` and `detailed_route`** — OpenROAD's own
   `global_route` can, per its documented Tcl surface, write a route-guide
   file (`write_guides`) that `detailed_route` can consume via its own
   `-guide` argument [LIT, standard TritonRoute usage — **not independently
   verified live in this environment**, flagged here rather than asserted
   as [REPO], since no `openroad` binary is available to run `help
   detailed_route`/`help global_route` in this session; a follow-on issue
   implementing anything at this insertion point must re-verify the exact
   flag names live, exactly as `_ANTENNA_DIODE_CELLS`'s own docstring
   documents doing for its own claims]. This is the natural seam for a
   **native global router** to replace OpenROAD's `grt` while still handing
   TritonRoute's own, far more mature detailed router the guide file it
   already knows how to consume — §4.4.
3. **After `detailed_route`, on its own `-output_drc` report** — a
   native, narrow-scope repair pass over specific violation classes,
   exactly the shape `repair_antennas` already is for antenna violations
   specifically (§1's own sequence above) but generalized to width/spacing
   classes using the DRC rule data #666 already established (§3) — §4.2.
4. **Replacing `detailed_route` entirely** — the maximal reading of "native
   Rust detailed routing," and, per §0, the one this document declines to
   propose as a near-term dispatchable issue. Named here only so the staged
   plan in §4 is explicit about what it is *not* committing to yet.

## 2. External SOTA survey

**2.1 Maze routing (Lee's algorithm).** The foundational single-net
shortest-path routing algorithm — a breadth-first-search-style wavefront
expansion over a routing grid (Lee, C. Y., "An Algorithm for Path
Connections and Its Applications," IRE Transactions on Electronic
Computers, 1961 — **[LIT]**). Every gridded detailed router since,
including TritonRoute, still uses a maze-routing (or A*-search) core for
single-net connection once track/via assignment has narrowed the search
space — confirmed **[REPO]** for this repo's own invocation: the
`detailed_route` Tcl call this command already issues passes
`-output_maze <log>` (`place_and_route.py:1381`), OpenROAD's own name for
TritonRoute's maze-routing debug trace — direct evidence the router this
project already wraps has a real maze-routing stage inside it, not
external inference.

**2.2 TritonRoute's own architecture: track-based, not fully gridless.**
TritonRoute (Kahng, A. B. et al., "TritonRoute: The Open Source Detailed
Router," ISPD 2019 — **[LIT]**, best-effort citation, as #735's §2.3 also
flagged) is track-assignment-based with sub-track flexibility at pin
access points, not a literal continuous-coordinate gridless router — it
follows the standard modern-detailed-router pipeline of global routing →
**track assignment** (Batterywala, S., Shenoy, N., Nicholls, W., Zhou, H.,
"Track Assignment: A Desirable Intermediate Step Between Global Routing and
Detailed Routing," ICCAD 2002 — **[LIT]**, the paper that established track
assignment as its own intermediate stage) → detailed routing with local
rip-up-reroute. This matters for scoping a native effort: "native gridless
routing" as a literal design goal misreads what the SOTA (and the tool
already wrapped) actually does — the real opportunity is in the stages
around the maze-routing core (track assignment, prioritization,
congestion-aware guide generation), not in eliminating the grid.

**2.3 Negotiated-congestion rip-up-reroute (PathFinder).** The
foundational technique for resolving multi-net resource contention
iteratively: route every net greedily first (ignoring overlap), then
repeatedly rip up and reroute nets through a cost function that grows with
a resource's *history* of being overused, converging toward a
legal, low-overlap solution without ever needing a single globally-optimal
pass (McMurchie, L., Ebeling, C., "PathFinder: A Negotiation-Based
Performance-Driven Router for FPGAs," FPGA 1995 — **[LIT]**). Originally an
FPGA technique, but the negotiated-congestion cost-function idea is the
conceptual ancestor of essentially every modern ASIC global/detailed
router's own rip-up-reroute loop, TritonRoute's included — general
technique family, not project-specific.

**2.4 Timing-driven detailed routing.** Net ordering and layer/via
assignment biased by per-net timing criticality (rather than uniform
priority) so the router spends its limited "shortest path on the cheapest
layer" budget on the paths that actually gate `worst_slack_ns` — general
EDA-flow practice (**[LIT]**, best-effort characterization; OpenROAD's own
documentation and the routing-literature survey space describe this family
of techniques without a single canonical paper this survey can cite with
confidence absent live source access). The concrete, checkable building
block this repo already has for it: `native/statime` (issue #809, **Go**
verdict, **[REPO-RUN]**) computes per-net/per-arc slack from a gate-level
netlist + liberty **in-process**, 1.5–2.1× faster than OpenSTA in the
"cold" no-new-engineering regime the rest of `place_and_route.py` already
operates in (one subprocess per stage, no persistent session) — a real,
already-validated way to get net-criticality data into a future native
routing pass without paying for another OpenSTA/OpenROAD round trip. Cited
directly in §4.3 as a reuse candidate, not a fresh spike.

**2.5 Known QoR/runtime gap, with this repo's own real evidence.** The
`flute-congestion-precheck-results.md` correlation study (#785,
**[REPO-RUN]**, a real `openroad/orfs:latest` container run against a real
volare `sky130A` install) captured exactly the kind of routing-stage
failure this survey's "known QoR/runtime gaps" ask is looking for: the
`util68` trial (a 384-instance `gcd` variant floorplanned to 68% requested
/ higher legalized utilization) had its `detailed_route` stage **killed
after 2460s (41 minutes) without converging** — roughly 20–35× either of
the same design's own successful trials' route wall-clock (71s / 146s).
This is not a hypothetical SOTA gap or a claim about TritonRoute's general
reputation; it is a concrete, already-observed non-convergence on a small
(384-cell) design in this project's own corpus, at a utilization level a
real synthesis/floorplanning DSE sweep would plausibly hit without a
pre-check to reject it first. It is also the direct evidentiary basis for
§4.1's congestion-model grounding item and a reason to weight §4.3 (a
DRC-driven repair pass, not a from-scratch router) over §4.4 (a from-
scratch global router) as the safer near-term bet: the observed failure
mode is *non-convergence under high congestion*, which a better pre-route
congestion signal (§4.1) plus a narrower post-route repair capability
(§4.2) both address without needing to out-route TritonRoute's own maze
core.

**2.6 GPU/ML-accelerated routing.** The placement literature's GPU trend
(#735 §2.1, DREAMPlace) has a routing-side counterpart in recent academic
work on GPU-accelerated detailed/global routing, but this survey does not
cite a specific tool by name here — without live network/library access in
this task, this survey cannot verify authorship/venue with the confidence
its own evidence-tier discipline requires, and the category is excluded
from this document's proposal regardless of citation quality: a GPU
dependency directly conflicts with `CLAUDE.md`'s "headless always...
runnable in CI" mandate, exactly the reasoning #735 §2.1 already applied to
GPU placement.

## 3. Grounding the metal-stack requirement (#666)

Epic #700's Phase 2 description names #666 (closed) as the shared
met1–met5 stack requirement. **[REPO]**, verified against
`src/klayout_tools/decks/sky130.py`: #666 closed the DRC deck's coverage
gap so met1 through met5 (plus mcon/via1/via2/via3/via4) all carry real
width/spacing/enclosure rules, sourced from `sky130.lydrc`/the sky130 DRM
(never guessed, per that module's own provenance-citation discipline):

| Layer | GDS layer | Min width | Min spacing |
| --- | --- | --- | --- |
| met1 | 68/20 | 0.14 µm | 0.14 µm |
| met2 | 69/20 | 0.14 µm | 0.14 µm |
| met3 | 70/20 | 0.30 µm | 0.30 µm |
| met4 | 71/20 | 0.30 µm | 0.30 µm |
| met5 | 72/20 | 1.60 µm | 1.60 µm |
| via2 (met2↔met3) | 69/44 | — | 0.20 µm |
| via3 (met3↔met4) | 70/44 | — | 0.20 µm |
| via4 (met4↔met5) | 71/44 | — | 0.80 µm |

(`met1.width.1`/`met1.space.1` through `met5.width.1`/`met5.space.1`,
`via2.space.1`/`via3.space.1`/`via4.space.1`, `src/klayout_tools/
decks/sky130.py`.) The same connectivity extension landed on the
extraction/LVS side (issue #619, cited in that module's own comments) — a
routed sky130 design now extracts with correct met1–met5 connectivity, the
correctness bug #666 itself was filed to fix. gf180mcu's deck
(`src/klayout_tools/decks/gf180mcu.py`) has the equivalent width/spacing/
enclosure coverage for metal1/metal2/metal3/metaltop, with one identified
gap this survey found in passing (not #666's own scope, and not fixed
here): no dedicated `metal4.width.1`/`metal4.space.1` rule exists — only
via-enclosure rules (`metal4.enclosing.via3.1`/`metal4.enclosing.via4.1`)
reference metal4 at all. Worth a separate, narrow issue if a native routing
effort ever needs a complete gf180mcu stack; not a blocker for a
sky130-first Phase 2, consistent with `CLAUDE.md`'s "open PDKs only (sky130
first)."

**What #666 delivers a native router: connectivity-correctness rules
(width/spacing/enclosure), not a routing-grid abstraction.** This is the
real gap this survey's own item (3) was asked to characterize. A DRC
width/spacing rule tells a router "don't violate this," which is
necessary but not sufficient — a router's own cost function and legal-move
generation need a **routing-grid descriptor**: per-layer track pitch,
preferred routing direction (conventionally alternating horizontal/
vertical layer-to-layer), track offset, and via-array/cut-spacing rules for
multi-cut vias, none of which #666 (a DRC-rule-coverage fix) produced or
was scoped to produce. The one place this repo has *any* per-layer routing-
grid number today is `native/congestion/src/contract.rs`'s
`DEFAULT_TRACK_PITCH_UM = 0.46` (**[REPO]**, sky130hd's own met2 vertical
track pitch, read from a real placement DEF's `STEP 460` — see that
module's own docstring) and `DEFAULT_LAYER_COUNT = 3.0` — both explicitly
documented as a single caller-overridable default, not a real per-layer
table. **The authoritative source for this data already exists and is
already resolved by this command, but nothing in this repo parses it
today**: the tech LEF (`<cell_library>__<corner>.tlef`,
`klayout_tools.pdk.lef_files()`, per `docs/cli/place-and-route.md`'s "PDK /
LEF / liberty resolution" section) is a standard LEF file whose own
`LAYER ... TYPE ROUTING` stanzas declare exactly this per-layer
pitch/direction/offset/width data — OpenROAD reads it (that is how
`set_routing_layers`/`global_route`/`detailed_route` already know sky130's
real geometry today), but this repo's own Python/Rust code has no LEF
tech-layer parser of its own. This is §4.1's proposal: the single
highest-leverage, lowest-risk first step, because every later native
routing item (congestion modeling with real numbers, a native global
router's own cost function, a DRC-driven repair pass that needs to know a
layer's legal via positions) needs this data and currently has none of it
beyond one hardcoded constant.

## 4. Prioritized proposal

Four items plus one explicitly-deferred fifth, staged by §0's own lesson:
cheap, near-zero-risk data plumbing first; a narrow, already-precedented
native capability next; the open-ended "native router" bet named last and
explicitly not committed.

### 4.1 Priority 1 — Parse the PDK tech-LEF routing-layer stack into a structured descriptor

- **Technique:** a tech-LEF `LAYER ... TYPE ROUTING` parser (per-layer
  name, `DIRECTION` (horizontal/vertical), `PITCH`, `OFFSET`, `WIDTH`,
  `SPACING` when a simple scalar rule applies) plus `VIA`/`VIARULE`
  min-cut-array data for the via layers between them, resolved from the
  same tech LEF `klt place-and-route` already loads
  (`klayout_tools.pdk.lef_files()`), emitted as one JSON-serializable
  per-`cell_library` stack descriptor.
- **QoR metric:** none directly — this is enabling infrastructure, not an
  algorithmic change. Its own correctness metric is a **fidelity** check:
  parsed values must match the DEF-observed reality already spot-verified
  once (`native/congestion`'s `DEFAULT_TRACK_PITCH_UM`, sky130hd met2 =
  0.46 µm) and OpenROAD's own resolved geometry (a real
  `place_and_route.py` run's `set_routing_layers`/floorplan output
  already implicitly proves the tech LEF resolves correctly — this item
  adds a second, independent reader of the same file, so the check is "do
  the two readings agree").
- **Rust vs. flow:** **could be pure Python** (LEF is a plain text format;
  `klayout.db`'s own LEF reader, already a dependency for the DEF→GDS merge
  path, is a plausible zero-new-dependency implementation) or a small Rust
  crate if a later native routing item wants the descriptor in-process
  without a Python round trip. This survey does not prejudge that choice —
  it is a genuinely small parser either way, and the "native Rust" framing
  of this document's own charter applies to the *routing algorithm* work
  downstream of it, not necessarily to this data-plumbing step.
- **Wrap-vs-native trade-off:** none to weigh at this layer — parsing a
  file OpenROAD already reads correctly is not a QoR question.
- **Measurement plan:** unit tests against the checked-in
  `tests/corpus/legalize/sky130_fd_sc_hd_tech.lef.gz` fixture (already in
  the repo, per `tests/corpus/legalize/` listing — no new PDK download
  needed to test the parser itself); a live cross-check against a real
  volare `sky130A` install's own tech LEF, confirming met1–met5 pitch/
  direction values match `native/congestion`'s existing 0.46 µm met2
  reference and are internally consistent (alternating direction,
  monotonically non-decreasing pitch layer-to-layer, the standard shape a
  real stack follows).
- **Risk:** low. Bounded parser scope, no algorithmic content, a crisp
  fidelity check (not an open-ended quality judgment) as its own gate.

### 4.2 Priority 2 — Rebase `native/congestion`'s defaults on real per-layer data; expand its corpus

- **Technique:** two independent, additive changes to the already-shipped
  (#785, Go verdict) congestion pre-check: (a) replace
  `DEFAULT_TRACK_PITCH_UM`/`DEFAULT_LAYER_COUNT`'s single hardcoded
  constants with §4.1's real per-layer stack descriptor (a proper capacity
  model summing each usable layer's own track density, not one averaged
  number); (b) rerun the correlation study
  (`scripts/research/flute_congestion_correlation.py`) against a larger
  corpus, closing the exact gap `flute-congestion-precheck-results.md`'s
  own "What would change the verdict" section names: more designs than the
  single `gcd` this study used, and at least one trial with a **nonzero,
  finite** DRC-violation count rather than only "clean vs. non-convergent."
  `tests/corpus/statime/mult8.v` (already in the repo, added for issue
  #809 — a purely combinational 8×8 multiplier, structurally distinct from
  `gcd`'s sequential shape) and `examples/functional-verification/
  modexp.v` (already used by both the legalize and statime corpora) are
  both immediately available without adding a new design to the repo,
  matching Epic #704/Phase 1's own established `gcd`/`mult8`/`modexp`
  corpus-proxy convention this issue's own body cites.
- **QoR metric:** correlation strength (Pearson `r`, already reported by
  the existing `--reject-threshold` tooling) between `congestion_score` and
  post-route DRC-violation count/non-convergence, across a real multi-
  design, multi-utilization-trial corpus — the metric #785's own
  acceptance criteria already defined, not a new one.
- **Rust vs. flow:** (a) is a small, additive change to
  `native/congestion/src/contract.rs`/`grid.rs` (a real per-layer capacity
  sum instead of one scalar `layer_count * tile_area / track_pitch`
  formula) — genuinely native-Rust hot-loop content, since this is the
  same RSMT/grid computation #785 already justified running natively. (b)
  is a corpus/measurement-harness expansion, no new code path.
- **Wrap-vs-native trade-off:** already settled by #785's own Go verdict
  for the estimator itself; this item does not reopen that question, only
  grounds its inputs in real data and widens its evidence base.
- **Measurement plan:** rerun `flute_congestion_correlation.py` against a
  ≥3-design, ≥2-utilization-point-per-design corpus (mirroring #785's own
  3-trial `gcd` study, but across designs rather than only within one);
  report correlation strength and false-reject/false-keep counts exactly as
  #785's own JSON output already does; only propose wiring the pre-check
  into `digital_fleet.py`'s actual candidate-ranking gate (still not done,
  per #785's own explicit deferral) if this expanded study clears a real
  bar — this item's own scope is "produce that evidence," not "flip the
  gate on."
- **Risk:** low-moderate. Corpus generation needs real `klt synthesize` +
  `klt place-and-route` + Docker/`openroad-orfs` runs at several
  utilization points per design (the same recipe #785's own
  `regenerate_flute_congestion_corpus.sh` already automates, generalized
  across designs) — real wall-clock cost, no algorithmic risk.

### 4.3 Priority 3 — DRC-driven local repair pass over `detailed_route -output_drc`, native-Rust hot-loop candidate

- **Technique:** generalize the `repair_antennas` precedent (§1, #759) from
  one specific violation class to a native-Rust local repair pass over
  TritonRoute's own `-output_drc` report (§1's "named, unused artifact"
  finding) — parse the report's per-violation geometry, classify against
  #666's own width/spacing rule table (§3, already-verified sky130 values),
  and attempt a small local fix (a jog, a via substitution, a segment
  nudge) only for violations whose fix is genuinely local (touching one
  net's one segment), never a global reroute. This is deliberately the
  narrowest possible "native routing" content: it operates on real,
  already-routed geometry and real DRC rules, but its own scope is bounded
  the same way §4.1's data-plumbing item is bounded — a repair, not a
  router.
- **QoR metric:** post-repair DRC-violation count on the merged GDS
  (`klt drc`'s own eventual gate) — directly comparable to the pre-repair
  count TritonRoute's own `-output_drc` report already provides, and to
  `repair_antennas`'s own existing before/after pattern
  (`antenna_violation_count`) as a template for a parallel
  `drc_repair_violation_count` (or similarly-shaped) additive response
  field.
- **Rust vs. flow:** **native-Rust hot-loop candidate**, and the first
  genuine one this document proposes (§4.1/§4.2 are data-plumbing/
  estimator-grounding, not routing algorithm content). The case for
  native over "ask OpenROAD to do another `detailed_route -output_drc`
  pass" mirrors #735 §3.5's own case for the legalizer spike: (a) it is a
  well-bounded sub-problem (fix *this specific* violation locally, not
  "reroute the design better"), with a crisp, checkable objective
  (violation count strictly decreases, connectivity is provably preserved
  the same structural way `native/legalize`'s own `def.rs` guarantees it —
  never parse/touch `NETS`); (b) it has a direct oracle (compare against
  simply re-running `detailed_route` a second time, which OpenROAD's own
  flow already does for antenna repair specifically); (c) it reuses the
  §4.1 stack descriptor and needs no new FFI pattern beyond what
  `native/legalize`/`native/congestion` already proved.
- **Wrap-vs-native trade-off, stated honestly:** OpenROAD's own
  `detailed_route`, re-run, already fixes most violations it itself
  introduces (the antenna-repair precedent's own reroute pass proves this
  works for one violation class already) — the honest case for a *separate*
  native pass is **not** "TritonRoute can't fix its own violations," it is
  "a narrow, local, native fixer is cheap to run inside a DSE loop or CI
  gate without paying for a second full `detailed_route` subprocess
  invocation on the whole design," the same fleet-throughput argument
  #735 §3.6 used to justify the congestion pre-check. This item's real risk
  is scope creep into "and now it's a router" — the acceptance bar below
  is written to guard against that explicitly.
- **Measurement plan:** on a corpus slice with **real, nonzero DRC
  violations post-route** — the known pre-existing `diff.enclosing.
  licon.1` filler-cell-gap violation `native/legalize/README.md`'s own
  comparison table documents as already present on the `gcd`/`modexp`
  corpus today (1 violation on `gcd`, 6 on `modexp`, both matching
  `tests/corpus/README.md`'s own documented baseline) gives at least one
  real, reproducible starting violation without needing to manufacture one
  — attempt a local repair, and report: violations fixed / violations
  attempted-but-not-fixed (must fall back to "leave it, report unfixed,"
  never silently drop a violation from the report) / new violations
  introduced (must be zero — a repair pass that trades one violation for
  another is a regression, not a fix) / wall-clock vs. a full
  `detailed_route` re-run.
- **Risk:** moderate. The connectivity-preservation guarantee is
  achievable by construction (mirror `native/legalize/src/def.rs`'s own
  "cannot parse NETS/PINS, therefore cannot mutate them" discipline); the
  genuinely open risk is whether "local" fixes exist for a large-enough
  fraction of real violation classes to be worth the engineering — some
  violation classes (e.g. genuine congestion-driven spacing violations)
  may have no local fix at all and always need a real reroute, which this
  item's own acceptance criteria must treat as an expected "reports
  unfixed" outcome, not a failure of the pass itself.

### 4.4 Priority 4 — Native global router spike (PathFinder-style), oracle-gated — explicitly deferred, not proposed for near-term dispatch

- **Technique:** the actual first slice of "native Rust routing" in the
  sense Epic #700's own Phase 2 narrative names it — a negotiated-
  congestion (§2.3) global router producing route guides for a fixed,
  OpenROAD-placed input (per the epic's own Champion-recorded Phase 2
  decision option "proceed to native routing on top of OpenROAD-placed
  layouts," 2026-08-12 comment on #700), feeding TritonRoute's own
  `-guide` input (§1 insertion point 2) rather than replacing detailed
  routing itself.
- **QoR metric:** total wirelength and overflow (tiles/edges over
  capacity) versus OpenROAD's own `global_route` output on the identical
  placed input — the standard global-routing comparison metric pair,
  directly analogous to how #735 §3.5 compared legalizer displacement
  against `opendp`'s own output on an identical GP input.
- **Rust vs. flow:** native-Rust hot-loop candidate, explicitly the
  higher-risk, higher-cost item in this document — global routing (even
  without detailed routing) is a genuinely harder multi-net simultaneous
  optimization problem than legalization was, and §0's own lesson from
  #784 is that this project's one prior attempt at "native beats OpenROAD
  on a bounded sub-problem" did not clear its QoR bar even on an easier
  problem shape.
- **Wrap-vs-native trade-off, stated honestly:** identical framing to
  #735 §3.5's own honest statement for the legalizer, updated for what
  that spike actually found: this is **not** proposed because OpenROAD's
  `global_route` is deficient (§2 found no evidence of that — §2.5's
  `util68` non-convergence was in `detailed_route`, not `global_route`,
  which completed on that same trial per the study's own data) — it is
  proposed, cautiously, as the natural next rung on Epic #700's own
  phase ladder, gated by an explicit go/no-go exactly like #784's own, so
  that a second "No-go" (if it happens) is cheap to learn from rather than
  a large sunk-cost commitment.
- **Measurement plan:** for each corpus design, run OpenROAD's own
  `global_route` once on an identical placed/CTS'd input (fixed seed);
  run the native global router on the same input; diff total wirelength,
  overflow-tile/edge count, and wall-clock; confirm the native router's
  guide output, fed to `detailed_route -guide`, still lets `detailed_route`
  reach a legal, DRC-clean result (the real end-to-end correctness gate —
  a global router that "wins" on its own metrics but produces guides
  `detailed_route` cannot legally honor has not actually won).
- **Risk:** high, by this document's own explicit framing — the item this
  survey most deliberately declines to recommend as a near-term
  dispatchable issue (see "Follow-on sketches" below, which names §4.1
  only, not this item, as Phase 2's first slice).
- **Priority note:** ranked below §4.1–§4.3 specifically *because* of §0's
  lesson, not despite it — a from-scratch native router spike is the
  highest-ambition, highest-risk item in this document, and this survey's
  own recommendation (per its acceptance criteria's own "Sketch"
  requirement) is to fund the cheap, boring, already-precedented items
  first and let their outcomes (particularly §4.3's local-repair
  experience with real routed geometry) inform whether §4.4 is worth
  attempting at all.

### 4.5 Named but not proposed — full native detailed routing (replacing TritonRoute)

Per §1's insertion point 4 and §0's own framing: this document does not
propose a "spike a native detailed router" issue. TritonRoute is a mature,
ISPD-contest-validated tool this project already wraps successfully (§1);
the one directly comparable prior attempt in this repo (native legalization,
an *easier* bounded sub-problem) missed its QoR bar substantially (§0). If
§4.4's global-router spike returns a **Go**, its own follow-on would be the
right place to revisit whether detailed routing itself is ever worth a
native attempt — not this document, and not before that evidence exists.

## 5. Measurement harness — common to every item above

- **Corpus.** `gcd` (`tests/corpus/place_and_route/gcd.gds.gz`,
  `tests/corpus/legalize/gcd/`), `modexp`
  (`examples/functional-verification/modexp.v`, also in
  `tests/corpus/legalize/modexp/`), and `mult8`
  (`tests/corpus/statime/mult8.v`) — the three-design proxy corpus Epic
  #704 and Phase 1's own native spikes (#784, #809) already established as
  this project's standing #520-corpus stand-in, per this issue's own body.
  As #735 §4 already noted and remains true: no batch harness over the
  full #520 (Tiny Tapeout) corpus exists yet — every item above scopes its
  own measurement plan to this smaller, already-available three-design
  proxy, consistent with how #784/#809 were actually measured.
- **OpenROAD as oracle.** For every native-Rust item (§4.2's estimator
  grounding, §4.3's repair pass, §4.4's global router), OpenROAD's own
  output on the identical input is the correctness/QoR bar, never this
  repo's own prior output — the same "reality-grounding discipline" #735
  §4 cites from Epic #700's own acceptance bar, and the discipline #784's
  own No-go verdict shows this project actually honors rather than only
  states.
- **`klt lvs`/connectivity-preservation gate.** Every item above changes
  geometry only, never netlist connectivity — §4.3 in particular commits to
  the same structural guarantee `native/legalize/src/def.rs` already
  proved (a repair engine that cannot parse `NETS`/`PINS` cannot mutate
  them, a guarantee stronger than an empirical spot-check).
- **`klt drc` gate.** The metric §4.3 actually targets directly; a standing
  sanity check for §4.4's own end-to-end legality claim.
- **No claim without a runnable check.** Every "Go" or "ship" recommendation
  in a follow-on issue from this survey must be backed by a real corpus run
  captured in that issue's own PR, per `docs/design-evidence-tiers.md` and
  this repo's own established practice (#784/#785/#809 above) — this
  survey itself makes no such claim, only proposes what to measure.

## 6. Follow-on implementation-issue sketch — Phase 2's first dispatchable slice

### Sketch A — tech-LEF routing-stack descriptor, plus real per-layer congestion-model grounding

**Title:** `place-and-route: parse the PDK tech-LEF routing-layer stack into a structured descriptor; rebase native/congestion's defaults on it`

**Why bundled:** §4.1 and §4.2(a) are the same shape of low-risk,
foundational change (a parser plus wiring its output into one existing,
already-shipped consumer), and §4.2's own corpus-expansion half (§4.2(b))
is naturally a second, smaller follow-on once the descriptor exists to
compare estimator output against — not bundled into this first sketch, to
keep it small and independently reviewable, mirroring how #735's own
Sketch A bundled three same-shaped flow changes but left the harder,
higher-variance item (the legalizer spike) as a separate Sketch B.

**Scope:**
1. A tech-LEF `LAYER ... TYPE ROUTING` parser (Python, reusing
   `klayout.db`'s existing LEF reader per §4.1's own reasoning, or a small
   new Rust crate if a concrete downstream native consumer already needs
   it in-process — the implementing issue should make this call once it
   has the tech LEF fixture in hand, not pre-decided here) emitting a
   per-`cell_library` JSON stack descriptor: layer name, direction, pitch,
   offset, width, plus via cut-array data for the layers between.
2. Unit tests against `tests/corpus/legalize/sky130_fd_sc_hd_tech.lef.gz`
   (already checked in — no new fixture needed for this half).
3. Rebase `native/congestion/src/contract.rs`'s `DEFAULT_TRACK_PITCH_UM`/
   `DEFAULT_LAYER_COUNT` (or, better, replace the single-scalar capacity
   model in `grid.rs` with a real per-layer sum) on the new descriptor's
   output for `sky130_fd_sc_hd`, with the existing `native/congestion` test
   suite re-run to confirm no regression against its own already-passing
   13 Rust + 13 Python unit tests.
4. A live cross-check (real volare `sky130A` install, not required for
   the unit tests above but required before claiming this item "done"):
   confirm the parser's own met1–met5 pitch/direction values are
   internally consistent (alternating direction, monotonic pitch) and
   match the one already-spot-verified number (`0.46 µm`, met2).

**Acceptance criteria:**
- Parser unit tests pass against the checked-in tech LEF fixture.
- `native/congestion`'s existing test suite passes unmodified after the
  capacity-model change (i.e., this is provably additive/corrective, not a
  behavior-changing rewrite of the estimator's own math beyond using real
  per-layer numbers instead of one constant).
- The live cross-check above, captured in the PR description with the
  actual parsed values, per this repo's "no claim without a runnable
  check" discipline.
- No change to `klt place-and-route`'s public request/response contract —
  this is internal to `native/congestion` and a new, separately-consumed
  descriptor artifact, not a `place-and-route.md`-documented field.

**Not in scope:** §4.2(b)'s corpus expansion (a separate, larger follow-on
needing several real `openroad-orfs` Docker runs per design); §4.3's
repair pass and §4.4's global-router spike (both explicitly later,
contingent items per §4's own staging, not bundled into Phase 2's first
slice).

## References

- Lee, C. Y. "An Algorithm for Path Connections and Its Applications."
  IRE Transactions on Electronic Computers, 1961.
- Kahng, A. B. et al. "TritonRoute: The Open Source Detailed Router" (best-
  effort citation, as #735 §2.3 also notes — OpenROAD's own project
  bibliography is the primary source to verify exact title/venue/authors
  against). ISPD, 2019.
- Batterywala, S., Shenoy, N., Nicholls, W., Zhou, H. "Track Assignment:
  A Desirable Intermediate Step Between Global Routing and Detailed
  Routing." ICCAD, 2002.
- McMurchie, L., Ebeling, C. "PathFinder: A Negotiation-Based
  Performance-Driven Router for FPGAs." FPGA, 1995.
- `docs/design/place-and-route-improvements-survey.md` (#735) — Phase 1's
  own survey; this document's structural and evidence-tier template, and
  the source of §2.1/§2.3/§2.8's own prior citations this document builds
  on rather than re-deriving.
- `docs/design/openroad-invocation-survey.md` (#397) — this repo's own
  real, live-verified OpenROAD invocation survey.
- `native/legalize/README.md` (#784) — the No-go native-legalizer spike
  this document's §0 draws its risk posture from.
- `native/statime/README.md` (#809) — the Go native-STA spike §2.4/§4.3
  cite as an in-process net-criticality building block.
- `docs/design/flute-congestion-precheck-results.md` (#785) — the Go
  (estimator)/deferred (gate) congestion pre-check spike; source of §2.5's
  real non-convergence evidence and §4.2's corpus-expansion scope.
- Issue #666 (closed) — the sky130 conductor-stack/DRC-coverage fix §3
  grounds against.
- Epic #700 (this document's own parent), including its 2026-08-12
  Champion comment naming the Phase 2 direction question §4.4 answers
  cautiously ("proceed to native routing on top of OpenROAD-placed
  layouts").
