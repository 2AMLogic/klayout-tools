# Work Plan

Current work from GitHub labels. Long-term direction lives in [ROADMAP.md](ROADMAP.md).

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs stuck under a `loom:operator` merge-risk hold — implementation work is done, only a human merge decision is missing.

- **#2645**: feat(synthesize,sta,place-and-route): shared request.macros hard-macro declaration
- **#2701**: feat(drc,extract,lvs): emit echoed input paths as the portable {path, scope} envelope
- **#2711**: feat(loom): frozen-timeline guard in claim-staleness.sh + pin (#1966, PR 1 of 2)
- **#2795**: drc: model sg13g2 Cnt.c activ_mask join and square-contact selection
- **#2801**: feat(drc): opt-in required containment for cuts; sg13cmos5l via-stack coverage

## Operator Priority

Issues the operator starred (`loom:operator-priority`); land these first.

_None._

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#1966**: Re-evaluate the .loom/resync-ignore pin on curator.md (#668) — it may be freezing this repo against upstream Curator fixes
- **#2340**: signoff: input_verified is always null for drc/extract envelopes that recorded an absolute host path
- **#2437**: Update pinned dependency fingerprint tooling from Loom upstream while preserving local guards
- **#2482**: Friction: a klt sim request runs one analysis per corner, so a figure defined across several solves in one corner cannot reach any backend
- **#2620**: Stale-verdict reconciliation cleared a fresh approval whose head SHA had not moved (PR #2571)
- **#2675**: yield-campaign: exercise native pipeline in CI and stub ngspice discovery
- **#2704**: Design spike: establish sound paired-instance pin anchoring for black-box LVS
- **#2753**: Allow sampled ERC walk attribution without a full profiling run
- **#2780**: sta: report per-corner constraint completeness and unclocked registers

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
- **#2801**: feat(drc): opt-in required containment for cuts; sg13cmos5l via-stack coverage

## Proposed

Issues carrying `loom:curated`.

- **#1519**: MoM increment (iii): mixed-axis winding geometry and series-path solves *(curated)*
- **#1779**: Epic: ship unscored sky130 VCO/PLL variants alongside behavioral benchmark references *(curated)*
- **#1966**: Re-evaluate the .loom/resync-ignore pin on curator.md (#668) — it may be freezing this repo against upstream Curator fixes *(curated)*
- **#2093**: The tools klt orchestrates have silent-failure modes: a catalogue with evidence, for workaround-or-report decisions *(curated)*
- **#2281**: Track synthesis memory-mapping reporting and the observability design spike *(curated)*
- **#2340**: signoff: input_verified is always null for drc/extract envelopes that recorded an absolute host path *(curated)*
- **#2437**: Update pinned dependency fingerprint tooling from Loom upstream while preserving local guards *(curated)*
- **#2458**: klt extract --parasitics sums a net's per-level lumped R in series, so via-stitched strapping raises the reported resistance instead of lowering it *(curated)*
- **#2482**: Friction: a klt sim request runs one analysis per corner, so a figure defined across several solves in one corner cannot reach any backend *(curated)*
- **#2490**: sim: document that monte_carlo.seed is reproducible per engine build, not across them -- ngspice 42 vs 46 draw differently from the same seed *(curated)*
- **#2501**: Document champion-epic pin drift and prepare the upstream resync handoff *(curated)*
- **#2574**: Build + publish the ihp-sg13g2 remote-sim AMI (operator-only; gated on #2573) *(curated)*
- **#2620**: Stale-verdict reconciliation cleared a fresh approval whose head SHA had not moved (PR #2571) *(curated)*
- **#2635**: synthesize / sta / place-and-route: no way to declare a hard macro (LEF + liberty + GDS) in any request schema *(curated)*
- **#2659**: drc/extract responses embed the producing host's absolute input path; signoff's input_verified gate cannot re-verify them from another checkout *(curated)*
- **#2675**: yield-campaign: exercise native pipeline in CI and stub ngspice discovery *(curated)*
- **#2679**: decks/sg13g2: poly-resistor body/marker are inverted vs the PDK's own PCell convention, so no drawn rsil/rppd/rhigh is ever recognized *(curated)*
- **#2688**: drc: curated sg13g2 Cnt.c drops the activ_mask join, so every HBT contact false-positives *(curated)*
- **#2704**: Design spike: establish sound paired-instance pin anchoring for black-box LVS *(curated)*
- **#2720**: Repoint SG13CMOS5L PDK fetch to IHP-Open-PDK (the ihp-sg13cmos5l repo is archived) *(curated)*
- **#2722**: Friction: klt extract has no hierarchical (per-cell subcircuit) output, so per-circuit LVS options cannot scope a composed layout *(curated)*
- **#2724**: klt sim: no AC measurement over a two-node complex ratio (loop gain / phase margin) *(curated)*
- **#2725**: klt sim: no first-class design-parameter axis (and no per-unit .nodeset); supply_v alter is the only hook *(curated)*
- **#2726**: klt drc: a via with zero lower-metal overlap passes clean -- no required-coverage check for single-population cut layers (sg13cmos5l Via1/Metal1) *(curated)*
- **#2730**: Extend retarded MoM impedance to oriented winding segments *(curated)*
- **#2734**: Epic: STA completeness, P&R density coverage, repair validation and iteration cost *(curated)*
- **#2736**: design-agent: add an unscored sky130 analog charge-pump PLL alongside the behavioral reference *(curated)*
- **#2739**: klt sta times a routed post-CTS DEF with an ideal clock, so boundary hold checks miss the clock-tree insertion delay *(curated)*
- **#2740**: Friction: klt sta I/O delays are one scalar for all ports (no per-port, no min/max) and no worst-path identity, so a block-to-block boundary check cannot be expressed *(curated)*
- **#2744**: design-agent: assemble and validate the unscored sky130 device charge-pump PLL *(curated)*
- **#2746**: Design spike: define a reproducible synthesis observability negative control *(curated)*
- **#2751**: klt erc: active_layer gate-area intersection is O(nets x active polygons) and dominates runtime on dense layouts *(curated)*
- **#2753**: Allow sampled ERC walk attribution without a full profiling run *(curated)*
- **#2760**: ASAP7 DRC deck for KLayout *(curated)*
- **#2761**: ASAP7 LVS / FinFET device extraction (nfin) *(curated)*
- **#2764**: pex: per-net parasitic RC network integrity report (opens, islands, short candidates, bad R, C delta) *(curated)*
- **#2765**: pex: point-to-point resistance query over a net's RC network *(curated)*
- **#2766**: ci: monotonic per-file size ratchet checked against the merge-base *(curated)*
- **#2767**: ci: enforce verb-module import boundaries statically *(curated)*
- **#2768**: drc: list deck rules and run a selected rule subset *(curated)*
- **#2769**: lef-abstract: structural self-check of emitted LEF *(curated)*
- **#2770**: Output-directory lock so concurrent agent runs can't interleave *(curated)*
- **#2780**: sta: report per-corner constraint completeness and unclocked registers *(curated)*
- **#2781**: place-and-route: declare metal-fill and density-check coverage *(curated)*
- **#2782**: Design spike: verify corner-aware slew and capacitance repair behavior *(curated)*
- **#2783**: Design spike: measure P&R iteration cost and specify safe checkpoint reuse *(curated)*
- **#2804**: klt sim: dispatch already-composed decks to the remote/batch backends (re-filing of #2464) *(curated)*
- **#2810**: ASAP7 DRC deck phase 2: BEOL M1-M9 / V1-V9 and remaining V0 rules *(curated)*

## Proposed (Architect / Hermit)

- **#485**: Live digital fleet validation — measure T_o/t, derive the K-selection rule *(architect)*
- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark *(architect)*
- **#708**: Epic: finite-element full-field solver for klt (Rust) — volumetric fields, electrothermal, MoM cross-validation *(architect)*
- **#2252**: Epic: layout-tier flow-comparison benchmark — draw-only vs generator-first vs generator+refine (AHRR, ICCAD'26) *(architect)*
- **#2831**: Contracts: ship JSON response schemas for the core verbs (layers, cells, stats, drc, lvs) with conformance tests *(architect)*
- **#2419**: Split extract.py's passive-device region-resolution subsystem (~615 lines) into extract_passive_regions.py *(hermit)*
- **#2842**: Remove or wire up digital_fleet.py: ~1,850 LOC with no production caller *(hermit)*

## Epics

- **#1519**: MoM increment (iii): mixed-axis winding geometry and series-path solves
- **#1779**: Epic: ship unscored sky130 VCO/PLL variants alongside behavioral benchmark references
- **#2281**: Track synthesis memory-mapping reporting and the observability design spike
- **#2351**: gf180mcu curated DRC deck checks no implant or tap rule (NP/PP, DF.12, DF.13/14) and CO.1 only as a minimum, and --engine klayout cannot resolve the gf180mcu split-table runset
- **#2570**: Friction: no ihp-sg13g2 remote-sim AMI, so klt sim's batch backend cannot run a PDK klt fully supports locally
- **#2724**: klt sim: no AC measurement over a two-node complex ratio (loop gain / phase margin)
- **#2734**: Epic: STA completeness, P&R density coverage, repair validation and iteration cost
- **#2736**: design-agent: add an unscored sky130 analog charge-pump PLL alongside the behavioral reference

## Backlog Balance

| Tier | Count |
|------|-------|
| Operator merge-risk holds | 5 |
| Operator priority | 0 |
| Ready (`loom:issue`) | 9 |
| In Progress (`loom:building`) | 0 |
| PRs awaiting review | 0 |
| Approved PRs awaiting merge | 5 |
| Curated | 48 |
| Architect / Hermit proposals | 7 |
| Active epics | 8 |
<!-- guide:plan-body:end -->
