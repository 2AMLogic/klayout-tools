# Roadmap: extraction fidelity for `klt extract --parasitics`

**Status:** research / proposal, no implementation. Filed for issue #737, a
research-and-propose task under the re-scoped
[Epic #701](https://github.com/2AMLogic/klayout-tools/issues/701) (Method of
Moments field solver) and
[Epic #709](https://github.com/2AMLogic/klayout-tools/issues/709) (PEX-aware
post-layout sim flow). It sequences the fidelity stages *between* today's
shipped lumped-RC extraction and #701's field solver, and names the first
concrete, measurable increment. It does **not** authorise implementation of
anything below.

**What this document does not settle.** Both parent epics carry
`loom:operator-only`. This roadmap does not presume either epic's outcome: it
takes today's shipped `--parasitics` as the baseline, grades every proposed
stage against measurable improvement over *that*, and treats the field solver
as the top stage's oracle rather than as a foregone replacement for the
shipped path.

**Required prior art, read first and not re-derived here:**

- [`docs/cli/extract.md`](../cli/extract.md) → "Parasitic (RC) extraction
  (`--parasitics`)" — the shipped contract, and §1's ground truth.
- [`docs/design/lvs-extraction-spike.md`](lvs-extraction-spike.md) →
  "Addendum (#216): parasitic (RC) extraction interface decision" — the
  accepted decision this command implements: parasitics stay inside `klt
  extract` behind a flag, the model is "fixed, not tunable" in a first cut,
  net-to-net coupling is "a credible second increment once a friction log
  demands it," and the engine choice was left to the implementation (#217).
  This document is that second increment's argument, not a re-opening of the
  interface decision.
- [`docs/design/em-field-sim-spike.md`](em-field-sim-spike.md) (#103) — the
  **closed** E&M engine survey. Stage 4 below cites its recommendation
  directly and deliberately does **not** re-survey field-solver engine
  choice: that decision is already spiked (quasi-static / DC-extrapolation as
  a *solver mode* of whichever full-wave engine is adopted, not a new external
  dependency; FastCap/FastHenry named as the right method class but
  disqualified on unresolved licensing).
- Issues **#718** (define `klt mom`, quasi-static capacitance MVP) and
  **#719** (closed-form validation + convergence) — Epic #701's freed
  Phase-0/1 children. §6 states the coordination explicitly: they *are* Stage
  4's first slice, not a parallel effort, and nothing proposed here duplicates
  or contradicts their acceptance criteria.
- [`docs/design-evidence-tiers.md`](../design-evidence-tiers.md) → T1
  checklist item 7 ("Post-layout verification… Until parasitic extraction
  lands (#217), state what the extracted netlist does and does not model") —
  the reason fidelity here is a T1 blocker for every analog canary at once.

## Evidence-tier discipline

Following this repo's own convention (`docs/design/place-and-route-improvements-survey.md`'s
tiering, `docs/design-evidence-tiers.md`'s broader ladder). Every claim below
is tagged:

- **[REPO]** — read directly from this repo's source/docs, cited by file (and
  line where it pins a specific mechanism).
- **[REPO-RUN]** — a **real** measurement this task ran, on this tree, against
  layouts already committed to this repo. Every number tagged this way is
  reproducible with the command quoted beside it; none is recalled or
  estimated. All `[REPO-RUN]` numbers below were produced on
  `feature/issue-737` at branch point `e0bdcda` (2026-08-11) with the repo's
  own `sky130` deck.
- **[PDK]** — transcribed from a **public** PDK source file (sky130's own
  magic technology file, as published in `fossi-foundation/open-pdks`, the
  same public source the shipped `PARASITICS` coefficients already cite).
  Never NDA'd data.
- **[LIT]** — a technique or result from the published EDA-CAD literature,
  cited by author/venue/year to the best of this task's ability without live
  network access. Treat exact author lists and titles as best-effort; verify
  against the primary source before reusing in a paper trail that requires
  precision.
- **[PROPOSAL]** — this document's own reasoning or recommendation, not a
  claim about the world.

No claim below rests solely on an uncited assertion.

## 1. Baseline: exactly what `--parasitics` computes today

### 1.1 The pipeline

**[REPO]**, from `src/klayout_tools/extract.py` and `docs/cli/extract.md`.
`--parasitics` is a **first-order lumped reduction built in-process**, not a
wrapped PEX engine — KLayout's `db` module has no interconnect-mesh RC
extraction call, and the addendum's engine survey found no suitably-licensed,
headless, KLayout-scriptable interconnect-PEX engine that avoids a second
geometry backend (`docs/cli/extract.md` → "Engine: a first-order lumped
reduction"). Concretely, per net:

| Step | Mechanism | Source |
|---|---|---|
| Per-net geometry | `LayoutToNetlist.polygons_of_net` per registered conductor layer, unioned; transistor-gate poly subtracted (#226) | `_net_area_perim_um`, `extract.py:4050` |
| Ground capacitance | `Σ_roles (area_um2 × cap_area_ff_um2 + perimeter_um × cap_perim_ff_um)` | `_compute_parasitics`, `extract.py:4176-4179` |
| Series resistance | `Σ_roles (sheet_res_ohm_sq × n_squares)` | `extract.py:4180` |
| `n_squares` | One **equivalent rectangle** per layer from `(A, P)`: `L`,`W` are roots of `t² − (P/2)t + A = 0`, squares `= L/W`, clamped `≥ 1` | `_n_squares`, `extract.py:4022`; `equivalent_rectangle_um`, `pdk_models.py:373` |
| Topology | **Star** (#592): net is the hub, each device terminal moves onto its own leg net behind a series resistor, one ground capacitor at the hub | `docs/cli/extract.md` → "The model: a star topology" |
| Leg split | Net's total R apportioned by each terminal's Euclidean distance from the centroid of all terminal positions, read from `Device.trans` (one transform **per device**, not per terminal) | `_terminal_star_weights` / `_terminal_star_positions_um`, `extract.py:4293-4335` |
| Coefficients | Curated per-PDK `PARASITICS` table, transcribed with citations from each PDK's **public** magic tech file | `decks/sky130.py:951`, `decks/gf180mcu.py` |

The model is fixed, not tunable (no `fast`/`accurate` selector) and
uncalibrated to silicon — both explicit non-goals of the first cut
(`docs/cli/extract.md`; #216 "Non-goals") **[REPO]**.

### 1.2 What it actually produces on real layouts **[REPO-RUN]**

Three layouts already committed here, extracted on this tree with
`PYTHONPATH=src python -m klayout_tools.cli extract <gds> --deck sky130
--parasitics --format json`:

| Layout | Source | Devices | Nets | `c_count` | `r_count` | `total_capacitance_ff` | `total_resistance_ohm` |
|---|---|---|---|---|---|---|---|
| `sky130_fd_sc_hd__inv_1` | `blocks/sky130_fd_sc_hd__inv_1/output/` | 2 | 6 | 4 | 4 | **1.2695** | 885.2 |
| `sky130_fd_sc_hd__dfxtp_2` | `blocks/sky130_fd_sc_hd__dfxtp_2/output/` | 26 | 18 | 12 | 70 | **10.881** | 12 578.7 |
| `gcd` (OpenROAD-routed block, 94.0 × 94.0 µm) | `tests/corpus/place_and_route/gcd.gds.gz` | 4 355 | 2 133 | 1 392 | 26 593 | **2 617.2** | 1 798 498.1 |

Per-net detail worth keeping in view for §3's measurement plans:

- `inv_1`: output net `Y` → **0.2397 fF**, 106.1 Ω split as two 53.07 Ω legs.
- `dfxtp_2`: output net `Q` → **0.2434 fF**, 129.3 Ω over 4 legs; largest
  internal net `$4` → 1.807 fF / 2 899.9 Ω.
- `gcd`: largest net `RESET_B|rst_n` → 92.4 fF and **90 944.5 Ω** across
  **200** terminal legs.

### 1.3 Captured vs. omitted — precisely

**[REPO]** (the `parasitics.model` block, #728, is the machine-readable form
of this table; `docs/cli/extract.md` → "Parasitic model scope"):

| Physical effect | Today | Where it is stated |
|---|---|---|
| Net-to-substrate **area** capacitance | ✅ modelled, per conductor layer | `model.capacitance` |
| Net-to-substrate **perimeter/fringe** capacitance | ✅ modelled, isolated-edge coefficient applied to the net's whole perimeter | `model.capacitance` |
| **Lateral** net-to-net coupling (same layer, sidewall) | ❌ **exactly zero** | `model.coupling` |
| **Vertical** net-to-net coupling (crossover / plate overlap on adjacent layers) | ❌ **exactly zero** | `model.coupling` |
| **Fringe shielding** (an edge facing a neighbour does not also see full substrate fringe) | ❌ not modelled — full substrate fringe charged regardless of neighbours | implied by `model.capacitance` |
| Distributed RC (per-segment ladder) | ❌ one lumped R per net, star-apportioned | `model.resistance` |
| True per-terminal routed-path resistance | ❌ coarse `Device.trans` centroid weighting | `docs/cli/extract.md` → "Per-terminal resistance is coarse" |
| Via / contact resistance | ❌ not modelled — vias carry connectivity only, no `sheet_res` term (`vias` is not a `PARASITICS` role) | `_compute_parasitics` roles list, `extract.py:4124-4149` |
| Self / mutual **inductance** | ❌ not modelled at all — no L element exists anywhere in the output | `model.frequency` |
| Frequency dependence (skin effect, distributed T-line behaviour) | ❌ quasi-static, one frequency-independent R and C | `model.frequency` |
| Temperature / corner dependence of R and C | ❌ single nominal coefficient set per PDK; no corner axis | `decks/sky130.py:951` (one `LayerRC` per level) |
| Device junction capacitance | ✅ **delegated** to the device model via `AS`/`AD`/`PS`/`PD`; deliberately not double-counted here (#226, #695) | `docs/cli/extract.md` → conductor roles |

Two of these deserve emphasis because they are easy to misread:

- **"No coupling" does not mean "capacitance is uniformly too low."** The
  charge that physically terminates on a neighbouring conductor is not
  dropped; it is **charged to ground** through the substrate fringe
  coefficient (see §1.5). Total capacitance can therefore land in a plausible
  ballpark while being attributed to the wrong node — which is exactly wrong
  for crosstalk, Miller-effect delay, and any shielded high-impedance node.
- **Inductance is absent by construction, not by tolerance.** There is no L
  element, so every inductive effect (supply-network ringing, clock-spine
  overshoot, RF behaviour) is reported as identically zero. That is #701's
  Phase-2 territory and is deliberately the *last* stage below.

### 1.4 The PDK already publishes coefficients for what is missing **[PDK]**

The shipped coefficients come from sky130's own magic technology file
(`libs.tech/magic/sky130A.tech` in a volare/open-pdks install — the file
`decks/sky130.py`'s comments already cite field-by-field). That file's
extraction section defines **five** coefficient families per conductor level.
`--parasitics` reads **two**:

| magic keyword | Physical meaning | sky130 example value | Read today? |
|---|---|---|---|
| `defaultareacap` | area cap to the substrate plane | `allm1 metal1 25.78` aF/µm² | ✅ `cap_area_ff_um2` |
| `defaultperimeter` | fringe cap from an edge to the substrate plane | `allm1 metal1 40.57` aF/µm | ✅ `cap_perim_ff_um` |
| `defaultsidewall` | **lateral** coupling between same-plane conductors | `allm1 metal1 44 0.25` | ❌ |
| `defaultoverlap` | **vertical** plate coupling to a specific lower plane | `allm1 metal1 allli locali 114.20` aF/µm² | ❌ |
| `defaultsideoverlap` | fringe from an edge down onto a specific lower plane | `allm1 metal1 allli locali 59.50` aF/µm | ❌ |

The three unread families are the coefficient set for Stages 2a/2b/2c below.
This matters for cost: **Stage 2 needs no new data source, no new license
question, and no new curation pattern** — the same public file, the same
"transcribe with an inline citation" discipline (#547's `metals_without_coefficient`
gap-reporting mechanism included), the same `LayerRC`-style table.

Selected `defaultoverlap` values, for §1.5's arithmetic **[PDK]**:

| Pair | `defaultoverlap` (aF/µm²) | Sum of the two levels' own `defaultareacap` | Ratio |
|---|---|---|---|
| met1 over li1 | 114.20 | 25.78 + 36.99 = 62.77 | **1.82×** |
| met2 over met1 | 133.86 | 17.50 + 25.78 = 43.28 | **3.09×** |
| met3 over met2 | 86.19 | 12.37 + 17.50 = 29.87 | **2.89×** |
| met4 over met3 | 84.03 | 8.42 + 12.37 = 20.79 | **4.04×** |

One caution, flagged rather than guessed: `defaultsidewall`'s **second**
number (`0.25` for met1, `0.3` for met2, `0.14` for li1) is a
distance/scaling parameter whose exact magic semantics must be read out of
magic's own tech-file manual before transcription. This roadmap deliberately
does not assume it, and Stage 2b's cost estimate includes reading it.

### 1.5 What the measurements say the model *is* **[REPO-RUN]**

Decomposing the ground capacitance of the routed `gcd` block into its two
terms, per conductor level (KLayout region area/perimeter × the shipped
coefficients — the same arithmetic `_compute_parasitics` performs):

| Level | Area (µm²) | Perimeter (µm) | `C_area` (fF) | `C_perim` (fF) |
|---|---|---|---|---|
| li1 | 1 931.89 | 18 176.5 | 71.46 | 739.78 |
| met1 | 1 826.94 | 17 085.2 | 47.10 | 693.15 |
| met2 | 892.00 | 12 404.2 | 15.61 | 468.38 |
| met3 | 238.53 | 1 636.2 | 2.95 | 67.07 |
| met4 | 107.31 | 723.0 | 0.90 | 26.52 |
| **Total (metals)** | | | **138.02** | **1 994.90** |

**93.5% of the metal ground capacitance this model reports is the
isolated-edge, fringe-to-substrate term** — every edge of every wire charged
as though nothing were beside it, above it, or below it **[REPO-RUN]**. That
is the single most important fact about today's baseline: it is, in effect, a
perimeter-fringe-to-substrate model with a small area correction, not a
"lumped RC" model in the sense a PEX flow would mean.

Second measurement, same block: the **crossover** (plate-overlap) area
between adjacent conductor levels, and what the two models say about it.
`Region &` between merged levels, with the corresponding via layer's
footprint reported separately since a via necessarily makes the two levels the
*same* net there:

| Pair | Overlap (µm²) | …minus via footprint | Charged to **ground** today | `defaultoverlap` reference, **net-to-net** | Ratio |
|---|---|---|---|---|---|
| li1 ^ met1 | 811.24 | 640.24 | 50.92 fF | 92.64 fF | 1.82× |
| met1 ^ met2 | 358.31 | 323.03 | 15.51 fF | 47.96 fF | 3.09× |
| met2 ^ met3 | 54.42 | 48.70 | 1.63 fF | 4.69 fF | 2.89× |
| met3 ^ met4 | 12.27 | 11.15 | 0.26 fF | 1.03 fF | 4.04× |
| **Total** | **1 236.24** | **1 023.12** | **68.31 fF** | **146.33 fF** | **2.14×** |

Read honestly: the overlap figures are a **net-agnostic upper bound** —
determining the *inter-net* share requires the connectivity the
implementation would supply (`polygons_of_net` per level per net, which
`_compute_parasitics` already walks). Subtracting the via footprints is a
crude floor, not a correction. Even so, on a real routed block ~1 236 µm² of
conductor sits directly over other conductor, today's model charges that area
**68.3 fF to ground**, and the PDK's own coefficients for that same geometry
say **146.3 fF between nets** — a 2.1× magnitude error on that term *and*
complete misattribution of it.

The same measurement on an isolated standard cell (`sky130_fd_sc_hd__dfxtp_2`)
gives li1 ^ met1 overlap of 3.803 µm² → 0.239 fF charged to ground vs 0.434 fF
reference, i.e. ~2% of that cell's 10.88 fF total **[REPO-RUN]**. This is the
"where does each stage earn its accuracy" answer in one comparison:
**coupling is a rounding error inside an isolated standard cell and a
first-order effect on a routed block.** Any measurement plan that grades a
coupling increment only on standard cells will under-report its value.

Third observation, on resistance **[REPO-RUN] + [PROPOSAL]**: `gcd`'s
`RESET_B|rst_n` net reports **90 944.5 Ω** across 200 terminal legs. The
square estimator is *series-only by construction* — one equivalent rectangle
per layer per net, `squares = L/W` — so a 200-sink net whose stubs are
physically in **parallel** is modelled as one long serial wire. The docstring
already documents this bias as "conservatively high" for L-shaped or
fragmented nets (`extract.py:4034-4037`), which is accurate for a two-terminal
L-bend; the measurement above suggests that at block scale, with hundreds of
branches, the bias is not a small conservatism. This is Stage 3's motivating
number, and is flagged here as an observation worth its own check rather than
asserted as a defect.

## 2. External SOTA survey

**2.1 Three method classes, and the axis they trade on.** Industrial
parasitic extraction is conventionally split into (a) **rule-based /
pattern-matched** extraction, which classifies local geometry into
pre-characterised patterns and looks up coefficients; (b) **quasi-3D /
hybrid** extraction, which decomposes a net into 2D cross-sections plus 3D
correction terms; and (c) **field solve**, which solves the governing integral
or differential equation on the actual geometry. The axis is not simply
"accuracy vs. runtime" — it is **accuracy vs. runtime vs. coverage**: a
pattern-matched extractor is fast and accurate *on the patterns in its
library* and has unbounded error outside it, while a field solver's error is
bounded by discretisation regardless of geometry **[LIT]**, general framing
across the interconnect-extraction literature.

**2.2 Rule-based capacitance extraction.** The closed-form basis is old and
well-established: Yuan & Trick's 2D formula for a conductor over a plane
(IEEE EDL, 1982) and Sakurai & Tamaru's simple 2D/3D capacitance formulas
(IEEE TED, 1983) **[LIT]**. Production rule decks generalise this into
multi-layer pattern tables — Chern et al., "Multilevel metal capacitance
models for CAD design synthesis systems" (IEEE EDL, 1992) and Arora et al.,
"Modeling and extraction of interconnect capacitances for multilayer VLSI
circuits" (IEEE TCAD, 1996) are the canonical descriptions, and the tables
themselves are normally *generated by a 2D/3D field solver offline* and then
applied at extraction time **[LIT]**. Two properties matter here:

- The **coefficient families** such a deck needs are exactly the five magic
  publishes (§1.4): area-to-plane, edge-fringe-to-plane, lateral sidewall,
  plate overlap, and edge-to-lower-plane fringe. magic's own extractor
  (Scott & Ousterhout, "Magic's circuit extractor," DAC 1984 / IEEE Design &
  Test 1986 — **[LIT]**) is precisely a rule-based extractor over that
  coefficient set, with **fringe shielding**: when a neighbour is close, part
  of the edge's fringe charge is moved from the substrate to the neighbour
  rather than counted twice.
- Reported accuracy for a well-characterised pattern deck against a 3D field
  solver is typically in the few-percent range on in-library geometry, with
  the honest caveat that "in-library" is doing the work **[LIT]**.

**2.3 Field-solver classes.** Three families, all headless-capable:

- **Boundary-element / Method of Moments (BEM/MoM).** Discretise conductor
  *surfaces*, fill a dense potential-coefficient matrix, solve for the
  capacitance matrix. Nabors & White's FastCap (IEEE TCAD, 1991) is the
  reference implementation, accelerated by the fast multipole method
  (Greengard & Rokhlin, J. Comput. Phys., 1987) to near-linear cost in panel
  count **[LIT]**. This is exactly Epic #701's Phase-1 direction, and its
  attraction for extraction is that only conductor surfaces need meshing.
- **Finite element (FEM).** Discretise the *volume*; naturally handles
  inhomogeneous dielectric stacks and, in a driven formulation, full-wave
  behaviour. This is the geode-fem direction #103 surveyed, including its
  quasi-static/DC-extrapolation mode **[REPO]** (spike §3).
- **Floating random walk (FRW).** Estimate each conductor's charge by Monte
  Carlo walks on Green's-function transition domains — Le Coz & Iverson, "A
  stochastic algorithm for high speed capacitance extraction in integrated
  circuits" (Solid-State Electronics, 1992), the basis of the QuickCap class
  of tools **[LIT]**. Its distinguishing properties are that it is
  **meshless** and gives a *per-net error bar* that shrinks with sample
  count, so accuracy is a tunable runtime knob rather than a mesh-refinement
  study. Worth naming explicitly here because it is the field-solver family
  best suited to "spot-check one critical net to 1%" — the exact use case
  Stage 4 wants — and it is not on #701's phase list.

**2.4 Resistance.** The progression is well-trodden **[LIT]**:

1. **Square counting** on an assumed simple shape (today's model).
2. **Geometry decomposition**: cut the net's conductor into rectangles and
   trapezoids, sum series/parallel resistance, apply corner corrections (the
   classical ~0.5-square-per-right-angle-corner rule) and add per-via/contact
   resistance from the PDK's own via resistance values.
3. **Node-based mesh solve**: mesh the conductor, build a resistive network
   between terminal nodes, and solve (a Laplace/finite-difference solve on the
   conductor). This is what a signoff extractor does for critical nets, and
   what magic's `extresist` does in the open flow.

Levels 2 and 3 differ from level 1 in kind, not degree: they are the only
levels that produce a *topology* (where resistance sits relative to
capacitance), which is the input a distributed model needs.

**2.5 Network reduction — how much topology a re-sim actually needs.** Given
per-segment R and C, the reduction choice is a distinct, well-studied
decision **[LIT]**:

- **Lumped C only** (no R): the classic gate-level load model; ignores wire
  delay entirely.
- **Single lumped R + C** (today's star, and the classical Γ/L section):
  Elmore delay at the far end ≈ `R·C`, whereas a genuinely distributed line's
  Elmore delay is `R·C/2` — a **2× overstatement** of the wire's own delay
  contribution (Elmore, J. Appl. Phys., 1948; Rubinstein, Penfield & Horowitz,
  "Signal delay in RC tree networks," IEEE TCAD, 1983).
- **Π / Π3 models**: O'Brien & Savarino, "Modeling the driving-point
  characteristic of resistive interconnect for accurate delay estimation"
  (ICCAD, 1989) — three moments of the driving-point admittance, the standard
  compromise a timer consumes.
- **Distributed RC ladder** (per-segment): the reference network a PEX flow
  emits (DSPF/SPEF), from which any of the above can be derived.
- **Model-order reduction**: AWE (Pillage & Rohrer, IEEE TCAD, 1990) and
  PRIMA (Odabasioglu, Celik & Pileggi, IEEE TCAD, 1998) reduce a large,
  passive RC(L) network to a small guaranteed-passive macromodel — the
  mechanism that makes full-block extracted netlists simulable at all.

The practical consequence for this roadmap: **a distributed extraction and a
reduced model are separable deliverables.** Extract the ladder once; choose
the reduction per consumer.

**2.6 How coupling capacitance is consumed, and why grounding it is wrong.**
A timer cannot treat a coupling capacitor as a grounded capacitor without
choosing a **Miller multiplier**: 0 if the aggressor is quiet and switching
with the victim, up to ~2 if it switches against it (Dartu & Pileggi,
"Calculating worst-case gate delays due to dominant capacitance coupling,"
DAC, 1997 — **[LIT]**). Grounding coupling caps at 1× is neither an upper nor
a lower bound on delay, and it makes crosstalk noise *identically zero*. This
is the precise sense in which today's `model.coupling: "not modelled"` is a
fidelity ceiling rather than a tolerance: a re-sim of a `--parasitics`
netlist cannot exhibit crosstalk at all, however severe the layout's coupling
is.

**2.7 Inductance.** Partial-element formulations (Ruehli, "Inductance
calculations in a complex integrated circuit environment," IBM J. Res. Dev.,
1972; "Equivalent circuit models for three-dimensional multiconductor
systems," IEEE Trans. MTT, 1974 — the PEEC method) and FastHenry (Kamon, Tsuk
& White, IEEE Trans. MTT, 1994) are the reference approaches **[LIT]**;
analytical RLC interconnect delay models (Kahng & Muddu, IEEE TCAD, 1997;
Ismail & Friedman's work on inductance effects in on-chip interconnect) set
the conditions under which L matters at all — long, wide, fast-edge nets:
clock spines, supply grids, RF passives **[LIT]**. For the digital and
low-frequency analog nets this repo's canaries are built from, RC extraction
without L is standard signoff practice, which is why inductance sits **last**
in §3 and is scoped as #701 Phase 2, not as a gap in the RC path.

**2.8 What "signoff-grade" means operationally.** Not "uses a field solver."
The industrial pattern is: a **fast rule-based/pattern-matched extractor for
the whole design**, its coefficient tables **generated and periodically
re-validated by a field solver**, plus **selective field solve on critical
nets**, with a documented accuracy target (commonly a few percent on total
net capacitance, tighter on nets that bind a spec) **[LIT]**. That structure
is directly transferable here and is the shape §3 adopts: Stages 2–3 build the
fast path, Stage 4 is the oracle that grades it and the selective solver for
the nets that matter — which is exactly how Epic #709 Phase 3 already frames
it (**[REPO]**, #709: "Feed MoM's (#701) R/L/C for a chosen critical net
directly into the re-sim").

## 3. The staged roadmap

Stages are ordered by (fidelity gained on the shipped path) ÷ (engineering
cost + new-dependency risk). Every stage names its own measurement; no stage
claims an improvement it cannot grade.

### Stage 0 — shipped: lumped ground C + star R

§1. Retained as the **default** at every later stage: the fast path is a
feature, not a stepping stone, and `--parasitics` must stay opt-in and
additive per #216's accepted interface decision **[REPO]**.

### Stage 1 — the measurement scaffolding (prerequisite, not this document's to build)

**This is Epic #709 Phase 0/1, and it gates every stage below.** A fidelity
claim needs (a) the same testbenches run against schematic and extracted
netlists, (b) a per-corner, per-spec-row delta report, and (c) the extraction
method + coefficient-table version pinned in the evidence record **[REPO]**,
#709's own acceptance criteria.

- **Accuracy gain:** none directly. It converts every later stage's claim from
  assertion to measurement.
- **Cost:** owned by #709; this roadmap adds no requirement to it.
- **How measured:** it *is* the measurement. §4 lists what already exists in
  this repo that Phase 0 can build on rather than invent.
- **Standing without it:** stages 2–4 can still be *unit*-graded (coefficient
  transcription, geometry cross-check against an independent extractor) but
  cannot make a re-sim fidelity claim. §5's increment is deliberately scoped
  so its primary measurement does not depend on Stage 1 landing first.

### Stage 2 — coupling capacitance

Split into three sub-stages because they have very different geometry costs
and very different payoffs, and because the split is what makes a *small first
increment* possible.

#### Stage 2a — vertical overlap (crossover) coupling + shielded-area correction

Per net pair, the area where net A's conductor on level *i* sits directly
under net B's conductor on level *i+1*: charge it `defaultoverlap(i, i+1)`
between A and B, and **remove** it from both nets' substrate-area term.

- **Accuracy gain:** on a routed block, converts 68.3 fF of misattributed
  ground charge into 146.3 fF of net-to-net charge (`gcd`, §1.5, upper bound)
  — a 2.1× magnitude correction on the crossover term, ~3% of total reported
  capacitance, plus the first non-zero crosstalk path the extracted netlist
  has ever had. On an isolated standard cell, ~2% of total C. **[REPO-RUN]**
- **Cost:** low, and the lowest of any stage below. Geometry is a Boolean
  `Region &` between two nets' already-registered per-level regions — no halo
  search, no neighbour-search structure, no new dependency, no second geometry
  backend. Coefficients come from the same public tech file, via the same
  curation-with-citation pattern (§1.4). The real work is the **pairwise
  attribution plumbing** (which nets interact, per level pair) and the JSON /
  SPICE contract extension — both of which Stage 2b then reuses unchanged.
- **How measured:** §5 (this is the proposed first increment; its full
  measurement plan is there).

#### Stage 2b — lateral sidewall coupling + fringe shielding

**Partial progress, issue #976 (Epic #709 Phase 2a, 2026-08-14):** the
geometry/coefficient half of this stage shipped, deliberately scoped down
from what this section describes in two ways this update flags rather than
silently narrows: (1) the pass only ever runs for a same-layer net pair
naming one of the caller's declared `klt extract --critical-net` nets, not
the whole layout unconditionally (the "medium cost" below is exactly why —
see that flag's docs for the "nets that matter" framing this scoping
borrows from Epic #709's own Phase 2 text); (2) it does **not** implement
the fringe-shielding deduction this section names — the coupling charge is
additive only, still not removed from the substrate perimeter term, because
the `defaultsidewall` second parameter's semantics (§1.4's flagged caution)
remain unresolved. A full-layout, fringe-shielded Stage 2b (the version this
section describes) is still open follow-on work.

Facing-edge length between neighbouring nets on the same level within a
lookback distance, charged `defaultsidewall`, with the corresponding fringe
charge **removed** from the substrate perimeter term (magic's fringe-shielding
behaviour, §2.2).

- **Accuracy gain:** the largest of any stage in this roadmap. It touches the
  **93.5%** of reported capacitance that is today's isolated-edge fringe term
  (§1.5). For a min-width met1 route of length *L* with neighbours at minimum
  spacing on both sides, `defaultsidewall`'s 44 aF/µm implies ≈ `0.088·L` fF
  of coupling, against `defaultperimeter`'s 40.57 aF/µm ≈ `0.081·L` fF
  currently charged to ground for those same two edges — i.e. on a crowded
  bus the *entire* dominant capacitance term is currently attributed to the
  wrong node **[PDK]** arithmetic over **[REPO]** width/spacing rules
  (`met1.width.1` = 0.14 µm, `met1.space.1` = 0.14 µm, `decks/sky130.py:272-288`).
- **Cost:** medium. Needs a spacing-aware neighbour search — feasible with
  KLayout's own edge/space-check primitives against per-net regions, but it is
  an O(interacting-pairs) pass with a real runtime budget on a 2 133-net
  block, and it needs the fringe-shielding model (how much substrate fringe to
  remove when a neighbour is present at distance *d*) read out of magic's
  tech-file semantics rather than guessed (§1.4's flagged caution).
- **How measured:** (i) per-level facing-edge length cross-checked against
  magic's own `ext2spice` coupling caps on the identical GDS (same coefficient
  source ⇒ any difference isolates the *geometry* algorithm — see §4);
  (ii) total-capacitance conservation check: the sum of a net's ground +
  coupling capacitance must not *increase* by more than the shielding model
  predicts, catching double-counting; (iii) a re-sim crosstalk bench (§4)
  where the victim-net disturbance goes from identically zero to a
  magic-corroborated non-zero value.

#### Stage 2c — edge-to-lower-plane fringe (`defaultsideoverlap`)

The remaining unread coefficient family: fringe from a conductor edge down
onto a specific lower plane rather than the substrate.

- **Accuracy gain:** small refinement of 2a/2b's attribution; completes the
  five-family coefficient set.
- **Cost:** low **once 2a/2b exist** (same geometry inputs, one more
  coefficient family and one more attribution term).
- **How measured:** folded into 2b's magic cross-check — with all five
  families implemented, this repo's extraction and magic's should agree on
  per-net total capacitance to within the geometry algorithms' difference,
  which is a much sharper bar than either stage alone can set.

### Stage 3 — distributed RC

#592's deferred "Option 2" **[REPO]**: decompose each net's conductor into
segments, build the RC ladder, and reduce it (§2.5) rather than collapsing to
one lumped R and one lumped C.

- **Accuracy gain:** two distinct corrections. (i) **Topology**: removes the
  systematic ~2× overstatement of a net's own Elmore delay inherent in a
  single-section model (§2.5), and replaces the `Device.trans`-centroid leg
  weighting with real routed-path resistance. (ii) **Magnitude**: replaces the
  series-only equivalent-rectangle square count, whose bias grows with branch
  count — `gcd`'s 200-terminal reset net reports 90.9 kΩ today (§1.5). On a
  standard cell the topology correction is worth well under 1% (`dfxtp_2`'s
  worst internal net: `R·C` ≈ 5.2 ps against a 206.5 ps `tcq` **[REPO-RUN]**);
  on a routed block with multi-hundred-ohm, multi-sink nets it is first-order.
- **Cost:** **high — the largest single increment in this roadmap.** It needs
  conductor decomposition into a segment graph (rectangles/trapezoids plus via
  nodes), per-segment R and C, capacitance apportioned onto ladder nodes, a
  reduction choice, and a much larger emitted netlist (`gcd` already emits
  26 593 resistors under the star model; a per-segment ladder is a multiple of
  that). It also raises a genuine contract question — whether the emitted
  SPICE stays a single flat netlist that `klt sim` consumes unmodified
  (#216's accepted requirement) at ladder scale **[REPO]**.
- **How measured:** (i) **DC sanity**: terminal-to-terminal resistance from
  the ladder must match a direct node-based solve on the same conductor to
  within a stated tolerance, and must be ≤ the series-only estimate for every
  multi-branch net (a strictly-checkable direction); (ii) cross-check against
  magic's `extresist` on the identical GDS (independent implementation, §4);
  (iii) re-sim delta on a canary spec row that is *resistance*-bound — this
  stage should be scheduled against a canary whose binding row is a delay or
  settling time, not a DC reference voltage, or the measurement will show
  nothing.
- **Sequencing note:** Stage 3 should follow Stage 2, not precede it. Its own
  measurements need capacitance to be attributed to the right nodes before a
  ladder's node placement means anything, and §1.5's decomposition says the
  capacitance error is currently the larger of the two.

### Stage 4 — quasi-static field solve (Epic #701's direction)

- **Engine choice is already spiked and is not re-opened here.** #103's
  recommendation stands: treat quasi-static/DC-extrapolation as a **solver
  mode** of whichever full-wave engine is adopted rather than a new external
  dependency, and do not adopt the unmaintained FastCap/FastHenry codebases
  given their unresolved licensing **[REPO]** (`em-field-sim-spike.md:156`).
  #718/#719 are the concrete first slice; §6 states the coordination.
- **Accuracy gain:** the only stage whose error is bounded by discretisation
  rather than by pattern coverage (§2.1). #103 already records the achievable
  band on real fixtures: geode-fem's quasi-static L₀ within **2.1%** of
  MoM-PEEC **[REPO]**.
- **Cost:** highest — a solver, a GDS→conductor-geometry→mesh pipeline that
  does not exist today (#103 §2), and a validation harness (#719).
- **Two distinct roles, and they should not be conflated:**
  1. **Oracle** (the near-term, higher-value role): grade Stages 2–3's fast
     path on a small benchmark of real net geometries. This is what makes
     "signoff-grade" meaningful per §2.8, and it is what Epic #701's own
     acceptance criterion asks for ("improving fidelity over lumped RC on at
     least one analog canary — measured, not asserted") **[REPO]**.
  2. **Selective solver** (per-net, on demand): #709 Phase 3's "feed MoM's
     R/L/C for a chosen critical net directly into the re-sim." Note §2.3's
     FRW family is arguably the better fit for exactly this
     one-net-to-1%-with-an-error-bar use case, and is currently absent from
     #701's phase list — flagged as a **[PROPOSAL]** for #701 to consider, not
     as a competing recommendation to #103's settled engine-mode decision.
- **How measured:** #719 already owns this (closed-form parallel-plate and
  coax oracles, convergence-under-refinement with a reported rate, optional
  external-solver cross-check). Nothing here changes those criteria.

### Summary

| Stage | Fidelity gain over Stage 0 | Cost | Primary measurement | Blocked on |
|---|---|---|---|---|
| **0** shipped | baseline | — | — | — |
| **1** measurement scaffolding | none (enables all) | owned by #709 | is the measurement | — |
| **2a** vertical overlap C | 2.1× on the crossover term; first non-zero coupling path | **low** | magic `ext2spice` cross-check + re-sim delta | — |
| **2b** lateral sidewall C + fringe shielding | **largest** — touches 93.5% of reported C | medium | magic cross-check + crosstalk bench | 2a's plumbing |
| **2c** side-overlap fringe | small refinement; completes the coefficient set | low (after 2a/2b) | per-net total-C agreement with magic | 2a, 2b |
| **3** distributed RC | removes ~2× single-section delay bias; fixes branch-count R bias | **high** | node-solve + `extresist` cross-check; R-bound canary row | 2 (ordering), #709 for re-sim claims |
| **4** quasi-static field solve | bounded error; the oracle for 2–3 | highest | #719's closed-form + convergence criteria | #718 |

## 4. Measurement harness — what already exists here

**Three oracles are available, with different independence properties.**

1. **magic `extract` / `ext2spice` on the identical GDS — oracle, not
   runtime.** This repo has already settled magic's role: "Oracle, not
   runtime… an independent, battle-tested implementation… but the wrong thing
   to make `klt`'s runtime dependency" (`lvs-extraction-spike.md:83`, `:95`)
   **[REPO]**. That framing applies verbatim here, and magic is a *better*
   oracle for Stage 2 than for LVS, for a subtle reason worth stating: because
   our coefficients and magic's come from the **same public tech file**, magic
   is **not** an independent check on coefficient *values* — but it is a
   genuinely independent check on the **geometry attribution algorithm**,
   which is exactly what Stages 2–3 implement. Any per-net disagreement
   isolates the algorithm rather than confounding it with coefficient drift.
   (Circularity caveat, stated so it is not forgotten: agreement with magic
   is evidence of correct attribution, never of correct coefficients. Only
   Stage 4 or silicon speaks to those.)
2. **The gallery cells' existing real ngspice PVT sweeps — the schematic
   reference.** `scripts/gallery_signals.py` already runs real,
   transistor-level, 15-corner sweeps on 7 standard cells from **vendored**
   PDK SPICE netlists, and the results are committed **[REPO]**. Concretely
   usable baselines (nominal corner `tt/1.800V/27C`):
   `sky130_fd_sc_hd__inv_1` → `tphl` **40.19 ps**, `tplh` **70.12 ps**;
   `sky130_fd_sc_hd__dfxtp_2` → `tcq` **206.52 ps**
   (`blocks/<slug>/output/layout.json`) **[REPO]**. Each cell's own GDS sits
   beside those results in the same directory, so "extract the layout of the
   exact cell whose schematic sim is already committed, re-run the identical
   testbench" needs **no new corpus** — it is the schematic-vs-extracted delta
   #709 Phase 0 wants, on data already in the tree.
3. **Stage 4 / #718–#719 as the field-solver oracle** for Stages 2–3's
   coefficients and, later, for whole-net capacitance on a benchmark of real
   net geometries.

**Sensitivity floor — is a re-sim delta even visible?** Yes, comfortably.
`gallery_signals.py`'s testbenches load each output with a fixed
`Cload = 5 fF` (`gallery_signals.py:388,399`) **[REPO]**, and `inv_1`'s
extracted output net `Y` carries **0.2397 fF** — a 4.8% load increase, which
in first order moves a 40.19 ps `tphl` by ≈ 2 ps and a 70.12 ps `tplh` by
≈ 3 ps **[REPO-RUN]** + **[PROPOSAL]**. Those are two to three orders of
magnitude above ngspice's numerical noise on a pinned timestep, so an A/B
re-sim resolves a *sub-percent* extraction change. The corollary: the harness
is sensitive enough that a delta *larger* than predicted is a defect signal,
which is what makes "state the expected magnitude in advance" a usable gate
rather than a formality.

**A/B protocol.**

- Same GDS, same deck, same testbench, same corner set, same seed/timestep;
  one extraction feature toggled. Diff the JSON responses' numeric fields
  directly, never eyeballed from logs.
- Report the **predicted** magnitude before running (from the geometry
  arithmetic, as in §1.5), then the measured one. Agreement is evidence; a
  large unexplained divergence is a bug report, not a result.
- `klt lvs` must stay `match` across every A/B pair: none of Stages 2–3
  changes device connectivity, so an LVS change is a defect, not a trade-off.
  (Note the star topology already renames terminal nodes onto leg nets by
  design — the invariant is that the *schematic-equivalent* view in
  `devices[]`/`nets[]` is untouched, exactly as documented **[REPO]**.)

**The trap this harness must not fall into.** A bigger
schematic-vs-extracted delta is **not** evidence of better extraction. Every
stage below Stage 4 can only be graded against an *independent reference*
(oracle 1 or 3), never against "the delta got larger" or "the number moved in
the direction I expected." #709's own discipline already says this from the
other side — "a delta that is implausibly small… is a flag that extraction
dropped something — surfaced, not hidden" **[REPO]** — and the symmetric
error is just as easy to make. Every stage in §3 therefore names a reference,
not a direction.

## 5. The first concrete increment

**Stage 2a: inter-net vertical overlap (crossover) coupling capacitance, plus
the shielded-area correction to the substrate term.**

**Proposed issue title:** `extract: model inter-net vertical overlap coupling
capacitance in --parasitics (Stage 2a)`

**Why this one first, stated against the alternative.** Stage 2b carries the
larger accuracy gain — it touches 93.5% of reported capacitance versus 2a's
~3% (§1.5) — and this document says so plainly rather than picking the
convenient item and calling it the biggest. 2a goes first because it is the
**only** coupling increment whose geometry is a plain Boolean intersection of
regions `_compute_parasitics` already walks: no halo search, no
fringe-shielding model, no tech-file semantics to resolve first (§1.4's
flagged `defaultsidewall` caution applies to 2b, not 2a). Yet it builds
*every* piece of shared machinery 2b then needs unchanged — pairwise net
attribution, the `coupled[]` JSON shape, two-terminal `C` cards between
signal nets, the `model.coupling` declaration flip, and the substrate-term
correction. It converts the highest-risk part of Stage 2 (contract shape and
attribution plumbing) into a small, independently-measurable change, and
leaves 2b as a pure geometry-and-coefficients follow-on.

**Scope.**

1. Extend the per-deck `PARASITICS` table with an overlap-coefficient family
   for each adjacent conductor-level pair, transcribed from each PDK's own
   public magic tech file with an inline per-value citation — the exact
   discipline the existing coefficients already follow, including a
   `metals_without_coefficient`-style gap report (#547) when a declared level
   pair has no curated coefficient, so a silent zero is impossible.
2. In `_compute_parasitics`, for each adjacent level pair and each pair of
   *distinct* nets, intersect the two nets' `polygons_of_net` regions on the
   two levels; the resulting area × the pair's overlap coefficient is a
   coupling capacitance between those nets. Same-net overlap (a via stack) is
   excluded by construction, since attribution is per net pair.
3. Subtract that overlapped area from **both** nets' substrate-area term on
   their respective levels, so charge is moved rather than duplicated.
4. Emit additively:
   - JSON: `parasitics.nets[].coupled[]` (`{"net", "capacitance_ff",
     "levels"}`), plus top-level `cc_count` and
     `total_coupling_capacitance_ff`. **`c_count` keeps its documented
     meaning** ("always one per `nets[]` entry" — ground capacitors only), so
     the existing invariant is preserved rather than silently redefined.
   - SPICE: one two-terminal `C` card between the two nets' hub nodes per
     coupled pair, named by the same sanitization rule the existing cards use
     (#312).
   - `parasitics.model.coupling` changes from `"not modelled"` to a
     declaration of exactly what *is* modelled (vertical overlap only;
     lateral sidewall still absent). This is the mechanism #728 built for
     precisely this moment **[REPO]** — a consumer asserting
     `model.coupling != "not modelled"` starts passing, by design.
5. Per `docs/json-contract.md`, this needs **no `schema_version` bump**: new
   fields are additive and no documented field is renamed or retyped; the
   changed `model.coupling` *value* is an additive behaviour change of the
   kind that file explicitly places in `CHANGELOG.md` rather than a version
   bump **[REPO]**. A CHANGELOG entry is therefore mandatory, not optional.
6. Document in `docs/cli/extract.md`: the new fields, the corrected
   substrate-area semantics, and the demotion of "net-to-net coupling is out
   of scope" to "lateral sidewall coupling is still out of scope."

**Acceptance criteria (all measurable, none asserted).**

- **Coefficient provenance:** a test asserts every new coefficient matches
  the value in the cited public tech-file entry, in the same style as the
  existing `test_parasitics_coefficients_sourced_from_pdk_tech`
  (`tests/test_extract.py:6026`) **[REPO]**.
- **Charge conservation:** for every net on every corpus layout,
  `ground_C_after + Σ coupled_C ≥ ground_C_before` **and** the increase is
  bounded by `overlap_area × (overlap_coef − Σ areacaps)` — a closed-form
  bound from the coefficients, so double-counting fails the test rather than
  requiring review to notice.
- **Predicted magnitude, verified:** on `tests/corpus/place_and_route/gcd.gds.gz`
  the total coupling capacitance must land **below** this document's measured
  net-agnostic upper bound of 146.33 fF and above the via-footprint-excluded
  floor of ~121 fF-equivalent, with the inter-net share reported. The bound
  comes from §1.5's measurement on the same file, so the test has a real
  number to check against on day one.

  **Measured, as shipped (#760) [REPO-RUN]:** the ceiling held; the floor did
  not, exactly as §1.5's "a crude floor, not a correction" caveat warned.
  Supplying the connectivity decomposes that same 146.328 fF as **77.611
  fF-equivalent same-net** (a net's own via stacks *and* its own li1 routed
  under its own met1 over via-free stretches — much more than the via
  footprint alone can see, which is why ~121 fF was too high), **0.324
  fF-equivalent between distinct nets sharing one layout label** (`gcd` has
  105 un-strapped `VGND` islands and 88 `VPWR` ones; they collapse to one
  node downstream, so this is left on ground rather than emitted as a
  self-loop), and **68.393 fF-equivalent genuinely inter-net** — the shipped
  figure, 46.7% of the net-agnostic bound. The three shares sum to 146.328 fF
  exactly. `tests/test_extract.py::test_gcd_parasitics_coupling_magnitude`
  therefore asserts the rigorous ceiling and the measured band, not the
  provisional floor.
- **Independent geometry cross-check:** per-net coupling capacitance compared
  against magic `ext2spice`'s own coupling caps on the identical GDS, with
  the agreement tolerance stated and the circularity caveat from §4 recorded
  alongside it. Oracle-only — magic does not become a dependency.
- **Re-sim delta, on data already in the tree:** re-run
  `scripts/gallery_signals.py`'s existing `inv_1` / `nand2_2` / `buf_4` /
  `dfxtp_2` testbenches against the extracted netlists with and without the
  new term. Expected magnitude stated in advance from §4's sensitivity
  arithmetic (sub-percent on `tphl`/`tplh`/`tcq`, since the crossover term is
  ~2% of an isolated cell's total C); a materially larger shift is a defect
  signal, not a success.
- **Crosstalk capability check — the sharpest single measurement:** a
  two-net bench on the extracted `gcd` netlist in which an aggressor net
  slews and a victim net is observed. Today's output makes this disturbance
  **identically zero by construction** (no element couples the two nets), so
  any non-zero, magic-corroborated victim response is an unambiguous fidelity
  improvement that needs no tolerance argument. This is the criterion that
  most directly discharges Epic #709's "improvement over lumped RC is
  measured, not asserted."
- **No regression in the schematic-equivalent view:** `devices[]`/`nets[]`
  byte-identical, `klt lvs` still `match`, and `--parasitics`-off output
  still byte-identical to today's (the existing
  `test_parasitics_off_writes_byte_identical_netlist` must keep passing
  unchanged) **[REPO]**.

**Explicitly not in scope:** lateral sidewall coupling and fringe shielding
(Stage 2b), `defaultsideoverlap` (2c), distributed RC (Stage 3), any
inductance, any field solver, any corner/temperature axis on the
coefficients, and any change to `--parasitics`'s default-off, fixed-model
posture (#216) **[REPO]**.

## 6. Coordination with #701 / #709 / #718 / #719 / #103

- **#718 / #719 are Stage 4's first slice, not a parallel effort.** #718
  defines `klt mom` and implements quasi-static capacitance extraction; #719
  validates it against closed forms with convergence-under-refinement. This
  roadmap adds **no** requirement to either and contradicts neither: it
  sequences the stages *before* them and names Stage 4's two roles (oracle,
  selective solver) so that when #718 lands there is a defined consumer for
  it. #719's stated dependency on #718 is unaffected.
- **#701's epic-level acceptance criterion** ("the extracted parasitics feed
  `klt`'s post-layout simulation path, improving fidelity over lumped RC on
  at least one analog canary — measured, not asserted") is the criterion §5's
  increment is designed to make dischargeable **earlier and more cheaply**
  than a field solver can: the crosstalk-capability check discharges
  "measured, not asserted" against a zero baseline.
- **#709's phases map onto this roadmap directly**: its Phase 0/1 is Stage 1
  here, its Phase 2 ("coupling + distributed RC") is Stages 2–3, its Phase 3
  is Stage 4's selective-solver role. Stage 2 is split into 2a/2b/2c here
  precisely so #709 Phase 2 has a small first slice rather than one large
  step.
- **#103 is cited, not re-run.** Engine choice for the field-solve stage is
  settled there; §3's Stage 4 restates its recommendation and adds exactly one
  new **[PROPOSAL]** for #701's consideration (the FRW/QuickCap family for the
  selective single-net role, §2.3), flagged as a suggestion rather than a
  revision of a closed spike.
- **No conflict with `docs/cli/extract.md`'s current contract.** Everything
  proposed is additive under `docs/json-contract.md`; the one field whose
  *value* changes (`model.coupling`) was built to change for exactly this
  reason (#728).

## 7. Non-goals and open questions

**Non-goals of this document:** picking a field-solver engine (settled, #103);
re-opening the `--parasitics`-inside-`klt extract` interface (settled, #216);
proposing silicon calibration (an explicit non-goal of #216 and unreachable
without measured parts, per `docs/design-evidence-tiers.md`'s T3 rung);
vendoring any proprietary PDK data (every coefficient named here comes from a
public open-PDK source file).

**Open questions a later stage must answer, recorded rather than guessed:**

1. `defaultsidewall`'s second parameter — exact magic semantics, needed
   before Stage 2b transcribes it (§1.4).
2. Netlist scale at Stage 3: does a per-segment ladder keep the emitted SPICE
   a single flat `.SUBCKT` body that `klt sim` consumes unmodified (#216's
   requirement) when `gcd` already emits 26 593 resistors under the star
   model, or does Stage 3 require a reduction step (§2.5) *in* the extractor
   rather than as a downstream choice?
3. Is `_n_squares`' series-only bias at high branch count (§1.5, 90.9 kΩ on a
   200-terminal net) within the documented "conservatively high" intent, or a
   defect worth its own issue ahead of Stage 3? This document flags it; it
   does not adjudicate it.
4. Which canary spec row is the right grading vehicle for Stage 3? It must be
   resistance/delay-bound, and neither current pre-layout canary
   (`gf180-bandgap`, `sky130-bandgap`, both DC-reference-bound and both still
   pre-layout **[REPO]**) qualifies today.

## References

- Yuan, C. P., Trick, T. N. "A simple formula for the estimation of the
  capacitance of two-dimensional interconnects in VLSI circuits." IEEE
  Electron Device Letters, 1982.
- Sakurai, T., Tamaru, K. "Simple Formulas for Two- and Three-Dimensional
  Capacitances." IEEE Trans. Electron Devices, 1983.
- Scott, W. S., Ousterhout, J. K. "Magic's Circuit Extractor." DAC, 1984 /
  IEEE Design & Test, 1986.
- Chern, J.-H. et al. "Multilevel metal capacitance models for CAD design
  synthesis systems." IEEE Electron Device Letters, 1992.
- Arora, N. D., Raol, K. V., Schumann, R., Richardson, L. M. "Modeling and
  extraction of interconnect capacitances for multilayer VLSI circuits." IEEE
  TCAD, 1996.
- Nabors, K., White, J. "FastCap: A Multipole Accelerated 3-D Capacitance
  Extraction Program." IEEE TCAD, 1991.
- Greengard, L., Rokhlin, V. "A fast algorithm for particle simulations."
  Journal of Computational Physics, 1987.
- Le Coz, Y. L., Iverson, R. B. "A stochastic algorithm for high speed
  capacitance extraction in integrated circuits." Solid-State Electronics,
  1992.
- Ruehli, A. E. "Inductance calculations in a complex integrated circuit
  environment." IBM Journal of Research and Development, 1972.
- Ruehli, A. E. "Equivalent Circuit Models for Three-Dimensional
  Multiconductor Systems." IEEE Trans. Microwave Theory and Techniques, 1974.
- Kamon, M., Tsuk, M. J., White, J. K. "FASTHENRY: A Multipole-Accelerated
  3-D Inductance Extraction Program." IEEE Trans. Microwave Theory and
  Techniques, 1994.
- Elmore, W. C. "The Transient Response of Damped Linear Networks with
  Particular Regard to Wideband Amplifiers." Journal of Applied Physics, 1948.
- Rubinstein, J., Penfield, P., Horowitz, M. A. "Signal Delay in RC Tree
  Networks." IEEE TCAD, 1983.
- O'Brien, P. R., Savarino, T. L. "Modeling the driving-point characteristic
  of resistive interconnect for accurate delay estimation." ICCAD, 1989.
- Pillage, L. T., Rohrer, R. A. "Asymptotic Waveform Evaluation for Timing
  Analysis." IEEE TCAD, 1990.
- Odabasioglu, A., Celik, M., Pileggi, L. T. "PRIMA: Passive Reduced-Order
  Interconnect Macromodeling Algorithm." IEEE TCAD, 1998.
- Dartu, F., Pileggi, L. T. "Calculating worst-case gate delays due to
  dominant capacitance coupling." DAC, 1997.
- Kahng, A. B., Muddu, S. "An analytical delay model for RLC interconnects."
  IEEE TCAD, 1997.
- `docs/cli/extract.md` — the shipped `--parasitics` contract (§1's ground
  truth).
- `docs/design/lvs-extraction-spike.md` → Addendum (#216) — the accepted
  interface decision this roadmap extends rather than re-opens.
- `docs/design/em-field-sim-spike.md` (#103) — the closed field-solver engine
  survey Stage 4 cites instead of re-deriving.
- `docs/design-evidence-tiers.md` — the T1 post-layout item this roadmap's
  stages unblock.
- `docs/json-contract.md` — the additive-change rules §5 tests its contract
  extension against.
- Epics #701 (MoM) and #709 (PEX-aware sim), with #718/#719 as Stage 4's
  first slice.
- sky130's own public magic technology file (`libs.tech/magic/sky130A.tech`,
  as published in `fossi-foundation/open-pdks`) — the source of every `[PDK]`
  coefficient quoted above, and of the coefficients `decks/sky130.py` already
  ships.
