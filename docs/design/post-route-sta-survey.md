# Survey & proposal: post-route static timing for `klt place-and-route`

**Status:** research / proposal, no implementation. Filed for issue #944, a
research-and-propose task under [Epic #700](https://github.com/2AMLogic/klayout-tools/issues/700)
Phase 3 ("place-and-route for `klt` — synthesis→placement→routing→GDS in
Rust, OpenROAD-validated"). This document plays the same role for Phase 3
that
[`docs/design/place-and-route-improvements-survey.md`](place-and-route-improvements-survey.md)
(#735) played for Phase 1 and
[`docs/design/native-routing-survey.md`](native-routing-survey.md) (#934)
played for Phase 2 — it is the "fresh eyes" input a later Champion pass
decomposes into dispatchable sub-issues, and it does **not** authorise
implementation of anything below.

**Phase 1 (#783/#784/#785) and Phase 2 (#938/#939) are both fully closed.**
Epic #700's own Phase 3 goal line states this phase's target precisely:
"Static timing (setup/hold across PVT) so a routed block can make the
digital-equivalent T1 claim the tier work (`#636`) defines" — i.e.
`docs/design-evidence-tiers.md`'s T1 checklist item 5 (digital column:
"multi-corner static timing analysis... across the PVT corner set") and,
this survey argues below, item 7 (digital: "the functional test suite
re-run against the post-route gate-level netlist with back-annotated SDF
timing"). Neither item is satisfied by anything this repo ships today for a
routed digital block, on grounds documented precisely in §1.

**This phase is explicitly *post-route*, a distinct scope from Epic #704's
already-shipped pre-route timing work.** `klt-statime-native` (issues
#809/#925/#926, Epic #704 Phase 3) is a real, wired-in, native-Rust
gate-level static-timing engine — but it is **wire-free** ("a net's arrival
equals its driver pin's arrival exactly... there is no placement, so no
parasitics," `native/statime/README.md`'s own "Known simplifications" item
1, restated verbatim in `docs/cli/synthesize.md`'s `sta` section) and has
**no SDC/`create_clock`** support at all. It is the right engine for
*pre-layout* QoR feedback inside `klt synthesize`'s own restructuring loop
(#926); it is not, and does not claim to be, what this issue is about. §1
below draws that line precisely rather than re-deriving #925/#926's own
already-accepted work.

**Required prior art, read first, not re-derived here:**

- [`docs/design/place-and-route-improvements-survey.md`](place-and-route-improvements-survey.md)
  (#735) — Phase 1's own survey and its evidence-tier/citation conventions,
  which this document follows exactly.
- [`docs/design/native-routing-survey.md`](native-routing-survey.md) (#934)
  — Phase 2's survey; §1's re-verified baseline table there is the direct
  ancestor of this document's own §1 (the `route` stage has grown one more
  call, `estimate_parasitics -global_routing`'s downstream consumers, since
  #934 was written — re-verified against the current tree below, not
  assumed unchanged).
- [`native/statime/README.md`](../../native/statime/README.md) (#809/#925)
  and [`docs/cli/synthesize.md`](../cli/synthesize.md)'s `sta`/"Timing-driven
  restructuring" sections (#925/#926) — the shipped **pre-route** native
  timing engine and its bounded resizing loop; this document's own §1.1
  restates only what is load-bearing for the pre-route/post-route boundary,
  not the whole of that work.
- [`docs/design/extract-fidelity-roadmap.md`](extract-fidelity-roadmap.md)
  (#737, closed) — the accepted survey of `klt extract --parasitics`'s
  lumped-RC engine, including a **real run on the exact routed GDS fixture
  Phase 1/2 P&R work produced** (`tests/corpus/place_and_route/gcd.gds.gz`).
  This is the single most load-bearing prior document for this survey's §2
  — read it before §2 below, which does not re-derive its numbers.
- [`docs/cli/place-and-route.md`](../cli/place-and-route.md) — the
  command's own contract documentation, ground truth for §1.
- [`docs/design-evidence-tiers.md`](../design-evidence-tiers.md) — the
  evidence-tier discipline this document follows, and the T1 items (5, 7)
  this phase's own acceptance bar is defined against.

## Evidence-tier discipline

Following this repo's own convention (`docs/design-evidence-tiers.md`; the
Phase 1/2 surveys' own tiering): this task did not run a full OpenROAD
place-and-route or corpus benchmark of its own — it is pure analysis, per
its own Definition of Done, reusing real measured data already captured by
`docs/design/extract-fidelity-roadmap.md`'s accepted `--parasitics` run
where relevant. Every claim below is one of:

- **[REPO]** — read directly from this repo's own source/docs, cited by
  file and line, re-verified against the current tree (commit `e1777d2`,
  2026-08-13) rather than assumed unchanged from a prior survey.
- **[REPO-RUN]** — a **real** result already captured by another accepted
  document in this repo, not re-derived from memory.
- **[LIT]** — a technique or finding from the published EDA-CAD literature
  or OpenROAD/OpenSTA's own documented command surface, cited by name/
  venue/year to the best of this survey's ability. Treat exact author lists
  and exact Tcl flag spellings as best-effort — verify against the primary
  source before citing in a paper trail that requires precision.
- **[PROPOSAL]** — this document's own reasoning/recommendation, not a
  claim about the world.

**Methodology note on live verification.** Issue #783's own
`clock_tree_synthesis` audit set the precedent of preferring live `info
body`/`help` introspection against a real `openroad/orfs` container over
reading source from memory; issue #939's own audit (`docs/cli/
place-and-route.md`'s "Timing-driven/repair-iteration routing flags"
section) documents the fallback when that is unavailable. This task
attempted the live path first — `docker pull openroad/opensta:latest` (the
same standalone, LEF-free image `native/statime/README.md` already
validated for pre-route STA comparison) — and it failed twice in this
task's sandboxed environment before any container ran: a first attempt
timed out inside `error getting credentials` (a credential-helper hang);
a second, retried attempt got past that but then failed with `no matching
manifest for linux/arm64/v8 in the manifest list entries` — this specific
task's host is Apple Silicon, and the `openroad/opensta` image apparently
publishes no `arm64` manifest (unlike `openroad/orfs:latest`, already
present in this same environment's local image cache from unrelated prior
work, which does). Neither failure is evidence the image or its commands
are unavailable in a normal (x86_64, or credential-configured) environment
— both are this specific task's own environment limitations, recorded
here rather than silently worked around. Every OpenSTA/OpenRCX command
cited in §3/§4 below is therefore **[LIT]**-tier, from OpenSTA's own
published Tcl command reference and general EDA-flow practice, **not
independently
container-verified in this task's environment** — exactly issue #939's own
posture, for the same underlying reason (no reachable container). Every
follow-on sub-issue this document proposes must re-verify its own exact
flag surface live, per that same precedent, before shipping.

## 1. Baseline: the two static-timing paths this repo has today, and the gap between them and "post-route STA"

### 1.1 Pre-route: `klt synthesize`'s `sta` field (Epic #704, out of this phase's scope)

**[REPO]**, restated only as far as needed to draw the boundary this phase
respects. `klt-statime-native` (`native/statime/`) parses a mapped
netlist + liberty pair and walks a rise/fall-aware timing graph with NLDM
delay lookup, reporting the design's worst path — verified within 1.34% of
an OpenSTA oracle on a 3-design corpus (`native/statime/README.md`,
"Results — accuracy"). It is wired into `klt synthesize` as the additive
`sta` response field (#925) and feeds a bounded cell-resizing loop
(`--restructure-timing`, #926). Every number it reports is, by its own
documented design, **wire-free** (no placement exists at synthesis time)
and **clock-free** (no SDC, uniform 0.05 ns input transition / 0.03 pF
output load on every primary port including the clock net). This is the
correct model for *its* stage — there is no placement yet to extract wire
delay from — and this phase does not propose changing it. It is cited here
only so the boundary is explicit: **nothing below asks "should `sta` model
wires" — it asks "what happens after `klt place-and-route` has actually
routed the design and wires exist to model."**

### 1.2 In-flow OpenSTA inside `klt place-and-route` — real, but not "post-route STA" in the parasitic-extraction sense

**[REPO]**, `src/klayout_tools/place_and_route.py`, re-read against the
current tree. Every stage script (`_stage_script_lines`) ends with
`_metrics_report_lines` (`place_and_route.py:1407-1427`) — the response's
`worst_slack_ns`/`total_negative_slack_ns`/`fmax_mhz`/
`setup_violation_count`/`hold_violation_count` fields (`docs/cli/
place-and-route.md`'s own response table) all come from a real OpenSTA
timing analysis, run inside the same OpenROAD process, over whatever
parasitics estimate that stage's own preceding call produced:

| Stage | Parasitics source before STA runs | Call |
| --- | --- | --- |
| `place` | Placement-only estimate (no routing topology at all — driver/sink Euclidean or simple model) | `estimate_parasitics -placement` (`place_and_route.py:1528/1538/1563`) |
| `cts` | Same placement-only estimate, re-run because the clock tree just changed placement | `estimate_parasitics -placement` |
| `route` | **Global-routing** estimate — RC from the `global_route` Steiner-tree topology and the tech LEF's own per-layer R/C, **not** the detailed-routed geometry `write_def` actually commits to disk | `estimate_parasitics -global_routing` (`place_and_route.py:1620`) |

`estimate_parasitics -global_routing` is OpenSTA's own Elmore-style RC
estimator over `global_route`'s coarse-grid Steiner topology — a real,
useful, and already-shipped estimate, but structurally **not** parasitics
extracted from the actual routed wire segments/vias `detailed_route`
placed. This is the precise sense in which today's `route`-stage timing
numbers are "post-*routing-stage*" but not "post-route STA on actual
routed parasitics" — the exact phrase issue #944's own body uses to define
this phase's scope, and the distinction this survey's §2/§4 build on.

**Two further, concrete gaps in what is reported, both [REPO]-verified
against the current tree, neither previously documented in a Phase 1/2
survey:**

1. **Single corner, always.** `_resolve_liberty` (`place_and_route.py:1241`)
   resolves exactly one `pdk.corner` per run (default: the nominal
   typical-process/room-temperature corner `klt pdk`'s own
   `list_cell_libraries` selection logic already picks —
   `src/klayout_tools/pdk.py:791` `_nominal_supply`). There is no loop over
   corners anywhere in `_stage_script_lines`, and no `define_corners`/
   multi-liberty `read_liberty -corner` call. This is the same "single-
   corner" characterization `docs/design/openroad-invocation-survey.md:342`
   already flagged for the ORFS reference flow this command mirrors — it
   was never closed. T1 checklist item 5's digital column requires
   "multi-corner static timing analysis (setup and hold across the PVT
   corner set)" — this command structurally cannot produce that today,
   however good its parasitics get.
2. **Only setup slack is reported as a value.** `_metrics_report_lines`
   calls `report_worst_slack_metric -setup` only (`place_and_route.py:1411`)
   — there is no `-hold` counterpart anywhere in this module. The response
   has `hold_violation_count` (a **count**, from the separate
   `report_check_types -min_delay -violators` stdout scrape,
   `place_and_route.py:1436`) but no `worst_hold_slack_ns` **value** at
   any stage. A caller can learn "3 hold violations exist" but not "by how
   much, worst-case" — a real, closed-form reporting gap independent of
   the parasitics question, worth closing alongside the multi-corner work
   in §4.2 since both touch the same `_metrics_report_lines` call site.

### 1.3 The gap, stated precisely

Neither path above is "post-route STA on actual routed parasitics, setup/
hold sign-off across PVT corners" — #944's own body's definition of this
phase's scope. §1.1 is wire-free by construction (correct for its stage).
§1.2 is wire-*aware* but from a coarse global-routing estimate, at exactly
one corner, with no hold-slack value and no SDF/back-annotation artifact
for a downstream gate-level simulation to consume. Closing this gap is
what §4 proposes, staged; §2 first grounds what real routed-parasitic data
this repo can already produce, since — as with Phase 1's legalizer and
Phase 2's routing survey — the honest first question is "what already
exists," not "what should be built from scratch."

## 2. Grounding the parasitic-extraction requirement

**The central finding of this section: real, per-net RC extracted from an
actual routed GDS — the artifact `klt place-and-route`'s Phase 1/2 shipped
routing already produces — already exists in this repo, on the exact
fixture this issue names, from an unrelated already-closed research task.**
This is not this survey's own new measurement; it is `docs/design/
extract-fidelity-roadmap.md` (#737)'s own accepted §1.2 table, reused here
as **[REPO-RUN]** evidence exactly as that document's own conventions
require, and it directly answers item 3 of issue #944's own "Do" list
("determine what routed-parasitic data is already available from Phase 1/2's
shipped routing output").

### 2.1 What's already available

`klt extract --parasitics` (`src/klayout_tools/extract.py`, `docs/cli/
extract.md` "Parasitic (RC) extraction") is a first-order lumped-RC engine
built directly on `klayout.db`'s `LayoutToNetlist` — per net, it walks
`polygons_of_net` on every registered conductor layer, computes area/
perimeter-based ground capacitance plus vertical-overlap (crossover)
coupling capacitance between distinct nets on adjacent metal levels
(shipped as of issue #760), and a star-topology series resistance apportioned
across each net's device terminals by Euclidean distance from the net's
own centroid. **This already ran, for real, on `tests/corpus/
place_and_route/gcd.gds.gz`** — the merged, routed GDS a real `klt
place-and-route` run produces (`extract-fidelity-roadmap.md`'s own §1.2
table, **[REPO-RUN]**):

| Layout | Devices | Nets | `c_count` | `r_count` | Total C (fF) | Total R (Ω) |
| --- | --- | --- | --- | --- | --- | --- |
| `gcd` (OpenROAD-routed, 94.0 × 94.0 µm) | 4 355 | 2 133 | 1 392 | 26 593 | **2 617.2** | 1 798 498.1 |

Per-net detail already measured on that same run: the largest net
(`RESET_B|rst_n`) reports **92.4 fF and 90 944.5 Ω across 200 terminal
legs** — the exact shape (a high-fanout reset/clock-like net) that matters
most for a post-route STA sanity check, since it is precisely the kind of
net a coarse Steiner-tree estimate (§1.2) and a star-topology lumped model
(this section) are each most likely to disagree with reality about, in
opposite directions.

**Provenance chain worth stating explicitly, since it is the load-bearing
fact for §4.1 below**: this is not a hypothetical "a router *could* feed an
extractor" — `_merge_def_to_gds` (defined at `place_and_route.py:1934`,
called from `place_and_route.py:782` in the `target_stage == "route"`
branch, Phase 0's own shipped work) is the exact function that produced
the GDS #737 extracted, and it merges the routed DEF's real geometry (not
a placement-stage abstraction) with the standard-cell GDS views. Net names on that merged
GDS come from `def2stream.py`'s own DEF→GDS label convention (metal-layer
text labels carrying the DEF net name), the same convention `klt extract`'s
own net-naming (`docs/cli/extract.md`'s "joined name" — a net's `nets[]`
entry is built from the labels found on its own conductor shapes) reads.
**Whether that label convention round-trips byte-for-byte to the flat net
names OpenSTA/`write_verilog -noattr` use internally (e.g. `_035_`,
`clk`) is not independently re-verified in this pass** — the observed
`RESET_B|rst_n` name (a `|`-joined pair, from klayout.db's own convention
for a net carrying two distinct text labels on the identical electrical
node) is evidence this correlation is *plausible*, not proof it is exact.
This is flagged explicitly as the first fact §4.1's own follow-on issue
must verify, not assumed here.

### 2.2 What's genuinely not available yet, precisely (not asserted to exist, not guessed)

- **No SPEF (or any other STA-consumable parasitics-interchange format)
  writer.** `klt extract --parasitics`'s own output is a JSON summary plus
  an annotated SPICE netlist (`docs/cli/extract.md`'s "JSON schema" /
  "Written SPICE netlist" sections) — a format `klt sim`'s ngspice backend
  consumes, not one OpenSTA's `read_spef` can. Translating the existing
  star-topology per-net R/C model into SPEF's `D_NET`/`CONN`/`CAP`/`RES`
  block syntax is new, but bounded, scope — a format translation of data
  already computed, not a new extraction algorithm (§4.1).
- **Single, nominal-only RC coefficient set.** `PARASITICS.metals`
  (`decks/sky130.py`) carries one sheet-resistance/area-cap/perimeter-cap
  triple per metal level — the nominal process corner only, **[REPO]**,
  confirmed by `extract-fidelity-roadmap.md:140`'s own gap table ("single
  nominal coefficient set per PDK; no corner axis"). A true multi-corner
  post-route STA sweep (§4.2) needs either a second/third RC coefficient
  set per corner (min/max routing-parasitic, the same axis the tech LEF's
  own `__min`/`__nom`/`__max` corner suffix already names, `pdk.py:526`'s
  own docstring) or, more cheaply, accepts today's single RC estimate held
  fixed while sweeping only the **liberty** (device-delay) corner — a real
  scoping choice §4.2 states explicitly rather than silently picking one.
- **Distributed (per-segment) RC remains its own, larger, already-scoped
  roadmap item — deliberately not re-proposed here.** `extract-fidelity-
  roadmap.md`'s own Stage 3 ("decompose each net's conductor into segments,
  build the RC ladder") is explicitly flagged there as "high — the largest
  single increment in that roadmap," not yet built, and is that document's
  own scope to sequence, not this one's. This survey's own §4 proposals
  below deliberately consume `--parasitics` at **whatever fidelity it is
  at when a follow-on issue starts** (today: Stage 0 ground C + Stage 2a
  vertical coupling, star-topology R) rather than re-litigating extraction
  fidelity — the alternative (blocking Phase 3 on Stage 3 landing first)
  would tie two separately-owned epics together for no evidence-backed
  reason. If accuracy work on §4.1's SPEF-fed STA result later shows the
  star-topology R model (§2.1's 90.9 kΩ high-fanout-net figure, plausibly
  overstated per Stage 3's own "removes the systematic ~2× overstatement...
  inherent in a single-section model" claim) is the dominant error source,
  that is itself a real, measured finding for #701/#709's roadmap to act
  on — not something this survey should pre-solve by scope-creeping into
  distributed RC under a "timing" issue title.
- **Via/contact resistance is not modelled at all** in `PARASITICS`
  (`extract-fidelity-roadmap.md:137`, **[REPO]**, re-confirmed) — every
  via in a routed net's path contributes 0 Ω today, a real omission a
  detailed-routed design (which is *made of* vias between metal levels)
  will feel more than the unrouted/abstract layouts `--parasitics` was
  originally measured against.
- **An unrelated, real, already-bundled KLayout capability worth naming
  for completeness, not proposed here**: the `klayout` Python package this
  repo already depends on ships a `klayout.pex` module (`RExtractor`/
  `RNetwork`) — a real distributed-resistance-network extractor over drawn
  polygon geometry, **[REPO]** (confirmed present in this repo's own
  resolved `.venv`, not previously cited anywhere in this repo's docs or
  source — grep-confirmed). This is a plausible building block for exactly
  the "Stage 3" distributed-R problem §2.2 above declines to re-scope here
  — noted as a fact for whichever future issue does take on that scope
  (its own roadmap, not this one), not evaluated further in this document.

**Net assessment**: unlike Phase 2's metal-stack finding ("substantially
available already... but real, named, bounded gaps"), this phase's
parasitic-extraction question resolves even more favourably — the core
data (real per-net R/C from an actually-routed GDS) is not merely
*derivable*, it has **already been computed and published**, on the exact
corpus fixture this phase's own issue names, by an unrelated closed
research task. The gap is entirely on the **STA-integration** side (a SPEF
writer, corner-sweep plumbing, net-name correlation verification), not on
the extraction-physics side — a materially cheaper starting position than
either Phase 1 (legalization, built from nothing) or Phase 2 (routing,
also built from nothing) had.

## 3. External SOTA survey

**3.1 SPEF as the standard PEX↔STA interchange format.** The Standard
Parasitic Exchange Format (Cadence-originated, now an open, widely-
implemented interchange standard — best-effort citation, **[LIT]**, verify
the exact standardising body before citing in a precision-sensitive
context) is how every mainstream extraction tool (Cadence QRC, Synopsys
StarRC, OpenROAD's own OpenRCX) hands a design's per-net RC network to a
static timer (OpenSTA's own `read_spef`, PrimeTime's `read_parasitics`).
It supports exactly the shape §2.1's star-topology model already computes
— arbitrary internal nodes per net, resistors between them, capacitors at
each node — via its `D_NET`/`CONN`/`CAP`/`RES` block syntax, so translating
`klt extract --parasitics`'s existing per-net output into SPEF is a format
mapping onto already-computed data, not a new physics model (§4.1).

**3.2 OpenRCX — OpenROAD's own extraction engine, cited for completeness,
not proposed as this phase's dependency.** OpenROAD ships `extract_
parasitics`/`write_spef` (the OpenRCX engine), driven by a per-process-
corner "extraction rules" file OpenROAD-flow-scripts' own sky130hd platform
ships (`platforms/sky130hd/rcx_patterns.rules` — best-effort filename
citation from this project's own general familiarity with the ORFS tree
structure other prior surveys in this repo have read directly, **not**
independently re-verified live in this pass, per this document's own
methodology note above). This is the "wrap OpenROAD's own extractor"
alternative to §2's "reuse `klt extract --parasitics`" path — cited here as
the SOTA baseline this phase's proposal (§4) explicitly chooses *not* to
take as its first step, and why: `klt extract --parasitics` already runs,
already produces real numbers on this repo's own corpus (§2.1), and needs
no new PDK asset (an `.rules` file this repo does not currently resolve or
ship a table for, the same "not derivable from the resolved PDK install
itself" problem `_CTS_BUFFER_CELLS`/`_ANTENNA_DIODE_CELLS` already solved
for CTS/antenna — real, but strictly more up-front cost than reusing an
engine this repo already has working). §4.1's own "Wrap-vs-reuse" framing
states this trade-off explicitly rather than assuming OpenRCX is out of
scope forever.

**3.3 Multi-corner sign-off — setup at the slow corner, hold at the fast
corner.** Standard industrial practice (general knowledge, **[LIT]**, not
attributable to a single citable paper): setup timing is checked at the
**slowest** process/voltage/temperature (PVT) corner (worst-case cell/wire
delay), hold timing at the **fastest** corner (worst-case race-through) —
a single-corner run, however accurate its parasitics, cannot make a real
signoff claim about either in isolation, because the corner that stresses
setup is, by construction, the corner least likely to also stress hold.
OpenSTA supports this via `define_corners` plus a `-corner` argument on
`read_liberty`/`report_checks`-family commands, analyzing multiple
liberty views of the same design in one session (**[LIT]**, OpenSTA's own
documented multi-corner analysis feature — not independently container-
verified in this pass, see methodology note). §4.2 proposes the minimum
version of this: sweep the corners a resolved `cell_library` already ships
`.lib` views for (§1.2's own gap finding), reporting worst setup / worst
hold across that sweep, rather than asserting full sign-off-grade
corner-merging machinery in one step.

**3.4 On-chip variation (OCV) / advanced-OCV (AOCV) / parametric-OCV
(POCV) derating.** Even at a fixed PVT corner, real silicon has
within-die process variation a single "typical" liberty timing arc does
not model — production signoff applies a derating margin (a simple
setup-side "add X%, hold-side subtract X%" OCV margin, or the
path-length/depth-aware AOCV refinement, or a fully statistical POCV
sigma-based model) to avoid claiming timing closure that is optimistic
by construction (Sanders, "On-Chip Variation," general industry-practice
citation, **[LIT]**; POCV: Habitz & Krieg, ISQED-era practice papers,
best-effort **[LIT]**). OpenSTA exposes the simplest of these,
`set_timing_derate -early`/`-late` (a flat percentage margin) — **[LIT]**,
not container-verified in this pass. §4.4 proposes this as the minimum,
cheapest realism improvement once §4.1/§4.2 land, explicitly *not*
proposing AOCV/POCV as a near-term item (both need path-depth or
statistical infrastructure this repo has none of today).

**3.5 SDF back-annotated gate-level simulation.** The Standard Delay
Format (IEEE 1497 — **[LIT]**) carries a static-timing engine's computed
cell/net delays back into a Verilog simulator via the `$sdf_annotate`
system task, so a functional testbench exercises the design against real
(or at least post-route-estimated) timing rather than the zero/unit-delay
model an RTL simulator uses by default. This is the standard mechanism
T1 checklist item 7's digital column names verbatim ("the functional test
suite re-run against the post-route gate-level netlist with back-annotated
SDF timing"). Two facts ground this repo's own reach for it, both
**[REPO]**: OpenSTA ships `write_sdf` (**[LIT]**, not container-verified in
this pass); and `klt functional-verification` already runs a gate-level
netlist (any `sources` value, explicitly including `klt synthesize`'s own
`netlist_path`, `docs/cli/functional-verification.md:175`) through Icarus
Verilog, which has documented `$sdf_annotate` support (**[LIT]**, Icarus's
own long-standing PLI feature, not independently re-verified live in this
pass). §4.3 proposes composing these two already-shipped pieces — a real,
concrete "T1 item 7, digital" path, not a new simulation engine.

**3.6 Graph-based vs. path-based analysis (GBA/PBA) and common-path
pessimism removal (CPPR).** Production STA tools run a fast graph-based
sweep (propagate worst-case arrival/required times through the whole
timing graph) to find candidate critical paths, then re-derive the exact
delay of the top-N candidates path-by-path (PBA) to remove the pessimism
GBA's node-local worst-casing introduces, especially where two paths share
a common clock-tree prefix (CPPR) — general, well-established EDA-flow
practice (**[LIT]**, not attributable to one paper). Cited here for
completeness of the SOTA survey and **explicitly not proposed** as a §4
item: OpenSTA's own `report_checks` already does GBA-with-CPPR by default
(**[LIT]**, general OpenSTA behaviour, not independently re-verified live
in this pass) — this is an "already using the mature technique via the
existing wrap" item in the same shape the Phase 1/2 surveys found
repeatedly, not a gap.

**3.7 Known gap, with this repo's own real evidence.** §1.2's two findings
(single-corner-only, setup-slack-value-only) plus §2.1's own real
`extract --parasitics` numbers (a 90.9 kΩ, 200-leg high-fanout net on a
real routed `gcd`) are not hypothetical SOTA gaps — they are concrete,
already-observed properties of this command's own output today, on a
design already in this repo's own corpus, exactly the evidentiary bar
Phase 1's §2.4 and Phase 2's §3.4 each set for their own strongest
priority-1 item. §4.1/§4.2 below are this document's equivalent.

## 4. Prioritized proposal

Six items. Items 1–4 are staged, in order, toward "real parasitics + real
corner sweep + a back-annotation artifact" — each individually valuable,
each buildable on what the previous item shipped, none requiring new Rust.
Item 5 is the one genuine native-Rust question this document raises,
explicitly staged as a spike behind items 1–2's own oracle data, mirroring
Phase 1/2's own "flow items first, native items oracle-gated and staged
last" discipline. Item 6 is stated and explicitly deferred, to be honest
about this repo's own T1 (not T2 commercial-signoff) ceiling per
`docs/design-evidence-tiers.md`.

### 4.1 SPEF export from `klt extract --parasitics`, wired into the `route` stage's OpenSTA session (Priority 1)

- **Technique:** (a) add a SPEF writer to `klt extract --parasitics`'s
  existing per-net R/C output (§2.1/§3.1) — a format translation of
  already-computed data, one new output artifact alongside the existing
  JSON/SPICE outputs, no new extraction algorithm; (b) in `klt
  place-and-route`'s `route`-stage script, after `write_def`/the DEF→GDS
  merge, add a `read_spef <file>` call (replacing, or run alongside as an
  A/B, `estimate_parasitics -global_routing`) before the existing
  `_metrics_report_lines`/`_violation_count_lines` report calls, so
  `worst_slack_ns`/`total_negative_slack_ns`/violation counts are computed
  against real routed-geometry-derived RC instead of a global-routing
  Steiner estimate.
- **QoR metric:** `worst_slack_ns`/`total_negative_slack_ns`/
  `setup_violation_count`/`hold_violation_count` delta between
  `estimate_parasitics -global_routing` (today's baseline) and the new
  `read_spef` path, on the identical routed design — the direct measure of
  how much today's estimate is actually wrong, which this survey does not
  presuppose the sign or magnitude of.
- **Rust vs. flow:** **flow/parameter on the `place_and_route.py` side**
  (one new Tcl call), **new Python on the `extract.py` side** (the SPEF
  writer itself — string formatting over data `_compute_parasitics`
  already produces, not new geometry/RC computation). Zero new Rust.
- **Wrap-vs-reuse trade-off, stated honestly:** OpenRCX (§3.2) is the
  "more standard" way to get SPEF into OpenSTA, but needs a PDK asset
  (`.rules` file) this repo does not resolve today, and this repo's own
  `klt extract --parasitics` engine already runs successfully, for real,
  on the exact fixture this phase cares about (§2.1) — reuse over rebuild,
  the same reasoning the routing survey used for every "use more of the
  tool already invoked" item, applied in the opposite direction (use more
  of *this repo's own* already-invoked tool, not OpenROAD's).
- **Measurement plan:** run `klt extract --parasitics` against `klt
  place-and-route`'s own `gds_path` output for each of `gcd`/`modexp`/
  `mult8` (the issue's own named #520 corpus proxy set), write SPEF,
  `read_spef` it into a fresh OpenSTA session alongside the same design's
  liberty, and diff `report_checks`'s own worst path/slack against (a)
  `estimate_parasitics -global_routing`'s value from the same run and (b)
  `klt-statime-native`'s pre-route wire-free value (§1.1) — three points on
  the same design, from most-optimistic (wire-free) through
  coarse-estimate to real-routed-RC, a real fidelity ladder rather than a
  single before/after diff. `klt lvs`/`klt drc` gates unaffected (this item
  changes no geometry or connectivity).
- **Risk:** the net-name correlation question §2.1 flags explicitly
  (`klt extract`'s GDS-label-derived net names vs. OpenSTA's own flat
  netlist net names) is the one real open question a follow-on issue must
  resolve **before** trusting `read_spef`'s own name-matching — if names do
  not correlate, `read_spef` either errors loudly (recoverable — fix the
  naming bridge) or silently drops annotation on unmatched nets (the
  dangerous failure mode, needing an explicit "N of M nets annotated"
  sanity check in the follow-on issue's own acceptance criteria, not
  assumed away here).

### 4.2 Multi-corner setup/hold sweep + a `worst_hold_slack_ns` value field (Priority 2)

- **Technique:** enumerate every `.lib` corner a resolved `cell_library`
  ships (the internal per-file walk `_nominal_supply`/`_parse_lib_corner`
  already does inside `pdk.py`, §1.2 — a small, additive extension to
  return the **list**, not just the nominal pick), run the `route` stage's
  OpenSTA session once per corner (or via `define_corners` in one session,
  §3.3, whichever a live-verified audit finds cheaper/more correct — an
  open implementation question this survey does not resolve), and report
  worst-case setup slack (across the slowest corners) and worst-case hold
  slack (across the fastest corners) as two new fields,
  `worst_setup_slack_ns`/`worst_hold_slack_ns` (closing §1.2's second gap
  — a hold slack **value**, not only a count — in the same change).
- **QoR metric:** the corner-to-corner spread itself is the metric —
  how much worse (or better) is setup/hold slack at the PVT extremes
  versus the single nominal corner this command reports today. A design
  that closes at nominal but fails at a real corner is exactly the false-
  positive T1 item 5 exists to catch.
- **Rust vs. flow:** **flow/parameter**, zero Rust. The corner-enumeration
  helper is a small `pdk.py` addition (returning data `_nominal_supply`
  already computes internally); the sweep itself is N re-runs (or one
  `define_corners` session) of the existing `route`-stage OpenSTA report
  calls.
- **Wrap-vs-native trade-off:** none — OpenSTA already supports
  multi-corner analysis (§3.3); this is "use more of the tool already
  invoked," the same shape as items 3.1/4.1 in the Phase 1/2 surveys.
- **Measurement plan:** for each corpus design, resolve every shipped
  corner for its `cell_library` (`sky130_fd_sc_hd` ships several — the
  exact list is a live-PDK-install fact this survey does not assert
  without checking a real `volare` install, flagged for the follow-on
  issue), run the sweep, report the corner-to-corner spread on
  `worst_setup_slack_ns`/`worst_hold_slack_ns`, and confirm the existing
  single-corner (nominal) numbers are unchanged as a regression check
  (this item is additive, per `docs/json-contract.md`'s own posture).
- **Risk:** wall-clock — N OpenSTA re-runs (or one heavier multi-corner
  session) inside a stage that already spawns a real OpenROAD subprocess
  per stage; worth measuring alongside QoR, not assumed free, the same
  caution the Phase 1 survey's §3.1 (`-timing_driven`) already applied to
  a smaller-scoped runtime addition.

### 4.3 SDF write + gate-level SDF-annotated functional re-simulation, closing T1 item 7 for digital (Priority 3)

- **Technique:** add `write_sdf <path>` to the `route` stage (after §4.1's
  `read_spef`, so the written SDF reflects real routed-parasitic delays,
  not the global-routing estimate), and — as a second, composable piece,
  not necessarily the same PR — verify `klt functional-verification`'s
  Icarus backend's `$sdf_annotate` support against the existing gate-level
  testbench convention (`sources` = `klt synthesize`'s `netlist_path`,
  already supported today per §3.5), so a caller can re-run the *same*
  functional test suite the pre-layout gate-level check already used,
  this time SDF-annotated.
- **QoR metric:** this is a **coverage** metric, not a QoR number — does
  the testbench's own pass/fail outcome change once real delay is
  present (the concrete failure mode this item exists to catch: a design
  that is functionally correct at zero-delay but exhibits a race/glitch
  once real setup/hold-adjacent timing is modelled).
- **Rust vs. flow:** **flow/parameter** on the `place_and_route.py` side
  (one new Tcl call, downstream of §4.1). The Icarus `$sdf_annotate`
  integration is new scope on the `functional-verification`/testbench
  side — not a new *engine*, but real new wiring (an SDF-aware testbench
  invocation mode `klt functional-verification` does not have today) —
  the honest reason this is Priority 3, not 1: it is real work beyond a
  Tcl-line addition, unlike §4.1/§4.2.
- **Wrap-vs-native trade-off:** none — both `write_sdf` and
  `$sdf_annotate` are existing tool features (OpenSTA, Icarus
  respectively); this item is entirely "compose two already-shipped
  pieces," the same shape the Phase 1 survey's §3.2 hold-repair item was.
- **Measurement plan:** on a corpus design with an existing gate-level
  functional-verification testbench (the `examples/functional-
  verification/` convention `native/statime/README.md` already reuses for
  `modexp`), run the same test suite three ways — RTL, zero-delay
  gate-level (today's existing `klt functional-verification` gate-level
  mode), and SDF-annotated gate-level (this item) — and report any status
  change per test, not only an aggregate pass/fail.
- **Risk:** Icarus's `$sdf_annotate` support and exact invocation
  convention are **[LIT]**-tier, not independently verified in this pass
  (methodology note above) — the single largest unverified assumption in
  this whole proposal, and the first thing a follow-on issue must confirm
  live before committing to this item's own scope. If Icarus's SDF support
  turns out too limited for this repo's own cocotb-driven testbench
  convention, this item's own "no-go" outcome (parking it, documenting
  why) is a valid, reportable result per this repo's own evidence-tier
  discipline — not assumed to succeed here.

### 4.4 On-chip variation (OCV) derating margin, a cheap realism increment once 4.1–4.2 land (Priority 4)

- **Technique:** add `set_timing_derate -early <margin> -late <margin>`
  (§3.4) as an optional request field on the `route` stage's OpenSTA
  session, applied on top of whatever corner sweep §4.2 lands.
- **QoR metric:** `worst_setup_slack_ns`/`worst_hold_slack_ns` delta with
  vs. without a stated derating margin — quantifies exactly how much of
  today's (or §4.1/4.2's) reported margin is an artifact of assuming zero
  within-die variation.
- **Rust vs. flow:** **flow/parameter**, zero Rust, one optional request
  field plus one Tcl line.
- **Wrap-vs-native trade-off:** none.
- **Measurement plan:** A/B the same corpus design/corner with a
  documented derating percentage (this survey does not propose a specific
  value — that is itself a PDK/foundry-characterization question outside
  this repo's own open-PDK data, flagged rather than guessed) and report
  the slack delta.
- **Priority note:** ranked below §4.1–4.3 because it is a margin
  *policy* choice layered on top of a real-parasitics + multi-corner
  result that does not exist until those ship — applying a derating
  margin to today's coarse `estimate_parasitics -global_routing` single-
  corner number would be adding false precision to an already-approximate
  base, not a meaningful improvement on its own.

### 4.5 Native-Rust extension: real routed RC into `klt-statime-native`'s in-process timing graph, oracle-gated (Priority 5)

- **Technique:** extend `klt-statime-native`'s timing graph (`sta.rs`) to
  accept a per-net RC annotation (from §4.1's SPEF, or directly from `klt
  extract --parasitics`'s own JSON — either is a data-format question, not
  a new algorithm) and add wire delay to its currently wire-free
  propagation, closing "Known simplifications" item 1 in
  `native/statime/README.md` with **real** (if first-order, star-topology)
  routed RC rather than a synthetic wireload model.
- **QoR metric:** accuracy against the same oracle discipline
  `native/statime/README.md`'s own "Results — accuracy" table already
  established — diff this engine's post-route worst-path delay against
  OpenSTA's `read_spef`-fed result from §4.1, the same "within a documented
  tolerance, same start/end pins" bar that comparison already used for the
  wire-free case.
- **Rust vs. flow:** **native-Rust hot-loop candidate**, explicitly staged
  *last* and *behind* §4.1's own oracle data — the same discipline the
  Phase 1 survey applied to its own native-legalizer item (§3.5 there) and
  the Phase 2 survey applied even more cautiously to native routing (§4.2
  there, informed directly by the legalizer's own No-go). This survey
  applies the identical caution for the identical reason: `native/
  legalize/README.md`'s own root-caused QoR miss (21.8–403.4% worse than
  OpenROAD on the *easiest* P&R sub-problem attempted so far) is direct
  evidence that "smaller, well-bounded, crisp-objective sub-problem" is
  necessary but **not sufficient** for a native-Rust reimplementation to
  match a mature open tool — this item should not proceed until §4.1 has
  already produced a real OpenSTA-plus-real-RC number to gate against.
- **Wrap-vs-native trade-off, stated honestly:** `klt-statime-native`'s own
  case for existing at all (per its README's own verdict) is a measured
  1.5–2.1× cold-invocation latency advantage over wrapping OpenSTA, plus
  the *unmeasured but structurally real* in-process-call argument (no
  subprocess/CLI/JSON round trip) — both arguments this item would need to
  re-establish specifically for the post-route, RC-aware case, not assume
  carried over from the pre-route measurement (real RC parsing/lookup adds
  new per-call cost neither number above included).
- **Measurement plan:** for each corpus design, produce SPEF via §4.1;
  feed it two ways — OpenSTA's own `read_spef` (the oracle) and this
  engine's new RC-aware propagation — diff worst-path delay (accuracy) and
  wall-clock in both the cold (fresh process) and warm (liberty+RC
  pre-loaded) regimes `native/statime/README.md`'s own performance table
  already used, so this item's own numbers are directly comparable to the
  pre-route spike's, not a fresh, incomparable measurement.
- **Complexity/risk:** medium-high. Genuinely new territory (SPEF/RC-table
  parsing, wire-delay propagation added to a timing graph that has never
  modelled it) on an engine whose own README already documents a real,
  measured, still-open 0.36–1.34% accuracy gap even in the *simpler*
  wire-free case (§"Known simplifications" item 5 there) — this item
  should expect that gap to grow, not shrink, once a genuinely harder
  (RC-propagation) problem is added, and its own go/no-go gate should be
  evaluated against that expectation, not a naive "should still be close"
  assumption.

### 4.6 Full PVT+AOCV/POCV commercial-grade signoff parity — explicitly deferred, not proposed now (Priority 6, informational)

- **Technique:** the complete industrial post-route signoff stack — every
  characterized PVT corner (not the subset §4.2 sweeps), AOCV/POCV
  statistical derating (§3.4) rather than a flat margin, and
  commercial-tool cross-checking (Synopsys PrimeTime, Cadence Tempus).
- **Why not proposed as a near-term item**: `docs/design-evidence-tiers.md`
  states this repo's own ceiling explicitly — "this toolkit's closed loop
  targets T1; T2+ require tools and fab access outside its scope." T2's
  own definition is literally "T1, plus DRC/LVS signoff and simulation on
  commercial tools with the foundry's own decks." Chasing commercial-grade
  signoff parity with open tools alone is not this repo's own stated goal
  — its goal is T1, defined and checkable with open tools, which §4.1–4.4
  above already target precisely.
- **Recommendation:** do not fund this directly. Re-state it here only so
  a future reader does not mistake "we did not build AOCV/POCV" for an
  oversight this survey missed — it is a scope boundary this repo's own
  evidence-tier document already draws, restated for this specific phase.

## 5. Measurement harness — common to every item above

- **Corpus.** The issue's own named #520 corpus proxy — `gcd`, `modexp`,
  `mult8` — the same three-design set Phase 2's own survey used, already
  present as place-and-route-shaped fixtures per that survey's own §5
  sketch (issue #941, merged, per this repo's own recent commit history).
- **OpenSTA as oracle.** Every item above that touches a number OpenSTA
  itself can independently compute (§4.1's SPEF-fed slack, §4.2's
  multi-corner sweep, §4.5's native RC-aware engine) is scored against
  OpenSTA's own output on the identical input, per Epic #700's own
  "reality-grounding discipline" ("OpenROAD is the mature open P&R
  flow... each klt P&R stage is validated by comparing its result...
  against OpenROAD's" — **[REPO]**, the epic's own body) — never against
  this repo's own prior output.
- **`klt extract --parasitics` as the RC source, not a second oracle to
  validate against here.** §2's own finding is that this engine's *output*
  is real and usable; its own *accuracy* (star-topology R vs. a
  distributed ladder, coupling completeness, corner axis) is `docs/design/
  extract-fidelity-roadmap.md` (#737/#701/#709)'s own, separately-owned
  measurement program — this survey's own items consume that engine's
  current fidelity as a fixed input, per §2.2's explicit scoping choice.
- **`klt lvs`/`klt drc` gates.** Every item above changes reported timing
  numbers or adds a new artifact (SPEF, SDF) — none touches routed
  geometry or connectivity. `klt lvs`/`klt drc` are expected unchanged-pass
  standing regression gates for every item, a failure being a defect
  report, not an accepted trade-off — the same posture both prior surveys'
  own §4/§5 harness sections state.
- **A/B protocol.** Same request document, same `seed` (place-and-route is
  genuinely stochastic), one change toggled at a time, JSON response
  fields diffed directly (`worst_slack_ns`, the new `worst_setup_slack_ns`/
  `worst_hold_slack_ns`, `setup_violation_count`, `hold_violation_count`,
  wall-clock), never eyeballed from logs — the same convention every prior
  survey in this family has used.

## 6. Follow-on implementation-issue sketches

### Sketch A — SPEF export + `read_spef` wiring, the real-parasitics baseline (§4.1)

**Title:** `place-and-route: extract real post-route parasitics via klt extract --parasitics, feed OpenSTA via SPEF`

**Why this is the right *first* Phase 3 issue:** every other item in this
document (§4.2's corner sweep, §4.3's SDF write, §4.5's native RC-aware
engine) is more valuable, or only meaningful at all, once real routed RC
— not a global-routing estimate — is flowing into the `route` stage's own
STA session. This is also, per §2's own finding, the **cheapest** item in
this document: the extraction physics already exists and already ran on
this exact corpus; the new work is a format writer plus one Tcl line.

**Scope:**
1. A SPEF writer for `klt extract --parasitics`'s existing per-net R/C
   model (new `--format spef`-style output or a new flag; exact shape is
   this issue's own to decide, following `docs/cli/extract.md`'s existing
   additive-field conventions).
2. Verify (live, against a real `openroad/orfs` or `openroad/opensta`
   container — the methodology-note gap this survey itself could not
   close) that `read_spef`'s net-name matching correlates correctly
   against `klt place-and-route`'s own routed netlist net names (§2.1's
   own flagged risk) — including an explicit "N of M nets successfully
   annotated" sanity check, not a silent partial-match.
3. Wire `read_spef <file>` into the `route` stage's script, after
   `write_def`/the DEF→GDS merge, either replacing or (for the A/B
   measurement this issue's own acceptance criteria need) running
   alongside `estimate_parasitics -global_routing` behind a request flag.
4. Update `docs/cli/place-and-route.md`'s response table for any new/
   changed metric provenance; `docs/cli/extract.md` for the new writer.

**Acceptance criteria:**
- `klt extract --parasitics --format spef` (or equivalent) produces a
  syntactically valid SPEF file on the existing `gcd`/`modexp`/`mult8`
  fixtures, cross-checked by loading it into a real OpenSTA session
  (`read_spef`, no error) — real OpenROAD/OpenSTA, not stubbed.
- The net-name correlation check from scope item 2 passes at 100% (or the
  gap is characterised and reported, not silently dropped) on all three
  corpus designs.
- A/B `worst_slack_ns`/`total_negative_slack_ns`/`setup_violation_count`/
  `hold_violation_count` reported for both the existing
  `estimate_parasitics -global_routing` path and the new `read_spef` path,
  captured in the PR description per this repo's "no claim without a
  runnable check" discipline.
- `klt lvs`/`klt drc` unchanged-pass on the corpus.

**Not in scope:** the multi-corner sweep (§4.2), SDF write (§4.3), and the
native-Rust extension (§4.5) are separate, larger follow-on issues that
consume this sketch's own SPEF output as their measurement substrate —
deliberately not bundled here, mirroring both prior surveys' own "cheapest,
most-certain-value item first" sequencing.

### Sketch B — multi-corner setup/hold sweep + `worst_hold_slack_ns` (§4.2)

**Title:** `place-and-route: sweep setup/hold slack across every shipped PVT corner, report worst-case values`

**Why second, not bundled with Sketch A:** this is the other half of T1
item 5's own two-part requirement ("multi-corner... setup **and hold**")
and can proceed independently of Sketch A's SPEF work (it is meaningful
even against today's `estimate_parasitics -global_routing` baseline, as a
pure corner-sweep addition) — but is naturally sequenced after Sketch A
since a caller evaluating "did timing close" wants both real parasitics
and the right corner in the same answer, not two separately-caveated
partial results shipped far apart.

**Scope:**
1. A small `pdk.py` addition exposing the full list of `.lib` corners a
   resolved `cell_library` ships (generalizing `_nominal_supply`'s own
   internal per-file walk, §4.2), not only the nominal pick.
2. Sweep the `route` stage's OpenSTA session across that corner list (one
   re-run per corner, or `define_corners` in one session — this issue's
   own live-verified implementation choice, per §4.2's own open question).
3. Two new additive response fields, `worst_setup_slack_ns`/
   `worst_hold_slack_ns` (the corner-swept worst-case values), alongside
   the existing single-corner `worst_slack_ns` (kept unchanged, for
   backward compatibility, per `docs/json-contract.md`'s additive posture).
4. Update `docs/cli/place-and-route.md`'s response table and request table
   (if a corner-subset selection field is added).

**Acceptance criteria:**
- Real OpenROAD run producing per-corner slack values for `gcd`/`modexp`/
  `mult8`, showing the corner-to-corner spread (not merely that the fields
  exist and are non-`null`).
- Existing single-corner fields (`worst_slack_ns`, etc.) unchanged for a
  request that does not opt into the sweep — a strict backward-
  compatibility regression test.
- Wall-clock cost of the sweep measured and reported (§4.2's own flagged
  risk), not assumed free.
- `klt lvs`/`klt drc` unchanged-pass on the corpus.

**Not in scope:** SDF write (§4.3), OCV derating (§4.4), and the native-Rust
extension (§4.5) — separate follow-on issues.

## References

- IEEE 1497-2001, "Standard Delay Format (SDF) for the Electronic Design
  Process."
- Sanders, D. et al., general industry practice on-chip-variation (OCV)
  derating methodology (best-effort category citation, **[LIT]**, verify
  the primary reference before citing precisely).
- Habitz, P., Krieg, C. et al., statistical/parametric OCV (POCV)
  signoff methodology (best-effort category citation, **[LIT]**).
- `docs/design/place-and-route-improvements-survey.md` (#735) — Phase 1's
  own survey; this document's direct structural and evidentiary ancestor.
- `docs/design/native-routing-survey.md` (#934) — Phase 2's own survey;
  this document's own §1 baseline directly extends its re-verified table.
- `docs/design/extract-fidelity-roadmap.md` (#737) — the accepted survey
  whose real, already-run `--parasitics` extraction on the routed `gcd`
  fixture is this document's own central §2 finding.
- `native/statime/README.md` (#809/#925) — the shipped pre-route native
  timing engine this document draws its own scope boundary against (§1.1),
  and the oracle-comparison methodology §4.5 directly reuses.
- `docs/cli/synthesize.md`'s `sta`/"Timing-driven restructuring" sections
  (#925/#926) — the pre-route integration and resizing-loop contract.
- `docs/cli/place-and-route.md` — this repo's own real, ground-truth
  contract documentation for the command this survey proposes extending.
- `docs/cli/functional-verification.md` — the existing gate-level
  simulation path §4.3's SDF-annotation proposal builds on.
- `docs/design-evidence-tiers.md` — the T1 checklist (items 5, 7) this
  phase's own acceptance bar is defined against, and the T1/T2 scope
  boundary §4.6 explicitly respects.
- Epic #700 (this document's own parent, Phase 3) and Epic #520 (the Tiny
  Tapeout corpus this document's measurement plan targets, via the
  gcd/modexp/mult8 proxy named in issue #944's own body).
