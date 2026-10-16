# Work Plan

Current work from GitHub labels. Long-term direction lives in [ROADMAP.md](ROADMAP.md).

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs stuck under a `loom:operator` merge-risk hold — implementation work is done, only a human merge decision is missing.

- **#2645**: feat(synthesize,sta,place-and-route): shared request.macros hard-macro declaration
- **#3143**: chore(deps): update dependency numpy to v2.5.4
- **#3149**: fix(deps): update dependency mcp to v2
- **#3174**: klt sim: analysis_aborted diagnostic and reject {param} braces in analysis.args
- **#3188**: fix(lvs): don't destroy interior top circuit when pruning wrappers (#2852)
- **#3198**: test(toolchain-pins): enforce exact Verilator/Icarus pin only on CI or pinned installs (#3072)
- **#3199**: docs: reconcile design-stage skills and ROADMAP with shipped RC parasitics / klt pex (#3179)
- **#3232**: feat(sim): request feature minimum-runner-version registry and docs (#2877)

## Operator Priority

Issues the operator starred (`loom:operator-priority`); land these first.

_None._

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#1966**: Re-evaluate the .loom/resync-ignore pin on curator.md (#668) — it may be freezing this repo against upstream Curator fixes
- **#2340**: signoff: input_verified is always null for drc/extract envelopes that recorded an absolute host path
- **#2849**: lvs: no device-aware path when a PDK's own KLayout LVS runset recognizes devices the curated deck lacks (no native-runset engine; runset netlists unreadable as layout.netlist)
- **#2882**: Friction: klt sim batch jobs fail with 'Could not find include file <host PDK path>/libs.tech/ngspice/design.ngspice' on the fleet (client 0.6.0 past the version gate)
- **#2883**: klt synthesize: no way to express a multi-clock design's constraints (single constraints.clock_period_ns)

## In Progress

Issues currently being built (`loom:building`).

_None._

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

_None._

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#2645**: feat(synthesize,sta,place-and-route): shared request.macros hard-macro declaration
- **#2701**: feat(drc,extract,lvs): emit echoed input paths as the portable {path, scope} envelope
- **#2711**: feat(loom): frozen-timeline guard in claim-staleness.sh + pin (#1966, PR 1 of 2)
- **#2795**: drc: model sg13g2 Cnt.c activ_mask join and square-contact selection
- **#3143**: chore(deps): update dependency numpy to v2.5.4
- **#3149**: fix(deps): update dependency mcp to v2
- **#3174**: klt sim: analysis_aborted diagnostic and reject {param} braces in analysis.args
- **#3178**: feat(signoff): --attest producer for T1 items 1, 9, 10
- **#3187**: test(asap7): gate shipped R/L/SL cells on exact deviations and a clean CLI run; document no DRM density rule
- **#3188**: fix(lvs): don't destroy interior top circuit when pruning wrappers (#2852)
- **#3193**: feat(lvs): forward abstract_cells/abstract_cell_lef to inline extraction (mixed-level LVS)
- **#3194**: feat(decks): BipolarDevice.marker accepts a derived LayerExpr (AND/NOT tree)
- **#3198**: test(toolchain-pins): enforce exact Verilator/Icarus pin only on CI or pinned installs (#3072)
- **#3199**: docs: reconcile design-stage skills and ROADMAP with shipped RC parasitics / klt pex (#3179)
- **#3200**: feat(signoff): item 6 not_applicable outcome (#2865)
- **#3205**: feat(extract): bipolar terminal relations to another terminal of the same device (#2863)
- **#3207**: feat(lvs): klayout-runset engine -- verdict from the PDK runset's .lvsdb (part of #2849)
- **#3208**: feat(extract): recognise sg13g2 poly resistors with abutting-head topology (#2679)
- **#3209**: feat(extract): SizeFilter / BipolarDevice.emitter_size (#2861)
- **#3220**: docs: point SG13G2 SiGe HBT finding at the runset-backed LVS ruling (#2864)
- **#3232**: feat(sim): request feature minimum-runner-version registry and docs (#2877)
- **#3256**: fix(extract): keep SPEF *CONN for dotted DEF net names (#2903)

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
- **#2766**: ci: monotonic per-file size ratchet checked against the merge-base *(curated)*
- **#2811**: ASAP7 DRC deck phase 3: density checks and full shipped-cell clean-run gate *(curated)*
- **#2812**: ASAP7 FEOL/MOL gap tracker: mechanism-specific rule increments *(curated)*
- **#2835**: sky130 DRC: enforce licon1/mcon maximum size without rejecting compound-layer exceptions *(curated)*
- **#2840**: Friction: klt yield reports carry no seed / corner-matrix / spec-row provenance for T1 item 6's recorded-seed requirement *(curated)*
- **#2844**: signoff: no tool-produced envelope for T1 items 1, 9, 10 (citation graded on presence only) *(curated)*
- **#2846**: klt sim: an aborted analysis is reported only as per-measurement 'no value' (and .param braces in analysis.args silently abort it) *(curated)*
- **#2847**: klt sim: no way to express a two-pass AC bench (dual-injection loop gain) with derived-vector margins *(curated)*
- **#2849**: lvs: no device-aware path when a PDK's own KLayout LVS runset recognizes devices the curated deck lacks (no native-runset engine; runset netlists unreadable as layout.netlist) *(curated)*
- **#2852**: klt lvs crashes ("Object has been destroyed already") when reference.top is a subcircuit that another subcircuit in the same reference file instantiates *(curated)*
- **#2853**: Friction: mixed-level LVS (transistor-level region + abstracted standard cells) needs a pre-extracted netlist, which drops body_verification and power_connectivity *(curated)*
- **#2858**: Track STA input-transition constraints and interconnect SPEF matrix *(curated)*
- **#2860**: extraction decks: a device marker cannot be a derived boolean of several layers (bipolar gap 1 of 4 from #1232) *(curated)*
- **#2861**: extraction decks: no size-filtered region (bbox / area) to tell device flavours apart (bipolar gap 2 of 4 from #1232) *(curated)*
- **#2863**: BipolarDevice: a terminal cannot be defined by geometric interaction with another terminal of the same instance (bipolar gap 4 of 4 from #1232) *(curated)*
- **#2864**: Revisit #1232: SG13G2 bipolar recognition is declined, and three blocks cannot reach T1 because of it *(curated)*
- **#2865**: klt signoff: a 'not applicable' outcome for T1 item 6 when the ratified spec has no statistical row (operator ruling 2026-10-08) *(curated)*
- **#2877**: klt sim batch: request features newer than the fleet runner fail the whole job instead of being refused client-side *(curated)*
- **#2882**: Friction: klt sim batch jobs fail with 'Could not find include file <host PDK path>/libs.tech/ngspice/design.ngspice' on the fleet (client 0.6.0 past the version gate) *(curated)*
- **#2883**: klt synthesize: no way to express a multi-clock design's constraints (single constraints.clock_period_ns) *(curated)*
- **#2885**: functional-verification options.sdf: write_sdf IOPATH entries cannot match edge-qualified (ifnone posedge/negedge) cell-model arcs on XOR/XNOR/MUX cells -- every such netlist fails annotation *(curated)*
- **#2886**: Post-route SDF export is tied to the place-and-route flow corner: no way to emit a slow/fast-corner SDF for an already-routed DEF *(curated)*
- **#2887**: signoff: no evidence form for T1 items 1/2 (generator/netlist-freshness, directory of committed artifacts) *(curated)*
- **#2889**: klt pex: no way to pair a hierarchical schematic DUT with the flat extracted netlist (no flatten option, unlike klt lvs flatten_reference) *(curated)*
- **#2890**: klt pex: extracted .SUBCKT pins are written alphabetically, but a testbench instantiates its DUT positionally; no reconciliation or diagnostic *(curated)*
- **#2891**: klt pex/sim local backend: a testbench's relative .include of its DUT does not resolve (ngspice runs from the corner directory); only the extracted leg is rewritten to an absolute path *(curated)*
- **#2893**: klt sim: measurements[].expr after a noise analysis cannot see inoise_spectrum (current plot is noise2; needs hard-coded noise1. prefix) *(curated)*
- **#2894**: klt sim: silent no-op supply_v key, no parameter corner axis, opaque runner capability skew (batch) *(curated)*
- **#2897**: functional-verification: escaped-divider INTERCONNECT SDFs are now rejected (fail-loud) but still cannot be back-annotated *(curated)*
- **#2898**: klt sim monte_carlo: family_mismatch reports gf180mcu poly-resistor/MIM-cap subcircuits as family 'other' (active: null), hiding that they have no per-instance mismatch *(curated)*
- **#2899**: klt sim monte_carlo: mismatch seed is keyed on corner_index, so the same sample_index is a different die at each corner *(curated)*
- **#2900**: Friction: documented 'klayout-tools[yield]' install path does not resolve (klt-yield-native not on PyPI) *(curated)*
- **#2902**: klt sim: no way to sweep supply level on a ramped (PWL) supply source; transient-settling benches need one request per supply *(curated)*
- **#2903**: klt extract --def-net-connections: nets whose DEF name contains '.' get no SPEF *CONN block since the dotted-name rewrite (#2145) *(curated)*
- **#2904**: Friction: mos_array dummy units expose no ports, so dummy gates and S/D cannot be tied off and extract as dead metal *(curated)*
- **#2905**: Friction: klt lvs subckt-call cannot verify a resistor's R although klt extract reports r_ohm from the same deck *(curated)*
- **#2906**: Grader decides partial_coverage from envelope status alone; ignores skipped-work entries in the coverage block *(curated)*
- **#2908**: Friction: klt signoff grades T1 item 6 from whichever yield report is cited, with no way to declare the statistical-row set or partial/unratified coverage *(curated)*
- **#2909**: Friction: klt yield reports a bit-identical sample set as non-degenerate (rounding stddev -> normality rejected, Cpk ~1e13) *(curated)*
- **#2911**: klt sim monte_carlo: no first-class way to declare design-owned per-instance mismatch for devices whose PDK model has no local hook *(curated)*
- **#3072**: test: verilator pin check fails preflight on hosts with Verilator 5.052 (pinned 5.050) *(curated)*
- **#3179**: docs: design-extraction skill and ROADMAP still claim RC parasitics are unshipped (#217 closed) *(curated)*

## Proposed (Architect / Hermit)

- **#485**: Live digital fleet validation — measure T_o/t, derive the K-selection rule *(architect)*
- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark *(architect)*
- **#708**: Epic: finite-element full-field solver for klt (Rust) — volumetric fields, electrothermal, MoM cross-validation *(architect)*
- **#3221**: ci: add a baseline-ratcheted static type check and ship py.typed for the JSON-contract modules *(architect)*
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
- **#2880**: klt sta: SPEF annotation reported complete by name count while parasitics are not applied (delay_changed false)

## Backlog Balance

| Tier | Count |
|------|-------|
| Operator merge-risk holds | 8 |
| Operator priority | 0 |
| Ready (`loom:issue`) | 5 |
| In Progress (`loom:building`) | 0 |
| PRs awaiting review | 0 |
| Approved PRs awaiting merge | 22 |
| Curated | 59 |
| Architect / Hermit proposals | 6 |
| Active epics | 10 |
<!-- guide:plan-body:end -->
