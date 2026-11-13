# Work Plan

Current work from GitHub labels. Long-term direction lives in [ROADMAP.md](ROADMAP.md).

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#5427**: refactor(spice): one shared ngspice inline-comment rule, in a _spice_text module

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

_None._

## Important

`loom:important`; taken before unlabelled work.

- **#4891**: batch fleet runner's ngspice cannot evaluate sky130A model-library expr() parameters (runner image gap), engine_version still null
- **#5334**: klt erc: evaluate erc.supply_short through the substrate (model substrate continuity) instead of disclosing it as inapplicable
- **#5336**: klt erc: evaluate erc.supply_short through the substrate (phase 2 of #5334, per design note)
- **#5346**: signoff: item 11 reads substrate supply_short + live ERC/LVS agreement, phase 2 E of #5336

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#2340**: signoff: input_verified is always null for drc/extract envelopes that recorded an absolute host path
- **#2912**: Friction: sky130 native-Vt NMOS (nfet_05v0_nvt) cannot be drawn by klt gen or compared by klt lvs
- **#3462**: klt sim batch: no way to require the worker PDK revision match a pin
- **#4372**: test flake: CI wall-clock budget breach on ASAP7 DRC deck fixtures (PR #4331, 405s vs 252s)
- **#4495**: place-and-route (asap7): router V1 landing pads fail the deck's M1 tip rules; not reported
- **#4827**: Live-verify #4820 floating_gate_allowed against ASAP7 DFFASRHQNx1
- **#5346**: signoff: item 11 reads substrate supply_short + live ERC/LVS agreement, phase 2 E of #5336

## In Progress

Issues currently being built (`loom:building`).

- **#2835**: sky130 DRC: enforce licon1/mcon maximum size without rejecting compound-layer exceptions
- **#3420**: Re-pin open_pdks to f6eeac7 (ciel) — fleet retires volare c6d73a3
- **#4312**: gf180mcu deck: no eFuse (EF.*) DRC rules and no eFuse device class in the extraction deck
- **#4771**: Friction: klt sim --batch-set --dry-run submits real batch jobs; --wait-for-capacity is refused with --batch-set
- **#5417**: Consolidate six divergent SPICE inline-comment strippers onto one shared helper
- **#5420**: klt mcp serve: expose the request and response schemas in docs/schemas as MCP resources
- **#5428**: Split sim.py (13,773 lines): move the corner-matrix expansion section (~1,215 lines) into four sibling modules, each ≤500 lines
- **#5433**: klt sim: the request's netlist is .include'd into a generated top deck, so a conventional SPICE title line on its line 1 is silently parsed as a netlist element
- **#5435**: drc: a rule whose layer_selection empties its input still grades 'checked', so a clean verdict can name a rule that measured zero geometry
- **#5436**: docs: stale 'shipped decks curate none' cut-resistance docstring in extract_parasitics.py
- **#5439**: klt netlist --block: restored .subckt line omits the top schematic's own parameter block, so the emitted block references undeclared parameters
- **#5442**: drc(sg13g2): a runset_note deviation is documented but still counted, so a stream with the PDK varactor can never grade clean (follow-up to #5212)
- **#5443**: drc --engine klayout: deck content_hash covers only the entry .drc, not the %include chain that carries the rules
- **#5444**: drc --engine klayout: no way to derive a native deck's declared rule-category set from the deck (only from a run), so --expect-rule-categories-file must be hand-written
- **#5446**: sim: a step measurement named after a deck node silently clobbers that node's vector, breaking later .meas cards in the same step
- **#5449**: sim (batch): --dry-run misses the models.pdk_root host-path refusal, and the PDK-ambiguity warning advises a pin batch refuses
- **#5452**: klt signoff cannot read a gzip-compressed klt sim report (--report-out *.json.gz) as manifest evidence
- **#5457**: klt sim batch: a request using per-section corner libraries (no models.lib) can never attest the models the runner executed
- **#5459**: klt batch --resume re-attaches to a terminally-interrupted single-unit job instead of resubmitting (follow-on to #5371)
- **#5460**: signoff --attest --check: drift diff names a check by list index, not by the file path that drifted
- **#5462**: klt signoff item 5: no way to require derived row coverage — an all-asserted item grades identically to a fully tagged one
- **#5463**: signoff --attest 9: record a validation-only cold start and its documented full invocation (documented_argv)

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#4334**: feat: extract gf180mcu eFuse as a two-terminal device via ResistorDevice.anode_marker
- **#4602**: docs: re-qualify c6d73a3 reference records under f6eeac7 and mark superseded
- **#4737**: feat(drc): enforce sky130 licon1/mcon max size with exemptions
- **#4772**: fix: honor --dry-run and --wait-for-capacity under klt sim --batch-set
- **#5425**: feat(mcp): serve docs/schemas as MCP resources over klt mcp serve
- **#5434**: refactor(sim): split corner-matrix expansion into four sibling modules
- **#5447**: fix(drc): grade a selection-emptied rule inapplicable, not checked
- **#5448**: fix(sim): classify a title line on the request's netlist as netlist_title_line
- **#5450**: feat(netlist): restore the cell's subcircuit parameters on --block's .subckt card
- **#5451**: fix(sim): refuse a step measurement whose name an earlier sibling shadows
- **#5453**: feat(drc): model sg13g2 NW.c's text-recognised SVaricap carve-out
- **#5454**: feat(drc): derive a native deck's rule-category set via --list-rule-categories
- **#5455**: fix(sim): apply the batch pdk_root refusal in --dry-run and scope its warning
- **#5465**: feat: attest runner-side corner-section libraries in batch sim reports

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#5427**: refactor(spice): one shared ngspice inline-comment rule, in a _spice_text module
- **#5440**: docs: attribute --resistance-graph-r-cut to unconsumed deck cuts, not absent ones

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
- **#5311**: klt extract/lvs: stepped-width shared pFET diffusion matches on 0.9.1 but splits nets on 0.9.2.dev661 *(curated)*
- **#5313**: place-and-route: macro-edge congestion control (keep-out independent of the PDN halo, per-channel overflow report) *(curated)*
- **#5346**: signoff: item 11 reads substrate supply_short + live ERC/LVS agreement, phase 2 E of #5336 *(curated)*
- **#5405**: Live-verify klayout-runset DEVICE_LIST pass-through against IHP sg13g2.lvs (follow-up to #5402) *(curated)*
- **#5406**: bug(lvs): klayout-runset subckt-call pass-through ignores IHP sg13g2 (no DEVICE_LIST); X inductor/svaricap still refused *(curated)*
- **#5420**: klt mcp serve: expose the request and response schemas in docs/schemas as MCP resources *(curated)*
- **#5428**: Split sim.py (13,773 lines): move the corner-matrix expansion section (~1,215 lines) into four sibling modules, each ≤500 lines *(curated)*
- **#5433**: klt sim: the request's netlist is .include'd into a generated top deck, so a conventional SPICE title line on its line 1 is silently parsed as a netlist element *(curated)*
- **#5435**: drc: a rule whose layer_selection empties its input still grades 'checked', so a clean verdict can name a rule that measured zero geometry *(curated)*
- **#5436**: docs: stale 'shipped decks curate none' cut-resistance docstring in extract_parasitics.py *(curated)*
- **#5439**: klt netlist --block: restored .subckt line omits the top schematic's own parameter block, so the emitted block references undeclared parameters *(curated)*
- **#5442**: drc(sg13g2): a runset_note deviation is documented but still counted, so a stream with the PDK varactor can never grade clean (follow-up to #5212) *(curated)*
- **#5443**: drc --engine klayout: deck content_hash covers only the entry .drc, not the %include chain that carries the rules *(curated)*
- **#5444**: drc --engine klayout: no way to derive a native deck's declared rule-category set from the deck (only from a run), so --expect-rule-categories-file must be hand-written *(curated)*
- **#5446**: sim: a step measurement named after a deck node silently clobbers that node's vector, breaking later .meas cards in the same step *(curated)*
- **#5449**: sim (batch): --dry-run misses the models.pdk_root host-path refusal, and the PDK-ambiguity warning advises a pin batch refuses *(curated)*
- **#5452**: klt signoff cannot read a gzip-compressed klt sim report (--report-out *.json.gz) as manifest evidence *(curated)*
- **#5457**: klt sim batch: a request using per-section corner libraries (no models.lib) can never attest the models the runner executed *(curated)*
- **#5459**: klt batch --resume re-attaches to a terminally-interrupted single-unit job instead of resubmitting (follow-on to #5371) *(curated)*
- **#5460**: signoff --attest --check: drift diff names a check by list index, not by the file path that drifted *(curated)*
- **#5462**: klt signoff item 5: no way to require derived row coverage — an all-asserted item grades identically to a fully tagged one *(curated)*
- **#5463**: signoff --attest 9: record a validation-only cold start and its documented full invocation (documented_argv) *(curated)*
- **#5464**: klt pex: add --dry-run (validate the invocation and testbench requests without extracting or simulating) *(curated)*

## Proposed (Architect / Hermit)

- **#485**: Live digital fleet validation — measure T_o/t, derive the K-selection rule *(architect)*
- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark *(architect)*
- **#708**: Epic: finite-element full-field solver for klt (Rust) — volumetric fields, electrothermal, MoM cross-validation *(architect)*
- **#5461**: Consolidate three SPICE card tokenizers onto spice_flatten's: pex and sim_mismatch_gate copies misparse spaced and parenthesised parameters *(hermit)*

## Epics

- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark
- **#2570**: Friction: no ihp-sg13g2 remote-sim AMI, so klt sim's batch backend cannot run a PDK klt fully supports locally

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 1 |
| Very important | 0 |
| Important | 4 |
| Ready (`loom:issue`) | 7 |
| In Progress (`loom:building`) | 22 |
| PRs awaiting review | 14 |
| Approved PRs awaiting merge | 2 |
| Curated | 47 |
| Architect / Hermit proposals | 4 |
| Active epics | 2 |
<!-- guide:plan-body:end -->
