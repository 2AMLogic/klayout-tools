# Work Plan

Current work from GitHub labels. Long-term direction lives in [ROADMAP.md](ROADMAP.md).

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#4783**: chore(deps): bump pyo3 from 0.29.3 to 0.30.1 in /native/mom

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

- **#4161**: klt sim batch: all jobs exit 87 within seconds on the current fleet image; no runner stderr surfaced

## Important

`loom:important`; taken before unlabelled work.

- **#3516**: klt sim batch: runner reported as 'match' still rejects analysis_steps[] requests (recurrence of #3249)
- **#4739**: main CI red: file-size baseline out of sync with source (lvs.py, sim.py, lvs_mismatch.py, netlist_normalize.py, test_lvs.py, test_sim.py)

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#2340**: signoff: input_verified is always null for drc/extract envelopes that recorded an absolute host path
- **#2912**: Friction: sky130 native-Vt NMOS (nfet_05v0_nvt) cannot be drawn by klt gen or compared by klt lvs
- **#3057**: klt sim --backend batch: -o FILE not written when --format json (report goes to stdout)
- **#3094**: decks/sg13cmos5l: poly resistors assume coincident GatPoly/PolyRes, same as sg13g2 before #2679
- **#3440**: gf180mcu MIM option A: live verification against PDK pcell and runset (follow-up to #2920 / PR #3358)
- **#3462**: klt sim batch: no way to require the worker PDK revision match a pin
- **#4161**: klt sim batch: all jobs exit 87 within seconds on the current fleet image; no runner stderr surfaced
- **#4308**: Run composed layout-plan GDS through klt drc --deck gf180mcu to confirm no *.ongrid.1 (follow-up to #4267)
- **#4372**: test flake: CI wall-clock budget breach on ASAP7 DRC deck fixtures (PR #4331, 405s vs 252s)
- **#4495**: place-and-route (asap7): router V1 landing pads fail the deck's M1 tip rules; not reported
- **#4739**: main CI red: file-size baseline out of sync with source (lvs.py, sim.py, lvs_mismatch.py, netlist_normalize.py, test_lvs.py, test_sim.py)
- **#4763**: Live-verify gf180mcu tran run with models.param_file (follow-up to #4759)
- **#4827**: Live-verify #4820 floating_gate_allowed against ASAP7 DFFASRHQNx1

## In Progress

Issues currently being built (`loom:building`).

- **#2835**: sky130 DRC: enforce licon1/mcon maximum size without rejecting compound-layer exceptions
- **#3420**: Re-pin open_pdks to f6eeac7 (ciel) — fleet retires volare c6d73a3
- **#4312**: gf180mcu deck: no eFuse (EF.*) DRC rules and no eFuse device class in the extraction deck
- **#4771**: Friction: klt sim --batch-set --dry-run submits real batch jobs; --wait-for-capacity is refused with --batch-set
- **#4793**: klt signoff: item 6 not_applicable cannot cite a markdown spec table; keyword scan refuses negated mentions
- **#4820**: klt erc: erc.floating_gate on ASAP7 gates wired through LIG/LISD only; no way to model or declare
- **#4821**: Friction: klt sim --batch --resume is not repeatable; second resume refused after the shard map is rewritten for the remainder
- **#4828**: klt erc: no way to declare intentional dummy gates for erc.floating_gate

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#4334**: feat: extract gf180mcu eFuse as a two-terminal device via ResistorDevice.anode_marker
- **#4602**: docs: re-qualify c6d73a3 reference records under f6eeac7 and mark superseded
- **#4737**: feat(drc): enforce sky130 licon1/mcon max size with exemptions
- **#4772**: fix: honor --dry-run and --wait-for-capacity under klt sim --batch-set
- **#4796**: feat: let signoff item 6 not_applicable cite a markdown spec table
- **#4822**: feat(erc): disclosed floating_gate_allowed carve-out for erc.floating_gate
- **#4823**: fix: make sharded batch --resume repeatable across shard-count changes
- **#4829**: feat(erc): add dummy_gate_boxes spec key to excuse intentional dummy gates

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#4783**: chore(deps): bump pyo3 from 0.29.3 to 0.30.1 in /native/mom

## Proposed

Issues carrying `loom:curated`.

- **#2340**: signoff: input_verified is always null for drc/extract envelopes that recorded an absolute host path *(curated)*
- **#2574**: Build + publish the ihp-sg13g2 remote-sim AMI (operator-only; gated on #2573) *(curated)*
- **#2727**: Friction: klt sim's batch/remote backends refuse ihp-sg13cmos5l, so no SG13CMOS5L corner grid can run on a batch-only host *(curated)*
- **#2760**: ASAP7 DRC deck for KLayout *(curated)*
- **#2812**: ASAP7 FEOL/MOL gap tracker: mechanism-specific rule increments *(curated)*
- **#2835**: sky130 DRC: enforce licon1/mcon maximum size without rejecting compound-layer exceptions *(curated)*
- **#2912**: Friction: sky130 native-Vt NMOS (nfet_05v0_nvt) cannot be drawn by klt gen or compared by klt lvs *(curated)*
- **#3057**: klt sim --backend batch: -o FILE not written when --format json (report goes to stdout) *(curated)*
- **#3147**: ASAP7 DRC deck: BEOL rules needing track, net, corner or grid predicates *(curated)*
- **#3312**: ci: add ratcheting per-module coverage floor once report-only data exists *(curated)*
- **#3420**: Re-pin open_pdks to f6eeac7 (ciel) — fleet retires volare c6d73a3 *(curated)*
- **#3462**: klt sim batch: no way to require the worker PDK revision match a pin *(curated)*
- **#3516**: klt sim batch: runner reported as 'match' still rejects analysis_steps[] requests (recurrence of #3249) *(curated)*
- **#3522**: Verify OMP_NUM_THREADS pinning on the batch fleet (follow-up to #3502) *(curated)*
- **#3619**: batch: confirm OMP_NUM_THREADS=1 per worker fixes transient oversubscription on the fleet *(curated)*
- **#3998**: Emit a real prBoundary (235/4) from a chip-level assembly step, the precondition the operator set for boundary-scoped density (#1991) *(curated)*
- **#3999**: Add a metal/poly/diffusion fill insertion verb driven by the PDK's density window *(curated)*
- **#4000**: Seal-ring / scribe conformance check against the PDK's seal-ring rules (ring-check only checks continuity) *(curated)*
- **#4001**: gf180mcu deck: disclose the DRM §9.2 bond-pad guidelines (PAD.1, PAD.2, PAD.5–20) as not evaluated *(curated)*
- **#4002**: Make gf180mcu's native sign-off deck runnable from klt drc --engine klayout (assemble rule_decks + run_drc.py) *(curated)*
- **#4003**: erc: add a gf180mcu antenna-ratio limit table (--pdk gf180mcu), mirroring the sg13g2 table from #3784 *(curated)*
- **#4075**: test flake: Install the klayout binary fails on ASAP7 DRC deck fixtures (PR #4074, recurrence of #4059) *(curated)*
- **#4161**: klt sim batch: all jobs exit 87 within seconds on the current fleet image; no runner stderr surfaced *(curated)*
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
- **#4739**: main CI red: file-size baseline out of sync with source (lvs.py, sim.py, lvs_mismatch.py, netlist_normalize.py, test_lvs.py, test_sim.py) *(curated)*
- **#4763**: Live-verify gf180mcu tran run with models.param_file (follow-up to #4759) *(curated)*
- **#4771**: Friction: klt sim --batch-set --dry-run submits real batch jobs; --wait-for-capacity is refused with --batch-set *(curated)*
- **#4793**: klt signoff: item 6 not_applicable cannot cite a markdown spec table; keyword scan refuses negated mentions *(curated)*
- **#4820**: klt erc: erc.floating_gate on ASAP7 gates wired through LIG/LISD only; no way to model or declare *(curated)*
- **#4821**: Friction: klt sim --batch --resume is not repeatable; second resume refused after the shard map is rewritten for the remainder *(curated)*
- **#4827**: Live-verify #4820 floating_gate_allowed against ASAP7 DFFASRHQNx1 *(curated)*
- **#4828**: klt erc: no way to declare intentional dummy gates for erc.floating_gate *(curated)*

## Proposed (Architect / Hermit)

- **#485**: Live digital fleet validation — measure T_o/t, derive the K-selection rule *(architect)*
- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark *(architect)*
- **#708**: Epic: finite-element full-field solver for klt (Rust) — volumetric fields, electrothermal, MoM cross-validation *(architect)*
- **#3873**: asap7_bsimcmg adapters have no runtime caller: wire into klt sim or demote to test helper *(hermit)*
- **#3962**: Split cli/parser.py: move per-verb parser registration into verb modules (190 commits / 20 conflict merges in 5 weeks) *(hermit)*
- **#4065**: Split sim_batch.py (5232 lines, 117 commits / 44 merges in 5 weeks) along its existing section seams *(hermit)*
- **#4126**: Simplify run_lvs: split the 2,047-line orchestrator in lvs.py into stage helpers *(hermit)*
- **#4233**: Remove 8 passthrough _report_path wrappers over env_provenance.repo_relative_path *(hermit)*
- **#4693**: Remove routing_pitch.py: library-only helper with no callers *(hermit)*

## Epics

- **#2570**: Friction: no ihp-sg13g2 remote-sim AMI, so klt sim's batch backend cannot run a PDK klt fully supports locally

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 1 |
| Very important | 1 |
| Important | 2 |
| Ready (`loom:issue`) | 13 |
| In Progress (`loom:building`) | 8 |
| PRs awaiting review | 8 |
| Approved PRs awaiting merge | 1 |
| Curated | 41 |
| Architect / Hermit proposals | 9 |
| Active epics | 1 |
<!-- guide:plan-body:end -->
