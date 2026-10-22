# Work Plan

Current work from GitHub labels. Long-term direction lives in [ROADMAP.md](ROADMAP.md).

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#2645**: feat(synthesize,sta,place-and-route): shared request.macros hard-macro declaration
- **#2701**: feat(drc,extract,lvs): emit echoed input paths as the portable {path, scope} envelope
- **#3310**: fix(release): guard unpublished klt-*-native extra pins and correct [yield] install docs
- **#3467**: ci: fetch the PDK with ciel pinned to open_pdks f6eeac7
- **#3903**: test: add live OpenROAD klt sta tests for write_spef NAME_MAP and driving cell/load
- **#3913**: feat(benchmark): add report-layout for ledger-only layout comparison reports

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

_None._

## Important

`loom:important`; taken before unlabelled work.

- **#3516**: klt sim batch: runner reported as 'match' still rejects analysis_steps[] requests (recurrence of #3249)

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#2340**: signoff: input_verified is always null for drc/extract envelopes that recorded an absolute host path
- **#2812**: ASAP7 FEOL/MOL gap tracker: mechanism-specific rule increments
- **#2900**: Friction: documented 'klayout-tools[yield]' install path does not resolve (klt-yield-native not on PyPI)
- **#3022**: gen cap_array: no parallel-connected multi-unit capacitor and no stacked (two-level) MiM option
- **#3055**: klt sim: no way to center a DC sweep window on a value measured in the same corner, and no ungraded named vector intermediates in measurements[].expr
- **#3057**: klt sim --backend batch: -o FILE not written when --format json (report goes to stdout)
- **#3094**: decks/sg13cmos5l: poly resistors assume coincident GatPoly/PolyRes, same as sg13g2 before #2679
- **#3289**: signoff: generator-command evidence for T1 items 1/2 (klt executes and byte-compares) — needs trust-model ruling
- **#3440**: gf180mcu MIM option A: live verification against PDK pcell and runset (follow-up to #2920 / PR #3358)
- **#3462**: klt sim batch: no way to require the worker PDK revision match a pin
- **#3805**: release: `klt signoff --attest` (#2844) is not in any PyPI release; latest 0.7.0 rejects it

## In Progress

Issues currently being built (`loom:building`).

- **#2912**: Friction: sky130 native-Vt NMOS (nfet_05v0_nvt) cannot be drawn by klt gen or compared by klt lvs
- **#3027**: Layout-flow benchmark phase 4: comparison reporting and bounded CI
- **#3625**: klt sim: engine_version pin is silently ignored on deck-list (decks) requests
- **#3776**: Live-verify klt sta set_driving_cell/set_load syntax and *NAME_MAP SPEF annotation against OpenROAD/OpenSTA
- **#4037**: klt pex: cannot consume the terminal-aware resistance graph (selected_resistance_ohm) from klt extract

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

_None._

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#2645**: feat(synthesize,sta,place-and-route): shared request.macros hard-macro declaration
- **#2701**: feat(drc,extract,lvs): emit echoed input paths as the portable {path, scope} envelope
- **#3310**: fix(release): guard unpublished klt-*-native extra pins and correct [yield] install docs
- **#3467**: ci: fetch the PDK with ciel pinned to open_pdks f6eeac7
- **#3903**: test: add live OpenROAD klt sta tests for write_spef NAME_MAP and driving cell/load
- **#3913**: feat(benchmark): add report-layout for ledger-only layout comparison reports

## Proposed

Issues carrying `loom:curated`.

- **#2340**: signoff: input_verified is always null for drc/extract envelopes that recorded an absolute host path *(curated)*
- **#2574**: Build + publish the ihp-sg13g2 remote-sim AMI (operator-only; gated on #2573) *(curated)*
- **#2635**: synthesize / sta / place-and-route: no way to declare a hard macro (LEF + liberty + GDS) in any request schema *(curated)*
- **#2659**: drc/extract responses embed the producing host's absolute input path; signoff's input_verified gate cannot re-verify them from another checkout *(curated)*
- **#2727**: Friction: klt sim's batch/remote backends refuse ihp-sg13cmos5l, so no SG13CMOS5L corner grid can run on a batch-only host *(curated)*
- **#2760**: ASAP7 DRC deck for KLayout *(curated)*
- **#2766**: ci: monotonic per-file size ratchet checked against the merge-base *(curated)*
- **#2812**: ASAP7 FEOL/MOL gap tracker: mechanism-specific rule increments *(curated)*
- **#2835**: sky130 DRC: enforce licon1/mcon maximum size without rejecting compound-layer exceptions *(curated)*
- **#2840**: Friction: klt yield reports carry no seed / corner-matrix / spec-row provenance for T1 item 6's recorded-seed requirement *(curated)*
- **#2900**: Friction: documented 'klayout-tools[yield]' install path does not resolve (klt-yield-native not on PyPI) *(curated)*
- **#2912**: Friction: sky130 native-Vt NMOS (nfet_05v0_nvt) cannot be drawn by klt gen or compared by klt lvs *(curated)*
- **#3022**: gen cap_array: no parallel-connected multi-unit capacitor and no stacked (two-level) MiM option *(curated)*
- **#3055**: klt sim: no way to center a DC sweep window on a value measured in the same corner, and no ungraded named vector intermediates in measurements[].expr *(curated)*
- **#3057**: klt sim --backend batch: -o FILE not written when --format json (report goes to stdout) *(curated)*
- **#3147**: ASAP7 DRC deck: BEOL rules needing track, net, corner or grid predicates *(curated)*
- **#3289**: signoff: generator-command evidence for T1 items 1/2 (klt executes and byte-compares) — needs trust-model ruling *(curated)*
- **#3426**: Re-pin CI, AMI script and install docs to open_pdks f6eeac7 via ciel 2.6.1 *(curated)*
- **#3462**: klt sim batch: no way to require the worker PDK revision match a pin *(curated)*
- **#3516**: klt sim batch: runner reported as 'match' still rejects analysis_steps[] requests (recurrence of #3249) *(curated)*
- **#3522**: Verify OMP_NUM_THREADS pinning on the batch fleet (follow-up to #3502) *(curated)*
- **#3625**: klt sim: engine_version pin is silently ignored on deck-list (decks) requests *(curated)*
- **#3776**: Live-verify klt sta set_driving_cell/set_load syntax and *NAME_MAP SPEF annotation against OpenROAD/OpenSTA *(curated)*
- **#3805**: release: `klt signoff --attest` (#2844) is not in any PyPI release; latest 0.7.0 rejects it *(curated)*
- **#3930**: Live-verify klt place-and-route io.pin_constraints against real OpenROAD (N/E/S/W) *(curated)*
- **#3998**: Emit a real prBoundary (235/4) from a chip-level assembly step, the precondition the operator set for boundary-scoped density (#1991) *(curated)*
- **#3999**: Add a metal/poly/diffusion fill insertion verb driven by the PDK's density window *(curated)*
- **#4000**: Seal-ring / scribe conformance check against the PDK's seal-ring rules (ring-check only checks continuity) *(curated)*
- **#4001**: gf180mcu deck: disclose the DRM §9.2 bond-pad guidelines (PAD.1, PAD.2, PAD.5–20) as not evaluated *(curated)*
- **#4002**: Make gf180mcu's native sign-off deck runnable from klt drc --engine klayout (assemble rule_decks + run_drc.py) *(curated)*
- **#4003**: erc: add a gf180mcu antenna-ratio limit table (--pdk gf180mcu), mirroring the sg13g2 table from #3784 *(curated)*
- **#4037**: klt pex: cannot consume the terminal-aware resistance graph (selected_resistance_ohm) from klt extract *(curated)*

## Proposed (Architect / Hermit)

- **#485**: Live digital fleet validation — measure T_o/t, derive the K-selection rule *(architect)*
- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark *(architect)*
- **#708**: Epic: finite-element full-field solver for klt (Rust) — volumetric fields, electrothermal, MoM cross-validation *(architect)*
- **#3873**: asap7_bsimcmg adapters have no runtime caller: wire into klt sim or demote to test helper *(hermit)*
- **#3956**: Trim _paths.py: 90-line refactor-history docstring and a one-line alias *(hermit)*
- **#3962**: Split cli/parser.py: move per-verb parser registration into verb modules (190 commits / 20 conflict merges in 5 weeks) *(hermit)*

## Epics

- **#2252**: Epic: layout-tier flow-comparison benchmark — draw-only vs generator-first vs generator+refine (AHRR, ICCAD'26)
- **#2570**: Friction: no ihp-sg13g2 remote-sim AMI, so klt sim's batch backend cannot run a PDK klt fully supports locally

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 6 |
| Very important | 0 |
| Important | 1 |
| Ready (`loom:issue`) | 11 |
| In Progress (`loom:building`) | 5 |
| PRs awaiting review | 0 |
| Approved PRs awaiting merge | 6 |
| Curated | 32 |
| Architect / Hermit proposals | 6 |
| Active epics | 2 |
<!-- guide:plan-body:end -->
