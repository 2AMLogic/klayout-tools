# Work Plan

Current work from GitHub labels. Long-term direction lives in [ROADMAP.md](ROADMAP.md).

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#3310**: fix(release): guard unpublished klt-*-native extra pins and correct [yield] install docs
- **#3467**: ci: fetch the PDK with ciel pinned to open_pdks f6eeac7
- **#4172**: chore(deps): bump astral-sh/setup-uv from 10.2.0 to 10.3.0 in the github-actions group across 1 directory

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

- **#4161**: klt sim batch: all jobs exit 87 within seconds on the current fleet image; no runner stderr surfaced

## Important

`loom:important`; taken before unlabelled work.

- **#3516**: klt sim batch: runner reported as 'match' still rejects analysis_steps[] requests (recurrence of #3249)

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#2340**: signoff: input_verified is always null for drc/extract envelopes that recorded an absolute host path
- **#2812**: ASAP7 FEOL/MOL gap tracker: mechanism-specific rule increments
- **#2900**: Friction: documented 'klayout-tools[yield]' install path does not resolve (klt-yield-native not on PyPI)
- **#2912**: Friction: sky130 native-Vt NMOS (nfet_05v0_nvt) cannot be drawn by klt gen or compared by klt lvs
- **#3022**: gen cap_array: no parallel-connected multi-unit capacitor and no stacked (two-level) MiM option
- **#3055**: klt sim: no way to center a DC sweep window on a value measured in the same corner, and no ungraded named vector intermediates in measurements[].expr
- **#3057**: klt sim --backend batch: -o FILE not written when --format json (report goes to stdout)
- **#3094**: decks/sg13cmos5l: poly resistors assume coincident GatPoly/PolyRes, same as sg13g2 before #2679
- **#3289**: signoff: generator-command evidence for T1 items 1/2 (klt executes and byte-compares) — needs trust-model ruling
- **#3440**: gf180mcu MIM option A: live verification against PDK pcell and runset (follow-up to #2920 / PR #3358)
- **#3462**: klt sim batch: no way to require the worker PDK revision match a pin
- **#3930**: Live-verify klt place-and-route io.pin_constraints against real OpenROAD (N/E/S/W)
- **#4128**: Trim oversized module docstrings in signoff, place_and_route, erc, size, equiv, pdk_models (2,746 lines of phase/issue narration)
- **#4161**: klt sim batch: all jobs exit 87 within seconds on the current fleet image; no runner stderr surfaced
- **#4308**: Run composed layout-plan GDS through klt drc --deck gf180mcu to confirm no *.ongrid.1 (follow-up to #4267)
- **#4372**: test flake: CI wall-clock budget breach on ASAP7 DRC deck fixtures (PR #4331, 405s vs 252s)
- **#4438**: Friction: klt sim local backend can't load OSDI v0.4 on host ngspice 42, and there's no supported sanity-corner path

## In Progress

Issues currently being built (`loom:building`).

- **#3625**: klt sim: engine_version pin is silently ignored on deck-list (decks) requests
- **#4221**: signoff --attest: support T1 item 2 (Layout) anchored generic envelope
- **#4312**: gf180mcu deck: no eFuse (EF.*) DRC rules and no eFuse device class in the extraction deck
- **#4393**: klt drc: no -o/--output flag to write the report to a file
- **#4436**: Friction: place-and-route route stage is one non-resumable process of 2 h or more, with no progress output
- **#4440**: Friction: klt's OpenROAD steps need Docker, which fleet workers can't use; no fleet executor for OpenROAD regeneration

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#4334**: feat: extract gf180mcu eFuse as a two-terminal device via ResistorDevice.anode_marker
- **#4396**: feat: add -o/--output report file copy to klt drc

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#3310**: fix(release): guard unpublished klt-*-native extra pins and correct [yield] install docs
- **#3467**: ci: fetch the PDK with ciel pinned to open_pdks f6eeac7
- **#4172**: chore(deps): bump astral-sh/setup-uv from 10.2.0 to 10.3.0 in the github-actions group across 1 directory

## Proposed

Issues carrying `loom:curated`.

- **#2340**: signoff: input_verified is always null for drc/extract envelopes that recorded an absolute host path *(curated)*
- **#2574**: Build + publish the ihp-sg13g2 remote-sim AMI (operator-only; gated on #2573) *(curated)*
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
- **#3930**: Live-verify klt place-and-route io.pin_constraints against real OpenROAD (N/E/S/W) *(curated)*
- **#3998**: Emit a real prBoundary (235/4) from a chip-level assembly step, the precondition the operator set for boundary-scoped density (#1991) *(curated)*
- **#3999**: Add a metal/poly/diffusion fill insertion verb driven by the PDK's density window *(curated)*
- **#4000**: Seal-ring / scribe conformance check against the PDK's seal-ring rules (ring-check only checks continuity) *(curated)*
- **#4001**: gf180mcu deck: disclose the DRM §9.2 bond-pad guidelines (PAD.1, PAD.2, PAD.5–20) as not evaluated *(curated)*
- **#4002**: Make gf180mcu's native sign-off deck runnable from klt drc --engine klayout (assemble rule_decks + run_drc.py) *(curated)*
- **#4003**: erc: add a gf180mcu antenna-ratio limit table (--pdk gf180mcu), mirroring the sg13g2 table from #3784 *(curated)*
- **#4075**: test flake: Install the klayout binary fails on ASAP7 DRC deck fixtures (PR #4074, recurrence of #4059) *(curated)*
- **#4128**: Trim oversized module docstrings in signoff, place_and_route, erc, size, equiv, pdk_models (2,746 lines of phase/issue narration) *(curated)*
- **#4161**: klt sim batch: all jobs exit 87 within seconds on the current fleet image; no runner stderr surfaced *(curated)*
- **#4191**: place-and-route: absolute paths in report, silent mount gap, late core_margin error, no pin plan *(curated)*
- **#4221**: signoff --attest: support T1 item 2 (Layout) anchored generic envelope *(curated)*
- **#4276**: klt sta (verilog mode): netlist path in generated Tcl is repo-root-relative, so run fails unless cwd is repo root *(curated)*
- **#4277**: Live-verify klt synthesize sv_frontend=sv2v against a real sv2v (follow-up to #4228) *(curated)*
- **#4308**: Run composed layout-plan GDS through klt drc --deck gf180mcu to confirm no *.ongrid.1 (follow-up to #4267) *(curated)*
- **#4312**: gf180mcu deck: no eFuse (EF.*) DRC rules and no eFuse device class in the extraction deck *(curated)*
- **#4372**: test flake: CI wall-clock budget breach on ASAP7 DRC deck fixtures (PR #4331, 405s vs 252s) *(curated)*
- **#4393**: klt drc: no -o/--output flag to write the report to a file *(curated)*
- **#4410**: lvs gate-level-verilog + abstract-cell netlist: dangling-output instances still error after #4323 *(curated)*
- **#4424**: klt lvs: custom-class cap_cmomi/resistor LVS regresses from match to mismatch between daf06a51afaf and 0.8.0 (sg13cmos5l) *(curated)*
- **#4436**: Friction: place-and-route route stage is one non-resumable process of 2 h or more, with no progress output *(curated)*
- **#4438**: Friction: klt sim local backend can't load OSDI v0.4 on host ngspice 42, and there's no supported sanity-corner path *(curated)*
- **#4440**: Friction: klt's OpenROAD steps need Docker, which fleet workers can't use; no fleet executor for OpenROAD regeneration *(curated)*

## Proposed (Architect / Hermit)

- **#485**: Live digital fleet validation — measure T_o/t, derive the K-selection rule *(architect)*
- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark *(architect)*
- **#708**: Epic: finite-element full-field solver for klt (Rust) — volumetric fields, electrothermal, MoM cross-validation *(architect)*
- **#3873**: asap7_bsimcmg adapters have no runtime caller: wire into klt sim or demote to test helper *(hermit)*
- **#3956**: Trim _paths.py: 90-line refactor-history docstring and a one-line alias *(hermit)*
- **#3962**: Split cli/parser.py: move per-verb parser registration into verb modules (190 commits / 20 conflict merges in 5 weeks) *(hermit)*
- **#4065**: Split sim_batch.py (5232 lines, 117 commits / 44 merges in 5 weeks) along its existing section seams *(hermit)*
- **#4126**: Simplify run_lvs: split the 2,047-line orchestrator in lvs.py into stage helpers *(hermit)*
- **#4233**: Remove 8 passthrough _report_path wrappers over env_provenance.repo_relative_path *(hermit)*

## Epics

- **#2570**: Friction: no ihp-sg13g2 remote-sim AMI, so klt sim's batch backend cannot run a PDK klt fully supports locally

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 3 |
| Very important | 1 |
| Important | 1 |
| Ready (`loom:issue`) | 17 |
| In Progress (`loom:building`) | 6 |
| PRs awaiting review | 2 |
| Approved PRs awaiting merge | 3 |
| Curated | 43 |
| Architect / Hermit proposals | 9 |
| Active epics | 1 |
<!-- guide:plan-body:end -->
