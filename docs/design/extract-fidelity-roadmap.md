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

> **Superseded in part (#2359).** The `n_squares` row above describes the
> whole-*net* fit as it stood when this roadmap was written. That fit is now
> applied **per connected fragment** and summed, and a fragment whose own
> terminals imply a current direction across a near-rectangular shape is
> measured as `L²/A` along that direction instead (the smaller of the two is
> taken). The rest of the table is unchanged. See `docs/cli/extract.md` →
> "total series R" for the current model.
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

> **Partly addressed since (#2359).** The "one equivalent rectangle per layer
> per net" half of this observation no longer holds: squares are counted per
> connected fragment and summed, so a net's disjoint stubs are no longer
> merged into one long serial wire, and a short/wide fragment crossed along
> its short axis is measured in that direction. The **series-only** half
> still stands — fragments combine in series, never in parallel — so the
> `RESET_B|rst_n` observation above remains Stage 3's motivating case, just
> with a smaller starting number.

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

**Partial progress, issue #977 (Epic #709 Phase 2b, 2026-08-14):** a scoped
first increment shipped, deliberately narrowed from what this section
describes in the same shape #976 narrowed Stage 2b: `klt extract
--distributed-rc` (requires `--critical-net`) replaces the star with a
multi-segment ladder only for a caller-declared `--critical-net` net (not
every net in the layout unconditionally), and only using each net's already-
computed total R/C plus its existing device-terminal positions as the
segment-ordering signal — not the conductor-decomposition-into-rectangles/
trapezoids-plus-via-nodes segment graph this section describes below. That
keeps the increment's cost down to "redistribute an existing lumped total
along an existing terminal-position proxy" rather than the "high — the
largest single increment in this roadmap" full geometry decomposition, at
the cost of a coarser segment boundary (terminal position, not real routed-
path geometry) than a true per-segment ladder would use. A full geometry-
decomposed Stage 3 (the version this section describes) is still open
follow-on work; #977's own canary (`tests/test_pex.py::
test_run_pex_distributed_rc_canary`) demonstrates the topology correction's
real, measurable effect on a genuinely high-impedance routed net regardless.

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

#### Stage 3 design spike: terminal-aware resistance graph (issues #2391, #2458)

**Status: design and fixture specification only (2026-10-06).** No solver is
implemented by this section and no extraction number changes. Verified against
`origin/main` @ `d28f13f0265cd5c397f136821ca58718d34402c5`: the helpers named
below exist in `src/klayout_tools/extract_parasitics.py`
(`_fragment_port_points_um`, `_region_n_squares`, `_net_port_regions`,
`_compute_parasitics`, `_distributed_rc_segments`), and the ladder
total-conservation tests (`test_distributed_rc_replaces_star_with_two_segment_ladder`,
`test_distributed_rc_order_and_segments_three_point_conservation`) and fragment
tests (`test_region_n_squares_*`) exist in `tests/test_extract.py`. The
existing resistor-network infrastructure this design reuses is also verified
at that sha: `solve_ir_drop` in `src/klayout_tools/ir_solver.py` (tests in
`tests/test_ir_solver.py` and `tests/test_power_ir_cross_check.py`) and
`_decompose_manhattan_polygon`, `_model_mesh_polygon`, `_rails_under_via` and
`_build_island_network` in `src/klayout_tools/power.py`. Re-check the sha
before starting any increment below. #2391 covers within-role
branches; #2458 is the cross-level (via-stitched) consumer and keeps its own
implementation/regression scope.

##### 3.D1 Decision record

- **Legacy limitation.** `_region_n_squares` sums per-fragment squares and
  `_compute_parasitics` accumulates the per-role result into one
  `resistance_ohm` per net. That is a *series* combination. It is a fair model
  of a net traversed fragment after fragment, and wrong for fragments that
  carry current side by side (per-device supply taps on one rail; two levels
  strapped together). The scalar grows with fragment count while the real
  resistance between any fixed pair of points does not. The bandgap `VSS`
  `metal0` observation (619 fragments, 25037.04 ohm after #2359) is retained
  as **historical motivation only**; it was not reproduced in this curation.
  Before any future measurement, establish that
  `blocks/sky130-bandgap/output/bandgap_core_routed.gds` is available and
  record its SHA-256 alongside the number.
- **Decision.** Parallel resistance is modelled by an **opt-in,
  geometry-derived, terminal-aware resistor graph** that covers within-role
  branches (#2391) and via-stitched levels (#2458) with one model. Effective
  resistance is defined only for *specified* source/sink terminals and
  boundary conditions (3.D2). N taps are not assumed to be N branches between
  common endpoints; the graph decides.
- **Rejected: supply-name heuristics** (treat `VSS`/`VDD`/`GND` as parallel).
  Names are not geometry: a supply net can contain a genuine series run, a
  signal net can contain parallel straps, and naming conventions differ per
  PDK and per designer. It also produces a different model for the same
  copper depending on a label.
- **Rejected: reciprocal fragment sums** (`1 / sum(1/R_i)` over fragments).
  It is right only when every fragment connects the same two equipotential
  endpoints. Fixtures F3, F5 and F8 break it: a shared series stub, a
  single-landing strap pair and a three-terminal star all contain fragments
  that are not between common endpoints. It also overstates the benefit for
  fragments that are tap stubs (dead ends carry no current).
- **Rejected: overlap-only classification** (two levels overlapping implies a
  strap). Overlap without a via/contact is no conduction path (F6); a strap
  connected at a single landing is in series, not parallel (F5). Connectivity
  must come from cut geometry, not from shadow area.
- **Separation of concerns.** This work is DC resistance only. Capacitance
  attribution to graph nodes and netlist integration of the full graph
  remain the rest of Stage 3 and are not prerequisites for the DC increments.

##### 3.D2 Model definition

**Graph construction (per net, per conductor role).**

1. *Fragments.* Reuse the connected-fragment split `_region_n_squares` already
   performs on the net's merged region for each metal/poly role.
2. *Port geometry.* Reuse `_net_port_regions` (this net's contact/via
   regions) and `_fragment_port_points_um` (contact/via landings plus
   device-body cut edges) to find where current can enter or leave a
   fragment.
3. *Segmentation.* Reuse `power._decompose_manhattan_polygon`, which cuts
   each merged Manhattan polygon on the grid formed by extending every vertex
   x/y across it. Two cells that share a boundary always share the whole
   side. Non-Manhattan input, degenerate input, or input over the cell cap
   (`MAX_DECOMPOSITION_CELLS = 4096`) returns a reason instead of guessing.
   The one extension it needs is an optional set of extra cut coordinates,
   used to cut at every port position (contact/via landing edges,
   device-body cut edges, terminal patch edges). With no extra cuts it
   produces today's grid, so `klt power` is unaffected. A cell is one
   piece. Junctions need no special handling because the vertex grid
   already places a cell boundary wherever three or more cells meet.
4. *Nodes.* One node per piece (lumped at the piece centre) plus one node per
   terminal/port. Node ids are `"<role>:<k>"` with `k` the rank of the piece in
   the deterministic order below; a port node is `"port:<k>"`.
5. *In-plane edges.* Two pieces sharing an interface of length `w` get an edge
   of `R = sheet_res * (d1/(2*w1) + d2/(2*w2))` with `d_i` the piece extent
   normal to the interface and `w_i` its width. A port node on a piece's
   boundary (a terminal, or a strip end joined to a via or an ideal node)
   connects to that piece by the **half-piece edge**
   `R = sheet_res * d/(2*w)`, which runs from the piece centre to the port.
   This is the same centre-to-side edge `power._model_mesh_polygon` already
   uses (`sheet_r * half_um / cross_um`). Interior edges contribute two
   halves per piece and each end port contributes one half, so a uniform
   strip of length `L` and width `W` cut into pieces `d_1..d_n` sums to
   `sheet_res * (d_1/2 + (d_1+d_2)/2 + ... + (d_{n-1}+d_n)/2 + d_n/2) / W =
   sheet_res * L / W`. That holds exactly however finely it is cut, and it
   holds for a single piece (`n = 1`: `d/2 + d/2 = L`). At bends and
   T-junctions inside one fragment this is the usual first-order
   approximation (current crowding is not modelled), and fixtures with such
   junctions carry a looser tolerance (3.D4).
6. *Via/contact edges.* Each via/contact landing joins the piece(s) under it
   on role *k* to the piece(s) above on role *k+1* by an edge of
   `R = r_cut / n_cut` where `n_cut = max(1, round(landing_area / cut_area))`
   and `r_cut` is the deck's per-cut resistance. `r_cut = 0` (explicitly
   curated, not merely absent) merges the two nodes (ideal via). Overlap
   without a landing creates no edge. The search for the pieces a landing
   touches reuses `power._rails_under_via`. That helper scopes the search to
   the merged polygon(s) the cut physically lands on and never searches
   net-wide (issue #2259), and the graph then picks the cells under the
   landing within that polygon. Two parts of power's via model are **not**
   reused, on purpose. Power snaps a via to the nearest endpoint of a
   box rail, which is its documented tap approximation; here the extra cuts
   in step 3 put a port at the landing's real position. Power also prices a
   via as one resistor per merged via shape; here it is per cut, because
   extraction decks give a per-cut resistance.
7. *Zero-resistance edges* (below `1e-9` ohm, or ideal vias/ties) are emitted
   with `resistance_ohm` exactly `0.0`. `solve_ir_drop` merges those
   endpoints with union-find into one supernode before it assembles the
   system, so they never enter the conductance matrix. The ideal nodes in
   the fixtures (3.D4) are expressed this way.

**Terminals and boundary conditions.** A *terminal* is an ideal equipotential
node: all pieces under one device-terminal port, pin, or caller-designated
patch are contracted into one node. Effective resistance between terminals
`s` and `t` solves `G v = i` with `v_t = 0`, unit current injected at `s`
(a Dirichlet/Neumann pair), every other terminal **floating** unless the
request says otherwise (a terminal may be declared `tied` to `s` or `t`; tying
is a boundary condition, not a topology change). `R_st = v_s`. For `T >= 3`
terminals the primary output is the full `T x T` terminal-pair matrix
(equivalently the Schur complement of `G` onto the terminals); a single
scalar per net exists only when exactly two terminals are specified. Fixture
F8 shows why: the scalar changes with the endpoints and the boundary
condition.

**Solver: reuse `ir_solver.solve_ir_drop`, do not add a second one.** The
formulation above is exactly what `solve_ir_drop(node_ids, edges, pads=...,
injections=...)` already solves. It takes `pads={t: 0.0}` and
`injections={s: 1.0}`, and then `R_st = voltages[s]`. Floating terminals are
simply nodes with no pad and no injection. A tied terminal is a `0.0` ohm edge,
which the solver merges. `solve_ir_drop` is geometry-free, pure Python and
already the repo's validated DC network solver: it has closed-form ladder,
parallel, bridge and lattice tests in `tests/test_ir_solver.py` and an ngspice
operating-point cross-check in `tests/test_power_ir_cross_check.py`. Writing
a second solver would go against the wrap-vs-rewrite rule in
`docs/ARCHITECTURE.md`. Its method is
Jacobi-preconditioned CG to a relative residual of `1e-12`. The achieved
residual is reported, and anything worse than `UNSOLVED_RESIDUAL = 1e-6` is
`not_converged`. Neither of the earlier reasons for a direct method holds up
against the code:

- *Determinism.* CG over a fixed node/edge input order performs the same
  floating-point operations every run, so the result is byte-identical
  run to run. The builder supplies that fixed order (see Determinism
  below).
- *Precision.* Every graph-level fixture in 3.D4 was run through
  `solve_ir_drop` at `d28f13f0`, with each strip cut into four pieces plus
  half-piece port edges. The values covered F1, F2 (N = 2, 4, 8), F2v
  (N = 3, 4), F3, F4v, F7b, the F8 matrix and tied F8. The worst relative
  error was `5e-15`, far inside the `1e-9` fixture tolerance. F9 and F8
  were then re-run with the per-component construction below (one
  reference per component, all references pinned in every solve). F9
  gave `R_BC = 100` (relative error `6e-16`) with A's entries `null`. F8
  gave 150, 250 and 200 ohm (worst `3e-15`). With an extra terminal-free
  strip added to F8, that strip alone reported `no_pad`.

If a later measured layout shows CG is too slow or too imprecise, the remedy
is to add a direct method *inside* `ir_solver.py` behind the same interface,
so `klt power` benefits too. A parallel solver in the extract module is not
the remedy.

**Terminal matrix: one reference terminal per connected component.** The
`T x T` matrix is assembled per connected component, not from a single
grounded terminal. A single global reference does not work.
`solve_ir_drop` reports `no_pad` for every component that has no pad and
leaves all of its voltages `None`. So once the terminals span more than one
component, grounding one terminal loses every pair in the other components.
For example, take terminal A isolated and B, C joined by 100 ohm, with
`pads={A: 0.0}`. Injecting at B or at C returns
`{'A': 0.0, 'B': None, 'C': None}`, and the valid `R_BC = 100` cannot be
recovered (fixture F9). The adapter does the following:

1. *Group the terminals.* One call `solve_ir_drop(nodes, edges, pads={},
   injections={})` returns `node_component`. With no pads, every component
   returns `no_pad` at once, without running CG. This partitions the `T`
   terminals into terminal-bearing components `c` of `T_c` terminals each.
   Components are numbered in first-seen order (see Determinism).
2. *Pick references.* In each terminal-bearing component, the terminal
   with the lowest id is its reference `r_c`. Every solve passes
   `pads={r_c: 0.0 for every c}`, so every terminal-bearing component is
   pinned in every solve. A component with no injection then solves to
   all zeros, and `no_pad` is left to mean only a component with no
   terminal at all (`floating_component`).
3. *Solve.* For each component and each non-reference terminal `i` in it,
   inject 1 A at `i` and read the voltages of that component's terminals.
   That gives the component's symmetric transfer matrix `Z` (`Z_ij = v_j`
   when injecting at `i`, and `Z` is 0 in any row or column of `r_c`).
   Then `R_{i,r_c} = Z_ii` and `R_ij = Z_ii + Z_jj - 2*Z_ij` for `i`, `j` in
   the same component.
4. *Cross-component entries.* When `i` and `j` are in different components,
   `R_ij` is JSON `null`, with diagnostic `terminals_disconnected`. No solve
   is spent on these entries.

This takes `sum_c (T_c - 1)` solves in total, which is at most `T - 1` and
equals `T - 1` only when all terminals share one component. A component
with exactly one terminal contributes only its diagonal entry (`0.0`) and
**no solves**. F9 takes one solve (component {B, C}) and F8 takes two. The
diagonal is always `0.0`.

**Degenerate cases.**

- *Disconnected terminals* (different components): `R_st = null` for
  that pair only. It is never `inf` or `0` in JSON. Pairs inside each
  component are still computed (F9). The net emits **one**
  `terminals_disconnected` diagnostic, which lists the terminal groups
  (one sorted list of terminal ids per terminal-bearing component, for
  example `[["A"], ["B", "C"]]`). The net's `status` stays `ok`, because
  every non-null entry is exact. The grouping comes from `node_component`
  ("Terminal matrix" step 1), not from a `no_pad` report: every terminal-bearing
  component is pinned in every solve.
- *Single-terminal components*: the terminal's diagonal is `0.0` and every
  off-diagonal entry in its row and column is `null` (covered by
  `terminals_disconnected`). Such a component costs no solve. Its pieces
  are a dead end, not a floating component.
- *Floating components with no terminal*: dropped, diagnostic
  `floating_component` (count and area). These are exactly the components
  `solve_ir_drop` reports as `no_pad` once every terminal-bearing component
  is pinned (F6). Dead-end stubs naturally carry no current and drop out of
  `R_st` without special-casing.
- *Fewer than two terminals*: no graph result, diagnostic `too_few_terminals`;
  legacy scalar remains.
- *Zero-resistance path* (terminals contracted into one node): `R_st = 0.0`
  exactly.
- *Zero-area / degenerate pieces*: dropped with a diagnostic.

**Determinism.** Pieces are ordered by `(role order from the deck, bbox.bottom,
bbox.left, bbox.top, bbox.right, area)` in integer dbu; ports by `(role,
bottom, left)`; edges by `(min(node_id), max(node_id), kind)` where kind is
`in_plane < via`. Ids derive from those ranks only, never from Python
iteration order of sets/dicts or klayout object identity. Nodes and edges are
passed to `solve_ir_drop` in id order, and the solver numbers supernodes and
components in first-seen order. So CG runs the same arithmetic in the same
order every time, and two runs on the same layout are byte-identical in JSON
(the repo's artifact determinism gate applies).

**Resource bounds.** Defaults: at most 5000 nodes and 20000 edges per net
before contraction, and at most 64 terminals, so at most
`sum_c (T_c - 1) <= 63` solves per net, plus the one grouping call, which
runs no CG.
Each polygon also keeps its own `MAX_DECOMPOSITION_CELLS` cap from step 3.
The runtime has no numpy/scipy dependency (`pyproject.toml` keeps numpy
test-only). The solve is `solve_ir_drop`'s pure-Python sparse CG over
adjacency lists, and its default iteration cap is `max(500, 10*n)`.
Exceeding a bound yields diagnostic `graph_too_large` (with the counts) and
the legacy scalar stands; the builder never truncates silently.

**Failure behaviour.** Geometry or coefficient problems never raise and never
change the legacy output. Each produces a diagnostic with a stable code and
`status: "unsupported"` for that net: `sheet_resistance_unknown`,
`via_resistance_unknown` (absent coefficient; an unknown coefficient is *not*
silently treated as ideal), `unsupported_geometry` (the step-3
decomposition returned a reason, e.g. a non-Manhattan polygon),
`graph_too_large` (also used when the step-3 cell cap is hit), and
`singular_system` (`solve_ir_drop` reports `not_converged` for a
terminal-bearing component; reported, not guessed). Only invalid CLI usage
is an `ExtractError`. The full diagnostic taxonomy is therefore: per-net
`status: "unsupported"` codes `sheet_resistance_unknown`,
`via_resistance_unknown`, `unsupported_geometry`, `graph_too_large` and
`singular_system`; informational codes with `status: "ok"`, namely
`terminals_disconnected` (terminal groups; cross-component entries `null`)
and `floating_component` (count and area); and `too_few_terminals`, which
yields no graph result and keeps the legacy scalar.

##### 3.D3 Mapping to existing code, JSON and the ladder

| Need | Existing helper | Role in the graph |
|---|---|---|
| Fragment split, squares | `_region_n_squares` | Fragment enumeration; legacy squares kept as the `legacy_resistance_ohm` cross-check. |
| Contact/via regions per net | `_net_port_regions` | Source of via/contact edges and per-cut counts. |
| Ports and device-body cut edges | `_fragment_port_points_um` | Terminal and segmentation cut positions. |
| Per-role sheet R, per-net orchestration | `_compute_parasitics` | Call site: build graph beside the existing accumulation; keep `base_r_ohm` untouched. |
| Ladder | `_distributed_rc_segments` | Unchanged consumer of the scalar `resistance_ohm`. |
| DC resistor-network solve | `ir_solver.solve_ir_drop` | **Reused as the solver** (3.D2 "Solver"). `pads={t: 0.0}` and `injections={s: 1.0}` give `R_st = voltages[s]`. `0.0` ohm edges are merged by union-find. `node_component` groups the terminals by component, and every solve pins one reference terminal per terminal-bearing component, for `sum_c (T_c - 1)` solves. Cross-component pairs are `null` with `terminals_disconnected`; a remaining `no_pad` component (no terminal) maps to `floating_component`; `not_converged` maps to `singular_system`. No change to the module. |
| Manhattan segmentation | `power._decompose_manhattan_polygon` (+ `_polygon_cut_coordinates`, `_decomposition_grid`) | **Reused** for step 3, plus one backward-compatible extension: optional extra cut coordinates for port positions. Its failure reasons map to `unsupported_geometry` / `graph_too_large`. |
| Piece edge model | `power._model_mesh_polygon` | **Same formula, reused as specification**: centre-to-side `Rs * half / cross` edges (3.D2 step 5). Its free-end terminal rule is not reused, because extraction ports come from landings and terminals rather than from the major axis of the cell. |
| Via landing scope | `power._rails_under_via` | **Reused**: restricts a landing to the merged polygon(s) it physically touches (issue #2259). |
| Geometry to network for `klt power` | `power._build_island_network` | **Not reused as a whole**, for three reasons. It is keyed to the `klt power` spec (stackup/vias with EM limits, an `LayoutToNetlist` island). It models a rectangular polygon as a single end-to-end edge and snaps vias to the nearest endpoint, while extraction needs ports at their true position along a strip. And it prices a via per merged via shape, not per cut. The extract builder is a sibling of it that calls the shared lower-level helpers above. |

**Additive opt-in output.** A new flag (proposed `--resistance-graph`; final
spelling is chosen in increment 3) adds, per net, an object
`parasitics.nets[].resistance_graph`:

```json
{
  "status": "ok",
  "terminals": ["M1.D", "M2.S"],
  "pair_resistance_ohm": [[0.0, 75.0], [75.0, 0.0]],
  "legacy_resistance_ohm": 450.0,
  "nodes": 12, "edges": 11,
  "diagnostics": []
}
```

`status` is `ok`, `unsupported` or `skipped`; `nodes`/`edges` are counts
(full node/edge lists are a separate, later, `--format json` detail option
to keep default output size flat). A `pair_resistance_ohm` entry is `null`
when its two terminals lie in different components (with
`terminals_disconnected` in `diagnostics`, as in F9). Without the flag,
no key is added and output is byte-identical to today. With it,
`resistance_ohm`, `terminals[].resistance_ohm`, the star/ladder netlist and
every other existing field keep their legacy meaning; any decision to *replace* the scalar with a
graph-derived value is a separate, explicit, documented change (and is where
#2458's cross-level fix lands), never a side effect of this flag. No field is
removed, renamed or retyped (`docs/json-contract.md`).

**Fallback.** `unsupported` or `skipped` nets keep the legacy scalar exactly.

**Coexistence with the ladder.** `--distributed-rc` redistributes
whatever scalar `resistance_ohm` it is given along the terminal order; it
conserves that total and cannot create or remove parallel structure. While the
scalar is the series sum, the ladder inherits the bias. If a graph-derived
scalar is later substituted, the ladder consumes it unchanged and its
conservation tests stay valid; a graph-aware ladder (segments taken from graph
edges) is the later Stage 3 netlist step, not part of the DC increments.

##### 3.D4 Fixture matrix

Assumptions for every row: illustrative sheet resistance `Rs = 10 ohm/sq` for
every conducting role (not PDK data; real runs use deck coefficients),
rectilinear strips of uniform width, rails/vias/terminals **ideal
(equipotential, zero resistance)** unless a finite via resistance `Rv` is
given, current-crowding not modelled. `n_sq = length / width`, strip
`R = Rs * n_sq`. "Legacy" is the current series-sum scalar for the same
geometry. Every value was recomputed independently from series (`R1+R2`) and
parallel (`R1*R2/(R1+R2)`) equations. Tolerance: relative `1e-9` for exactly
representable cases (uniform strips, any segmentation), `1e-6` where a divide
is inexact, and `3%` absolute-relative for any variant with a bend or T where
first-order junction treatment applies.

Strip names: **S** = 20 x 2 um (10 sq, 100 ohm); **H** = 10 x 2 um (5 sq,
50 ohm); **L** = 60 x 2 um (30 sq, 300 ohm).

**Roles and ideal nodes (this is what makes the Legacy column well-defined).**

- *Conducting pieces.* Every strip, branch and arm named in a row is a
  straight rectangle on **level 1**. The only exceptions are the level-2
  strips in F4, F4v, F5, F5v and F6. Within one role, no two strips touch or
  overlap, so each strip is **its own connected fragment**. Legacy therefore
  equals the sum over strips of `_n_squares` per fragment, which is exact
  (`L/W`) for a rectangle, times `Rs`.
- *Ideal nodes.* Rails (F2, F2v, F3, F7a, F7b), the F3 junction and the F8 hub
  are **abstract ideal nodes**: equipotential, zero resistance, and not drawn on
  any conducting role. They contribute no squares to Legacy and no pieces
  to the graph. A strip reaches an ideal node through a `0.0` ohm edge from its
  end-edge port, and `solve_ir_drop` merges these (3.D2 step 7). The one
  exception is the F2v branch via, which is the finite `Rv` edge.
- *Ports.* Every terminal, via and ideal-node connection attaches at a strip's
  **end edge**, so it has zero landing length. It is joined to the end piece by
  the half-piece edge `Rs*d/(2w)` from 3.D2 step 5. With this rule the
  conducting length of each strip is its full drawn length, which is why
  the analytic column uses `L/W` for each strip.
- *No in-plane junctions.* Every point where three or more conductors meet
  is an ideal node, never a T or bend inside one fragment. So **none of the
  rows below needs the 3% tolerance**. All of them use `1e-9` or `1e-6`. The
  3% tier applies only to variants drawn as a single same-role comb or
  star. In such a variant Legacy becomes one `_n_squares` fit of the whole
  shape, and the junction is a real in-plane T. Those variants are future
  rows and are not in this matrix.
- *Layout realisation (increment 3).* Graph-level inputs (pieces plus declared
  ports) realise these rows exactly. A layout-level fixture instead draws
  finite via landings and rail shapes, which adds landing squares to
  *both* columns. Such a fixture must state its landing geometry and
  recompute both values from it. It must not reuse the numbers below.

Per-row realisation, with all strips on level 1 unless stated:

- **F1**: S. The terminals are the end-edge ports at its two ends.
- **F2, F7a, F7b**: N (or 2, or 3) parallel, separated strips. Ideal node
  `RAIL_A` joins every strip's left end, and ideal node `RAIL_B` joins
  every right end. The terminals are `RAIL_A` and `RAIL_B`.
- **F2v**: as F2. On each branch, the left end joins `RAIL_A` through a via
  edge of `Rv = 10` ohm, and the right end joins `RAIL_B` ideally. The via
  is not charged by Legacy.
- **F3**: H plus four separated S. Ideal node `J` (the explicit junction rail)
  joins H's far end to the near ends of all four S. Ideal node `RAIL` joins
  the four far ends. The terminals are H's free end and `RAIL`.
- **F4, F4v**: S on level 1 and S on level 2 with the same footprint. At each
  end, the two strips' end-edge ports are joined by a via edge, which is
  ideal in F4 and `Rv = 10` in F4v. The terminals are the level-1 end ports.
- **F5, F5v**: the level-1 S far end is joined to the level-2 S near end by one
  via edge, which is ideal in F5 and `Rv = 10` in F5v. The terminals are the
  level-1 near end and the level-2 far end.
- **F6**: as F4 but with no via edge. Level 2 is a separate component with no
  terminal, which gives the `floating_component` diagnostic, and the net is
  joined by label only.
- **F8**: arms a, b and c are separated strips. Ideal node `HUB` joins their
  inner ends, and the terminals A, B and C are their outer ends. Each arm's
  length is its full drawn length, because there is no in-plane junction
  square to share.
- **F9**: two separated level-1 strips joined by label only, with no via
  edge or ideal node between them. Strip H, 10 x 2 um, carries terminal A
  at its left end-edge port, and its right end is free. Strip S, 20 x 2 um,
  carries terminals B and C at its two end-edge ports. The net therefore
  has two terminal-bearing components, {A} and {B, C}, and no terminal-free
  component.

| ID | Geometry and endpoints | Analytic result | Legacy |
|---|---|---|---|
| F1 | Single strip S; terminals at the two ends | 100 ohm | 100 |
| F2 | N identical S branches between two ideal rails; terminals on the rails. N = 2, 4, 8 | `100/N` = 50, 25, 12.5 ohm | 200, 400, 800 |
| F2v | As F2, each branch also has one via of `Rv = 10` ohm in series. N = 4 (and N = 3 for the monotonicity check) | `(100+10)/N` = 27.5 ohm (N = 4); 110/3 = 36.666667 ohm (N = 3) | 400 (N = 4); 300 (N = 3) |
| F3 | Shared strip H, ideal junction `J`, then N = 4 S branches in parallel to ideal `RAIL`; terminals at H's free end and `RAIL` | `50 + 100/4` = 75 ohm | 450 |
| F4 | Two levels: S on level 1 and S on level 2 stacked, ideal vias at **both** ends; terminals at level-1 ends | `100/2` = 50 ohm | 200 |
| F4v | As F4 with `Rv = 10` per end (one via each end) | `100 \|\| (10+100+10)` = 12000/220 = 54.5454545 ohm | 200 |
| F5 | Level-1 S then ideal via then level-2 S; the levels share **one** landing; terminals at the two outer ends | `100 + 100` = 200 ohm | 200 |
| F5v | As F5 with `Rv = 10` at the single via | 210 ohm | 200 |
| F6 | Level-1 S and level-2 S fully overlapping, **no via/contact** between them (net joined by label only); terminals at level-1 ends | 100 ohm (level 2 is not on the current path) | 200 |
| F7a | Unequal branches S (100) and L (300) between ideal rails | `100*300/400` = 75 ohm | 400 |
| F7b | Three unequal branches 100, 200, 400 ohm (S, 2S, 4S in length at equal width) between ideal rails | `1/(1/100+1/200+1/400)` = 400/7 = 57.142857 ohm | 700 |
| F8 | Three-terminal star: arms `a` = S (100), `b` = H (50), `c` = 30 x 2 um (15 sq, 150 ohm), separate level-1 fragments joined at ideal node `HUB`; terminals A, B, C at the outer arm ends | see below | 300 |
| F9 | Disconnected terminals: H (50) with terminal A at one end, and a separate S (100) with terminals B and C at its ends; net joined by label only | `R_BC = 100` ohm; `R_AB = R_AC = null` (see below) | 150 |

F8 pair resistances (the other terminal floating): `R_AB = a+b = 150`,
`R_AC = a+c = 250`, `R_BC = b+c = 200` ohm. Equivalent delta network with
`ab+bc+ca = 5000+7500+15000 = 27500`: `R_AB' = 27500/c = 183.333`,
`R_BC' = 27500/a = 275`, `R_AC' = 27500/b = 550` ohm (check: `183.333 ||
(550+275)` = 150, as above). Endpoint/boundary dependence: with C **tied** to B,
`R_A,(B=C) = a + b||c = 100 + 37.5 = 137.5` ohm, not 150. There is no unique
scalar for the net; the series sum 300 matches no terminal pair. A fixture
passes when the full matrix matches to `1e-6` and the tied variant to `1e-6`.

F9 terminal matrix, in terminal order A, B, C:

```
        A     B      C
  A [ 0.0,  null,  null ]
  B [ null, 0.0,   100.0 ]
  C [ null, 100.0, 0.0  ]
```

`R_BC` is the full S strip, `Rs * 20/2 = 100` ohm, and must match to
relative `1e-9`. The diagonal is exactly `0.0`. `R_AB`, `R_AC` and their
transposes are exactly JSON `null`, never `inf`, `0` or a number. The
diagnostics are exactly one `terminals_disconnected` with groups
`[["A"], ["B", "C"]]` and no `floating_component`, because H carries
terminal A and is a dead end, not a floating component. `status` is `ok`.
The construction makes exactly **one** solve (component {B, C}, reference
B, injection at C) and none for {A}. A single-reference construction
(`pads={A: 0.0}`) fails this fixture: it returns `None` for B and C.
Legacy charges both fragments, `50 + 100 = 150`.

**Monotonicity note (scope).** Adding a passive edge (non-negative
conductance) in parallel between two *existing* nodes cannot increase the
effective resistance between the *same fixed* endpoints under the *same*
boundary conditions (Rayleigh monotonicity). Examples that are exactly this
case: F2 N=2 (50) vs. F1 (100); F4 (50) vs. F1; F7a (75) vs. F1; and F2v N=4
(`110/4` = 27.5) vs. F2v N=3 (`110/3` = 36.667). The last one adds a fourth
`Rv`+S branch between the same two rails, and every existing branch keeps
its `Rv`. A comparison that also changes the series content of the existing
branches, such as F2v vs. F2, is not an instance of the rule. Tests may assert
`R_graph <= R_without_the_edge` only under that exact condition. It must not
be generalised: changing endpoints can raise R (F8: `R_AC` 250 > `R_AB` 150),
adding a terminal changes the boundary condition (a *tied* terminal can lower
R, a floating one leaves it unchanged), and adding a series element raises R
(F5v > F5).

For the ideal-via fixtures the sanity relation `R_graph <= legacy` holds (the
roadmap's direction check), and the F1 and F5 rows show equality is
legitimate: the graph can also *confirm* the series sum. It is **not** a
universal invariant: the legacy scalar charges no via resistance, so F5v
(210 ohm) exceeds its legacy 200 ohm by exactly `Rv`.

##### 3.D5 Implementation increments

Each increment is separately mergeable; none changes default output.

1. **Graph construction.** New module `src/klayout_tools/extract_resistance_graph.py`
   (pure data and builder: node/edge ids, port cuts, via/contact edges,
   zero-edge emission, diagnostics). Reads, does not modify,
   `_region_n_squares` / `_net_port_regions` / `_fragment_port_points_um`
   outputs. Segmentation and landing scope reuse `power.py`'s helpers:
   `_polygon_cut_coordinates`, `_decomposition_grid`,
   `_decompose_manhattan_polygon` and `_rails_under_via` move **unchanged** into
   a shared geometry module (proposed `src/klayout_tools/resistor_mesh.py`),
   `power.py` re-imports them, and `_decompose_manhattan_polygon` gains an
   optional extra-cut-coordinates argument that defaults to none. Tests in
   a new `tests/test_extract_resistance_graph.py`: node/edge counts and
   determinism (two runs identical, input order shuffled) on F1, F2, F4, F5,
   F6, F9; port cuts placed at landing positions; diagnostics on missing
   coefficient, non-Manhattan polygon, oversized graph. Acceptance: the
   edge resistances along F1's and F5's single path, including the two
   half-piece port edges (3.D2 step 5), sum to the analytic 100 and 200 ohm
   to `1e-9` for 1, 2 and 7 pieces per strip. The existing `klt power` tests
   pass unchanged, which shows the helper move and the defaulted argument are
   behaviour-neutral.
2. **DC solve.** A thin adapter in the same module over
   `ir_solver.solve_ir_drop`, with **no new solver**. It produces the
   two-terminal `R_st` (`pads={t: 0.0}`, `injections={s: 1.0}`). It
   builds the `T x T` matrix per connected component (3.D2 "Terminal
   matrix"): it groups terminals via `node_component`, pins one reference
   per terminal-bearing component in every solve, and makes
   `sum_c (T_c - 1)` solves. It fills cross-component entries with JSON
   `null` plus one `terminals_disconnected` diagnostic, maps the remaining
   `no_pad` components to `floating_component` and `not_converged` to
   `singular_system`. `ir_solver.py` itself is unchanged. Acceptance:
   - every fixture row F1-F9, including F2v (N = 3 and 4), F4v, F5v and the
     tied F8 variant, at the stated tolerances;
   - F9's full matrix (diagonal `0.0`, `R_BC = 100` to `1e-9`, A's
     off-diagonals `null`) and its exact diagnostics;
   - the solve count, asserted by counting the adapter's injection
     solves (the grouping call is not counted): 1 for F9, 2 for F8, and 1
     for each connected two-terminal row;
   - the monotonicity assertion (F2v N=4 <= F2v N=3, and F2/F4/F7a <= F1);
   - F6 returns `floating_component`, and a not-converged case returns
     `singular_system`.
3. **Integration.** `_compute_parasitics` calls the builder/solver when the
   opt-in flag is set and attaches `resistance_graph`; CLI flag in
   `src/klayout_tools/cli/` plus `docs/cli/extract.md` and the JSON schema
   notes. Acceptance: with the flag off, existing `tests/test_extract.py`
   output is byte-identical; with it on, `resistance_ohm` and the ladder
   conservation tests are unchanged and `resistance_graph` matches F-fixture
   layouts built with the existing test helpers; artifact-determinism gate
   passes.
4. **Out of this design (not scheduled here).** Substituting the graph value
   for `resistance_ohm` (owned by #2458 for cross-level cases, with its
   regression on measured layouts), graph-aware ladder segments, and
   per-node capacitance. Measurement against magic `extresist` and the
   bandgap `VSS` artefact (with hash) belong to that follow-on.

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
