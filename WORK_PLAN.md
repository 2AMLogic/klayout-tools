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

## In Progress

Issues currently being built (`loom:building`).

- **#2835**: sky130 DRC: enforce licon1/mcon maximum size without rejecting compound-layer exceptions
- **#3420**: Re-pin open_pdks to f6eeac7 (ciel) — fleet retires volare c6d73a3
- **#4312**: gf180mcu deck: no eFuse (EF.*) DRC rules and no eFuse device class in the extraction deck
- **#4771**: Friction: klt sim --batch-set --dry-run submits real batch jobs; --wait-for-capacity is refused with --batch-set
- **#5346**: signoff: item 11 reads substrate supply_short + live ERC/LVS agreement, phase 2 E of #5336
- **#5417**: Consolidate six divergent SPICE inline-comment strippers onto one shared helper
- **#5420**: klt mcp serve: expose the request and response schemas in docs/schemas as MCP resources
- **#5452**: klt signoff cannot read a gzip-compressed klt sim report (--report-out *.json.gz) as manifest evidence
- **#5457**: klt sim batch: a request using per-section corner libraries (no models.lib) can never attest the models the runner executed
- **#5460**: signoff --attest --check: drift diff names a check by list index, not by the file path that drifted
- **#5461**: Consolidate three SPICE card tokenizers onto spice_flatten's: pex and sim_mismatch_gate copies misparse spaced and parenthesised parameters
- **#5463**: signoff --attest 9: record a validation-only cold start and its documented full invocation (documented_argv)
- **#5481**: extract: add a klayout-runset engine so a device class only the PDK runset recognises can reach an extracted netlist
- **#5486**: klt sim monte_carlo: mismatch_scale and instances are curated to two PDK families, so a third family can neither scale nor gate per-instance mismatch
- **#5487**: klt sim monte_carlo.instances: sg13g2 gates per-instance mismatch by the mm_ok instance parameter, which the switch-shadowing wrapper cannot reach
- **#5490**: klt signoff --manifest item 6: a klt yield citation with no target_yield (status "reported") contributes no row coverage, so an honestly-unratified row reads as uncovered
- **#5491**: extract/pex: sg13g2 antenna diodes are written as D cards naming a model the PDK defines only as a .subckt
- **#5492**: sim: runner-resolved options.osdi_preload entries are dropped when a host-default off-host backend yields to local after the OSDI list is settled
- **#5494**: klt pex: multi-top mode so one graded run can extract every drawn cell a link testbench instantiates
- **#5512**: tests: _stub_subprocess_run in test_sim.py assumes every subprocess.run is an ngspice call
- **#5518**: klt sim: no measurement form for a local-slope-band contiguous run over a swept waveform
- **#5519**: klt characterize: --help, docs and module docstring still say 'combinational arcs only' after clock-to-output and setup/hold shipped
- **#5520**: klt characterize: async set/reset arcs (preset/clear propagation, recovery/removal) for sequential cells
- **#5521**: klt characterize: level-sensitive latch arcs (transparent D->Q, enable->Q, setup/hold on the closing edge)
- **#5522**: klt sim: a split batch request reports its jobs under remote.fleet as a list, so a single-job reader silently loses job identity and runner_compatibility
- **#5527**: No way to re-pin a block manifest's attestation content_hash after an inventory change; stale_evidence does not say what is stale

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#4334**: feat: extract gf180mcu eFuse as a two-terminal device via ResistorDevice.anode_marker
- **#4602**: docs: re-qualify c6d73a3 reference records under f6eeac7 and mark superseded
- **#4737**: feat(drc): enforce sky130 licon1/mcon max size with exemptions
- **#4772**: fix: honor --dry-run and --wait-for-capacity under klt sim --batch-set
- **#5465**: feat: attest runner-side corner-section libraries in batch sim reports
- **#5469**: feat: name the drifted check in signoff --attest --check drift entries
- **#5470**: feat(signoff): record a validation-only item-9 cold start as documented_argv
- **#5496**: feat: add a klayout-runset engine to klt extract
- **#5498**: feat(sim): gate sg13g2 per-instance mismatch by the mm_ok instance parameter
- **#5504**: feat(sim): scale sg13g2 MOS mismatch sigmas read from the model libraries
- **#5506**: feat(extract): bind sg13g2 antenna diodes to their PDK subcircuits
- **#5507**: refactor(spice): one shared card tokenizer, understanding spaced and parenthesised params
- **#5523**: feat(pex): extract every --top into one combined netlist for a both-ends-extracted leg
- **#5526**: docs: record the live sg13g2-ldo ERC/LVS substrate agreement run
- **#5528**: feat: add measurements[].slope_band, a dc local-slope-band run reduction
- **#5529**: feat: roll job_ids and a worst-of runner_compatibility up to a sharded batch remote block
- **#5532**: feat: characterize level-sensitive latch arcs from cell.enable_pin

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#5427**: refactor(spice): one shared ngspice inline-comment rule, in a _spice_text module

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
- **#5452**: klt signoff cannot read a gzip-compressed klt sim report (--report-out *.json.gz) as manifest evidence *(curated)*
- **#5457**: klt sim batch: a request using per-section corner libraries (no models.lib) can never attest the models the runner executed *(curated)*
- **#5460**: signoff --attest --check: drift diff names a check by list index, not by the file path that drifted *(curated)*
- **#5463**: signoff --attest 9: record a validation-only cold start and its documented full invocation (documented_argv) *(curated)*
- **#5481**: extract: add a klayout-runset engine so a device class only the PDK runset recognises can reach an extracted netlist *(curated)*
- **#5486**: klt sim monte_carlo: mismatch_scale and instances are curated to two PDK families, so a third family can neither scale nor gate per-instance mismatch *(curated)*
- **#5487**: klt sim monte_carlo.instances: sg13g2 gates per-instance mismatch by the mm_ok instance parameter, which the switch-shadowing wrapper cannot reach *(curated)*
- **#5490**: klt signoff --manifest item 6: a klt yield citation with no target_yield (status "reported") contributes no row coverage, so an honestly-unratified row reads as uncovered *(curated)*
- **#5491**: extract/pex: sg13g2 antenna diodes are written as D cards naming a model the PDK defines only as a .subckt *(curated)*
- **#5492**: sim: runner-resolved options.osdi_preload entries are dropped when a host-default off-host backend yields to local after the OSDI list is settled *(curated)*
- **#5494**: klt pex: multi-top mode so one graded run can extract every drawn cell a link testbench instantiates *(curated)*
- **#5512**: tests: _stub_subprocess_run in test_sim.py assumes every subprocess.run is an ngspice call *(curated)*
- **#5518**: klt sim: no measurement form for a local-slope-band contiguous run over a swept waveform *(curated)*
- **#5519**: klt characterize: --help, docs and module docstring still say 'combinational arcs only' after clock-to-output and setup/hold shipped *(curated)*
- **#5520**: klt characterize: async set/reset arcs (preset/clear propagation, recovery/removal) for sequential cells *(curated)*
- **#5521**: klt characterize: level-sensitive latch arcs (transparent D->Q, enable->Q, setup/hold on the closing edge) *(curated)*
- **#5522**: klt sim: a split batch request reports its jobs under remote.fleet as a list, so a single-job reader silently loses job identity and runner_compatibility *(curated)*
- **#5527**: No way to re-pin a block manifest's attestation content_hash after an inventory change; stale_evidence does not say what is stale *(curated)*

## Proposed (Architect / Hermit)

- **#485**: Live digital fleet validation — measure T_o/t, derive the K-selection rule *(architect)*
- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark *(architect)*
- **#708**: Epic: finite-element full-field solver for klt (Rust) — volumetric fields, electrothermal, MoM cross-validation *(architect)*
- **#5514**: Remove 83 dead re-exports from post-split compat shims (sim_batch, lvs, functional_verification, extract, +6) *(hermit)*

## Epics

- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark
- **#708**: Epic: finite-element full-field solver for klt (Rust) — volumetric fields, electrothermal, MoM cross-validation
- **#2570**: Friction: no ihp-sg13g2 remote-sim AMI, so klt sim's batch backend cannot run a PDK klt fully supports locally

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 1 |
| Very important | 0 |
| Important | 4 |
| Ready (`loom:issue`) | 6 |
| In Progress (`loom:building`) | 26 |
| PRs awaiting review | 17 |
| Approved PRs awaiting merge | 1 |
| Curated | 48 |
| Architect / Hermit proposals | 4 |
| Active epics | 3 |
<!-- guide:plan-body:end -->
