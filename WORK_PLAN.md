# Work Plan

Current work from GitHub labels. Long-term direction lives in [ROADMAP.md](ROADMAP.md).

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#5340**: Bump Repo Skills 0.25.10 -> 0.26.1

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

_None._

## Important

`loom:important`; taken before unlabelled work.

- **#4891**: batch fleet runner's ngspice cannot evaluate sky130A model-library expr() parameters (runner image gap), engine_version still null
- **#5334**: klt erc: evaluate erc.supply_short through the substrate (model substrate continuity) instead of disclosing it as inapplicable
- **#5336**: klt erc: evaluate erc.supply_short through the substrate (phase 2 of #5334, per design note)

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#2340**: signoff: input_verified is always null for drc/extract envelopes that recorded an absolute host path
- **#2912**: Friction: sky130 native-Vt NMOS (nfet_05v0_nvt) cannot be drawn by klt gen or compared by klt lvs
- **#3462**: klt sim batch: no way to require the worker PDK revision match a pin
- **#4308**: Run composed layout-plan GDS through klt drc --deck gf180mcu to confirm no *.ongrid.1 (follow-up to #4267)
- **#4372**: test flake: CI wall-clock budget breach on ASAP7 DRC deck fixtures (PR #4331, 405s vs 252s)
- **#4495**: place-and-route (asap7): router V1 landing pads fail the deck's M1 tip rules; not reported
- **#4827**: Live-verify #4820 floating_gate_allowed against ASAP7 DFFASRHQNx1
- **#4891**: batch fleet runner's ngspice cannot evaluate sky130A model-library expr() parameters (runner image gap), engine_version still null
- **#5196**: Decompose run_extract: ~2,700-line function in extract.py
- **#5322**: lvs: no working GDS-to-verdict route for hierarchical compare of a large regular array (stub flattened, abstract-cell pin labels promoted)

## In Progress

Issues currently being built (`loom:building`).

- **#2835**: sky130 DRC: enforce licon1/mcon maximum size without rejecting compound-layer exceptions
- **#3420**: Re-pin open_pdks to f6eeac7 (ciel) — fleet retires volare c6d73a3
- **#4312**: gf180mcu deck: no eFuse (EF.*) DRC rules and no eFuse device class in the extraction deck
- **#4771**: Friction: klt sim --batch-set --dry-run submits real batch jobs; --wait-for-capacity is refused with --batch-set
- **#5311**: klt extract/lvs: stepped-width shared pFET diffusion matches on 0.9.1 but splits nets on 0.9.2.dev661
- **#5313**: place-and-route: macro-edge congestion control (keep-out independent of the PDN halo, per-channel overflow report)
- **#5317**: klt sim --batch-set crashes with KeyError 'netlist' on deck-list (decks[]) members
- **#5321**: klt sim --backend batch: runner PDK stamp unreadable, so the report cannot establish which PDK revision the runner used
- **#5325**: klt sim batch: report cannot attest the PDK revision of staged models, and the staged model-card sha256 differs from the source file's with no explanation
- **#5345**: erc: declare sg13g2/sg13cmos5l substrate_isolation in extraction decks, phase 2 D of #5336

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#4334**: feat: extract gf180mcu eFuse as a two-terminal device via ResistorDevice.anode_marker
- **#4602**: docs: re-qualify c6d73a3 reference records under f6eeac7 and mark superseded
- **#4737**: feat(drc): enforce sky130 licon1/mcon max size with exemptions
- **#4772**: fix: honor --dry-run and --wait-for-capacity under klt sim --batch-set
- **#5314**: feat: add placement-only macros[].keepout independent of the PDN halo
- **#5323**: feat(sim): report and compare runner's executed models_lib hash in batch reports
- **#5327**: feat: add staged-input transform name and caller-asserted pdk_revision to sim provenance
- **#5349**: feat: declare nBuLay substrate_isolation on sg13g2 and sg13cmos5l decks

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#5340**: Bump Repo Skills 0.25.10 -> 0.26.1

## Proposed

Issues carrying `loom:curated`.

- **#2340**: signoff: input_verified is always null for drc/extract envelopes that recorded an absolute host path *(curated)*
- **#2835**: sky130 DRC: enforce licon1/mcon maximum size without rejecting compound-layer exceptions *(curated)*
- **#2912**: Friction: sky130 native-Vt NMOS (nfet_05v0_nvt) cannot be drawn by klt gen or compared by klt lvs *(curated)*
- **#3147**: ASAP7 DRC deck: BEOL rules needing track, net, corner or grid predicates *(curated)*
- **#3420**: Re-pin open_pdks to f6eeac7 (ciel) — fleet retires volare c6d73a3 *(curated)*
- **#3462**: klt sim batch: no way to require the worker PDK revision match a pin *(curated)*
- **#3522**: Verify OMP_NUM_THREADS pinning on the batch fleet (follow-up to #3502) *(curated)*
- **#3962**: Split cli/parser.py: move per-verb parser registration into verb modules (190 commits / 20 conflict merges in 5 weeks) *(curated)*
- **#4075**: test flake: Install the klayout binary fails on ASAP7 DRC deck fixtures (PR #4074, recurrence of #4059) *(curated)*
- **#4277**: Live-verify klt synthesize sv_frontend=sv2v against a real sv2v (follow-up to #4228) *(curated)*
- **#4308**: Run composed layout-plan GDS through klt drc --deck gf180mcu to confirm no *.ongrid.1 (follow-up to #4267) *(curated)*
- **#4312**: gf180mcu deck: no eFuse (EF.*) DRC rules and no eFuse device class in the extraction deck *(curated)*
- **#4371**: Live-verify klt synthesize sv_frontend=sv2v against a real sv2v binary *(curated)*
- **#4372**: test flake: CI wall-clock budget breach on ASAP7 DRC deck fixtures (PR #4331, 405s vs 252s) *(curated)*
- **#4397**: Live-verify SG13G2 runset recognises npn13G2 (needs KLayout >= 0.29) *(curated)*
- **#4410**: lvs gate-level-verilog + abstract-cell netlist: dangling-output instances still error after #4323 *(curated)*
- **#4424**: klt lvs: custom-class cap_cmomi/resistor LVS regresses from match to mismatch between daf06a51afaf and 0.8.0 (sg13cmos5l) *(curated)*
- **#4495**: place-and-route (asap7): router V1 landing pads fail the deck's M1 tip rules; not reported *(curated)*
- **#4508**: ASAP7: trim protruding VIA_* M1 pad tips to clear M1.S.2-5 (follow-up to #4495) *(curated)*
- **#4771**: Friction: klt sim --batch-set --dry-run submits real batch jobs; --wait-for-capacity is refused with --batch-set *(curated)*
- **#4827**: Live-verify #4820 floating_gate_allowed against ASAP7 DFFASRHQNx1 *(curated)*
- **#4845**: ring-check --pdk sg13g2: cross-check re-implemented Seal.* rules against the real Ruby deck *(curated)*
- **#4902**: klt sim batch backend: derived measurements referencing other measurements (m_<name>) produce no value *(curated)*
- **#5153**: Live-verify klt sim request.adapt on real ASAP7 extract-then-sim (needs lambdapdk + bsimcmg.osdi) *(curated)*
- **#5160**: Extract compare-setup and klayout-engine branch from run_lvs (stage 4/6, after #4126) *(curated)*
- **#5196**: Decompose run_extract: ~2,700-line function in extract.py *(curated)*
- **#5311**: klt extract/lvs: stepped-width shared pFET diffusion matches on 0.9.1 but splits nets on 0.9.2.dev661 *(curated)*
- **#5313**: place-and-route: macro-edge congestion control (keep-out independent of the PDN halo, per-channel overflow report) *(curated)*
- **#5317**: klt sim --batch-set crashes with KeyError 'netlist' on deck-list (decks[]) members *(curated)*
- **#5321**: klt sim --backend batch: runner PDK stamp unreadable, so the report cannot establish which PDK revision the runner used *(curated)*
- **#5322**: lvs: no working GDS-to-verdict route for hierarchical compare of a large regular array (stub flattened, abstract-cell pin labels promoted) *(curated)*
- **#5325**: klt sim batch: report cannot attest the PDK revision of staged models, and the staged model-card sha256 differs from the source file's with no explanation *(curated)*
- **#5345**: erc: declare sg13g2/sg13cmos5l substrate_isolation in extraction decks, phase 2 D of #5336 *(curated)*
- **#5348**: klt characterize: no capacity wait, no re-attach, cells run serially on the batch backend *(curated)*

## Proposed (Architect / Hermit)

- **#485**: Live digital fleet validation — measure T_o/t, derive the K-selection rule *(architect)*
- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark *(architect)*
- **#708**: Epic: finite-element full-field solver for klt (Rust) — volumetric fields, electrothermal, MoM cross-validation *(architect)*

## Epics

- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark
- **#2570**: Friction: no ihp-sg13g2 remote-sim AMI, so klt sim's batch backend cannot run a PDK klt fully supports locally

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 1 |
| Very important | 0 |
| Important | 3 |
| Ready (`loom:issue`) | 10 |
| In Progress (`loom:building`) | 10 |
| PRs awaiting review | 8 |
| Approved PRs awaiting merge | 1 |
| Curated | 34 |
| Architect / Hermit proposals | 3 |
| Active epics | 2 |
<!-- guide:plan-body:end -->
