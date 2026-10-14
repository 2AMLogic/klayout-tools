# Work Plan

Current work from GitHub labels. Long-term direction lives in [ROADMAP.md](ROADMAP.md).

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs stuck under a `loom:operator` merge-risk hold — implementation work is done, only a human merge decision is missing.

_None._

## Operator Priority

Issues the operator starred (`loom:operator-priority`); land these first.

_None._

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#1966**: Re-evaluate the .loom/resync-ignore pin on curator.md (#668) — it may be freezing this repo against upstream Curator fixes
- **#2340**: signoff: input_verified is always null for drc/extract envelopes that recorded an absolute host path
- **#2849**: lvs: no device-aware path when a PDK's own KLayout LVS runset recognizes devices the curated deck lacks (no native-runset engine; runset netlists unreadable as layout.netlist)
- **#2865**: klt signoff: a 'not applicable' outcome for T1 item 6 when the ratified spec has no statistical row (operator ruling 2026-10-08)

## In Progress

Issues currently being built (`loom:building`).

- **#2814**: ASAP7 FinFET: ngspice BSIM-CMG simulation of extracted netlists (OSDI prerequisite)
- **#2879**: sta: response echoes def_path/spef_path as absolute host paths, so signoff cannot verify an sta citation's input from another checkout

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#2645**: feat(synthesize,sta,place-and-route): shared request.macros hard-macro declaration
- **#2701**: feat(drc,extract,lvs): emit echoed input paths as the portable {path, scope} envelope
- **#2711**: feat(loom): frozen-timeline guard in claim-staleness.sh + pin (#1966, PR 1 of 2)
- **#2795**: drc: model sg13g2 Cnt.c activ_mask join and square-contact selection
- **#3143**: chore(deps): update dependency numpy to v2.5.4
- **#3149**: fix(deps): update dependency mcp to v2
- **#3200**: feat(signoff): item 6 not_applicable outcome (#2865)
- **#3234**: sta: emit def/verilog/spef paths as {path, scope} envelopes (schema 2)
- **#3235**: docs(sim): record ASAP7 BSIM-CMG ngspice real-run evidence (#2814)

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#3152**: feat(sim): dispatch pre-composed ngspice decks to remote/batch backends (#2804)
- **#3174**: klt sim: analysis_aborted diagnostic and reject {param} braces in analysis.args
- **#3178**: feat(signoff): --attest producer for T1 items 1, 9, 10
- **#3187**: test(asap7): gate shipped R/L/SL cells on exact deviations and a clean CLI run; document no DRM density rule
- **#3188**: fix(lvs): don't destroy interior top circuit when pruning wrappers (#2852)
- **#3193**: feat(lvs): forward abstract_cells/abstract_cell_lef to inline extraction (mixed-level LVS)
- **#3194**: feat(decks): BipolarDevice.marker accepts a derived LayerExpr (AND/NOT tree)
- **#3196**: fix(extract): deterministic FinFET source/drain assignment (#3154)
- **#3198**: test(toolchain-pins): enforce exact Verilator/Icarus pin only on CI or pinned installs (#3072)
- **#3199**: docs: reconcile design-stage skills and ROADMAP with shipped RC parasitics / klt pex (#3179)
- **#3205**: feat(extract): bipolar terminal relations to another terminal of the same device (#2863)
- **#3207**: feat(lvs): klayout-runset engine -- verdict from the PDK runset's .lvsdb (part of #2849)
- **#3208**: feat(extract): recognise sg13g2 poly resistors with abutting-head topology (#2679)
- **#3209**: feat(extract): SizeFilter / BipolarDevice.emitter_size (#2861)
- **#3220**: docs: point SG13G2 SiGe HBT finding at the runset-backed LVS ruling (#2864)
- **#3232**: feat(sim): request feature minimum-runner-version registry and docs (#2877)
- **#3233**: docs(signoff): uv-first pinned regeneration recipe for consumer repos

## Proposed

Issues carrying `loom:curated`.

- **#1519**: MoM increment (iii): mixed-axis winding geometry and series-path solves *(curated)*
- **#1966**: Re-evaluate the .loom/resync-ignore pin on curator.md (#668) — it may be freezing this repo against upstream Curator fixes *(curated)*
- **#2093**: The tools klt orchestrates have silent-failure modes: a catalogue with evidence, for workaround-or-report decisions *(curated)*
- **#2281**: Track synthesis memory-mapping reporting and the observability design spike *(curated)*
- **#2340**: signoff: input_verified is always null for drc/extract envelopes that recorded an absolute host path *(curated)*
- **#2458**: klt extract --parasitics sums a net's per-level lumped R in series, so via-stitched strapping raises the reported resistance instead of lowering it *(curated)*
- **#2574**: Build + publish the ihp-sg13g2 remote-sim AMI (operator-only; gated on #2573) *(curated)*
- **#2635**: synthesize / sta / place-and-route: no way to declare a hard macro (LEF + liberty + GDS) in any request schema *(curated)*
- **#2659**: drc/extract responses embed the producing host's absolute input path; signoff's input_verified gate cannot re-verify them from another checkout *(curated)*
- **#2679**: decks/sg13g2: poly-resistor body/marker are inverted vs the PDK's own PCell convention, so no drawn rsil/rppd/rhigh is ever recognized *(curated)*
- **#2688**: drc: curated sg13g2 Cnt.c drops the activ_mask join, so every HBT contact false-positives *(curated)*
- **#2724**: klt sim: no AC measurement over a two-node complex ratio (loop gain / phase margin) *(curated)*
- **#2725**: klt sim: no first-class design-parameter axis (and no per-unit .nodeset); supply_v alter is the only hook *(curated)*
- **#2727**: Friction: klt sim's batch/remote backends refuse ihp-sg13cmos5l, so no SG13CMOS5L corner grid can run on a batch-only host *(curated)*
- **#2730**: Extend retarded MoM impedance to oriented winding segments *(curated)*
- **#2734**: Epic: STA completeness, P&R density coverage, repair validation and iteration cost *(curated)*
- **#2760**: ASAP7 DRC deck for KLayout *(curated)*
- **#2761**: ASAP7 LVS / FinFET device extraction (nfin) *(curated)*
- **#2766**: ci: monotonic per-file size ratchet checked against the merge-base *(curated)*
- **#2804**: klt sim: dispatch already-composed decks to the remote/batch backends (re-filing of #2464) *(curated)*
- **#2811**: ASAP7 DRC deck phase 3: density checks and full shipped-cell clean-run gate *(curated)*
- **#2812**: ASAP7 FEOL/MOL gap tracker: mechanism-specific rule increments *(curated)*
- **#2814**: ASAP7 FinFET: ngspice BSIM-CMG simulation of extracted netlists (OSDI prerequisite) *(curated)*
- **#2835**: sky130 DRC: enforce licon1/mcon maximum size without rejecting compound-layer exceptions *(curated)*
- **#2840**: Friction: klt yield reports carry no seed / corner-matrix / spec-row provenance for T1 item 6's recorded-seed requirement *(curated)*
- **#2844**: signoff: no tool-produced envelope for T1 items 1, 9, 10 (citation graded on presence only) *(curated)*
- **#2846**: klt sim: an aborted analysis is reported only as per-measurement 'no value' (and .param braces in analysis.args silently abort it) *(curated)*
- **#2847**: klt sim: no way to express a two-pass AC bench (dual-injection loop gain) with derived-vector margins *(curated)*
- **#2849**: lvs: no device-aware path when a PDK's own KLayout LVS runset recognizes devices the curated deck lacks (no native-runset engine; runset netlists unreadable as layout.netlist) *(curated)*
- **#2851**: klt sim batch: publish runner klt identity and reject incompatible clients before instance launch *(curated)*
- **#2852**: klt lvs crashes ("Object has been destroyed already") when reference.top is a subcircuit that another subcircuit in the same reference file instantiates *(curated)*
- **#2853**: Friction: mixed-level LVS (transistor-level region + abstracted standard cells) needs a pre-extracted netlist, which drops body_verification and power_connectivity *(curated)*
- **#2858**: Track STA input-transition constraints and interconnect SPEF matrix *(curated)*
- **#2860**: extraction decks: a device marker cannot be a derived boolean of several layers (bipolar gap 1 of 4 from #1232) *(curated)*
- **#2861**: extraction decks: no size-filtered region (bbox / area) to tell device flavours apart (bipolar gap 2 of 4 from #1232) *(curated)*
- **#2863**: BipolarDevice: a terminal cannot be defined by geometric interaction with another terminal of the same instance (bipolar gap 4 of 4 from #1232) *(curated)*
- **#2864**: Revisit #1232: SG13G2 bipolar recognition is declined, and three blocks cannot reach T1 because of it *(curated)*
- **#2865**: klt signoff: a 'not applicable' outcome for T1 item 6 when the ratified spec has no statistical row (operator ruling 2026-10-08) *(curated)*
- **#2874**: regenerate.sh template builds a venv with python3 -m venv/pip; fails where ensurepip is unavailable *(curated)*
- **#2877**: klt sim batch: request features newer than the fleet runner fail the whole job instead of being refused client-side *(curated)*
- **#2879**: sta: response echoes def_path/spef_path as absolute host paths, so signoff cannot verify an sta citation's input from another checkout *(curated)*
- **#2880**: klt sta: SPEF annotation reported complete by name count while parasitics are not applied (delay_changed false) *(curated)*
- **#2881**: gen-compose: a port wired by connectivity[] cannot also be promoted via pins[], so an internal net can't be exposed as a cell port without a stub *(curated)*
- **#3072**: test: verilator pin check fails preflight on hosts with Verilator 5.052 (pinned 5.050) *(curated)*
- **#3154**: tests/test_asap7_finfet.py::test_fin_count_and_gate_length_recovered_exactly fails on main (S/D net labels swapped) *(curated)*
- **#3179**: docs: design-extraction skill and ROADMAP still claim RC parasitics are unshipped (#217 closed) *(curated)*

## Proposed (Architect / Hermit)

- **#485**: Live digital fleet validation — measure T_o/t, derive the K-selection rule *(architect)*
- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark *(architect)*
- **#708**: Epic: finite-element full-field solver for klt (Rust) — volumetric fields, electrothermal, MoM cross-validation *(architect)*
- **#3204**: tests: fail the pytest session when the run leaves new untracked files in the repo tree *(architect)*
- **#3221**: ci: add a baseline-ratcheted static type check and ship py.typed for the JSON-contract modules *(architect)*
- **#3222**: ci: enable the clean high-signal ruff security (S) rules and review the 4 remaining findings *(architect)*
- **#3223**: ci: measure Python test coverage (report-only) on one CI leg to expose skip-masked gaps *(architect)*
- **#2842**: Remove or wire up digital_fleet.py: ~1,850 LOC with no production caller *(hermit)*

## Epics

- **#1519**: MoM increment (iii): mixed-axis winding geometry and series-path solves
- **#2252**: Epic: layout-tier flow-comparison benchmark — draw-only vs generator-first vs generator+refine (AHRR, ICCAD'26)
- **#2281**: Track synthesis memory-mapping reporting and the observability design spike
- **#2351**: gf180mcu curated DRC deck checks no implant or tap rule (NP/PP, DF.12, DF.13/14) and CO.1 only as a minimum, and --engine klayout cannot resolve the gf180mcu split-table runset
- **#2570**: Friction: no ihp-sg13g2 remote-sim AMI, so klt sim's batch backend cannot run a PDK klt fully supports locally
- **#2724**: klt sim: no AC measurement over a two-node complex ratio (loop gain / phase margin)
- **#2734**: Epic: STA completeness, P&R density coverage, repair validation and iteration cost
- **#2831**: Contracts: ship JSON response schemas for the core verbs (layers, cells, stats, drc, lvs) with conformance tests
- **#2847**: klt sim: no way to express a two-pass AC bench (dual-injection loop gain) with derived-vector margins

## Backlog Balance

| Tier | Count |
|------|-------|
| Operator merge-risk holds | 0 |
| Operator priority | 0 |
| Ready (`loom:issue`) | 4 |
| In Progress (`loom:building`) | 2 |
| PRs awaiting review | 9 |
| Approved PRs awaiting merge | 17 |
| Curated | 46 |
| Architect / Hermit proposals | 8 |
| Active epics | 9 |
<!-- guide:plan-body:end -->
