# Work Plan

Current work from GitHub labels. Long-term direction lives in [ROADMAP.md](ROADMAP.md).

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#6254**: fix: kill the openroad docker container when the wrapper dies; record launch-time engine evidence
- **#6962**: chore(release): bump version to 0.10.0
- **#7047**: fix(release): gate PR setup pre-push, recover pushed branch, report failed runs
- **#7547**: build(deps): bump docker/login-action from 4.7.0 to 4.8.0 in the github-actions group across 1 directory

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

- **#6897**: Publish the v0.10.0 bridge release for consumers pinned to PyPI

## Important

`loom:important`; taken before unlabelled work.

- **#4891**: batch fleet runner's ngspice cannot evaluate sky130A model-library expr() parameters (runner image gap), engine_version still null
- **#5322**: lvs: no working GDS-to-verdict route for hierarchical compare of a large regular array (stub flattened, abstract-cell pin labels promoted)
- **#6898**: place-and-route: no thread-count control, so OpenROAD's detailed route runs on one core whatever the host has and a large block cannot finish inside any executor cap
- **#6902**: signoff: a matching sky130 klayout-runset LVS report can never grade item 4 or 11 met (zero-instance declared classes, case-different census, sky130.lvs not pinned)
- **#7799**: Repair remaining ASAP7 M1 pad-to-neighbor tip spacings on a routed slice

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#2835**: sky130 DRC: enforce licon1/mcon maximum size without rejecting compound-layer exceptions
- **#5494**: klt pex: multi-top mode so one graded run can extract every drawn cell a link testbench instantiates
- **#5630**: lvs: SIGSEGV (exit 139, no output) on a routed ASAP7 block under the documented #4610 recipe; each half of the recipe completes
- **#5730**: klt signoff --check reports 'drifted' for a provenance schema change, indistinguishable from real evidence drift
- **#5796**: klt lvs (klayout-runset): no device map for subckt-call references — reference side converts ~nothing, silently
- **#5829**: signoff attest envelopes: provenance.input shape changed incompatibly under an unchanged schema_version; no migration path or version-tolerant --check
- **#5945**: klt signoff --attest: evidence records that git can merge (one line per covered file, no committed aggregate), so concurrent chip PRs stop conflicting
- **#6389**: sim_validate._validate_noise_measurement:155 still hand-lists 7 of 14 measurement forms
- **#6526**: klt signoff --attest 9: 'dependency_binding' text line says '1 dependencies declared'
- **#6681**: klt lvs category CHANGELOG guard is blind to changelog.d/ fragments, so the documented changelog convention fails it
- **#6897**: Publish the v0.10.0 bridge release for consumers pinned to PyPI

## In Progress

Issues currently being built (`loom:building`).

- **#3147**: ASAP7 DRC deck: resolve remaining V0–V3 spacing, enclosure and track-dependent rules
- **#5578**: Verify the original long-running simulation on the simlong fleet profile
- **#7799**: Repair remaining ASAP7 M1 pad-to-neighbor tip spacings on a routed slice
- **#7801**: klt sim: no regression guards the limits-less derived observation's error skip reason
- **#7807**: klt signoff: an evidence citation's content_hash pins a run-scope-invariant artifact, so a reduced-coverage run grades identically to the complete one
- **#7817**: place-and-route (asap7): router-emitted M4 can land off the deck's track grid (M4.AUX.1/2 vertices at half-pitch, M4.W.3 non-integer-multiple widths) and no request field reaches it
- **#7818**: Bundle duplicated PEX extraction-option forwarding and share CLI dry-run kwargs
- **#7822**: lvs: gate-level-verilog reference rejects part-select operand inside an assign concatenation (0.9.0), after a full extraction
- **#7824**: Warn when klt extract uses a different KLayout engine than the release was tested with

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#4737**: feat(drc): enforce sky130 licon1/mcon max size with exemptions
- **#4772**: fix: honor --dry-run and --wait-for-capacity under klt sim --batch-set
- **#5781**: fix: remove the dead keep_artifacts parameter from _build_batch_job_spec
- **#6159**: feat(signoff): name a cross-release provenance.input shape mismatch in --attest --check
- **#6405**: fix(sim): drop hand-listed forms from the noise exclusivity refusal
- **#6473**: docs: record asap7 residual M5.W.3 as route-dependent router metal
- **#6733**: fix: pluralize the signoff dependency count correctly
- **#6739**: test: accept changelog.d fragments in the lvs CATEGORY_* changelog guard
- **#7804**: Document directional ambiguity for ASAP7 V0 edges at LIG
- **#7815**: feat: let an item-5 signoff citation assert the run scope it must cover
- **#7821**: refactor: bundle klt pex extraction options into PexExtractOptions
- **#7823**: feat(place-and-route): repair ASAP7 router off-grid M4 after the DEF-GDS merge
- **#7825**: feat: accept part-select operands in assign concatenations and convert gate-level references before extraction
- **#7827**: Cover missing observations in a limits-less derived measurement
- **#7828**: Repair ASAP7 M1 spacing at remote-via wire sides and routed notches
- **#7829**: feat(extract): record klayout_version_mismatch and warn on engine drift

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#6254**: fix: kill the openroad docker container when the wrapper dies; record launch-time engine evidence
- **#6962**: chore(release): bump version to 0.10.0
- **#7047**: fix(release): gate PR setup pre-push, recover pushed branch, report failed runs
- **#7547**: build(deps): bump docker/login-action from 4.7.0 to 4.8.0 in the github-actions group across 1 directory

## Proposed

Issues carrying `loom:curated`.

- **#2835**: sky130 DRC: enforce licon1/mcon maximum size without rejecting compound-layer exceptions *(curated)*
- **#3147**: ASAP7 DRC deck: resolve remaining V0–V3 spacing, enclosure and track-dependent rules *(curated)*
- **#3462**: klt sim batch: no way to require the worker PDK revision match a pin *(curated)*
- **#3962**: Split cli/parser.py: move per-verb parser registration into verb modules (190 commits / 20 conflict merges in 5 weeks) *(curated)*
- **#4075**: test flake: Install the klayout binary fails on ASAP7 DRC deck fixtures (PR #4074, recurrence of #4059) *(curated)*
- **#4351**: klt sim --backend batch: add --cancel JOB_ID (needs fleet-side cancel primitive) *(curated)*
- **#4771**: Friction: klt sim --batch-set --dry-run submits real batch jobs; --wait-for-capacity is refused with --batch-set *(curated)*
- **#5084**: [Epic #520] Sharded gf180mcuD run and CI sample *(curated)*
- **#5160**: Extract compare-setup and klayout-engine branch from run_lvs (stage 4/6, after #4126) *(curated)*
- **#5313**: place-and-route: macro-edge congestion control (keep-out independent of the PDN halo, per-channel overflow report) *(curated)*
- **#5494**: klt pex: multi-top mode so one graded run can extract every drawn cell a link testbench instantiates *(curated)*
- **#5578**: Verify the original long-running simulation on the simlong fleet profile *(curated)*
- **#5630**: lvs: SIGSEGV (exit 139, no output) on a routed ASAP7 block under the documented #4610 recipe; each half of the recipe completes *(curated)*
- **#5672**: place-and-route: a timing-free (structural) standard-cell Liberty cannot reach place/route — clock_period_ns is mandatory and CTS cannot be opted out of *(curated)*
- **#5726**: openroad docker wrapper leaks the container when klt is killed, so an orphaned route writes unattributable artefacts into the caller's repo hours later *(curated)*
- **#5730**: klt signoff --check reports 'drifted' for a provenance schema change, indistinguishable from real evidence drift *(curated)*
- **#5796**: klt lvs (klayout-runset): no device map for subckt-call references — reference side converts ~nothing, silently *(curated)*
- **#5829**: signoff attest envelopes: provenance.input shape changed incompatibly under an unchanged schema_version; no migration path or version-tolerant --check *(curated)*
- **#5945**: klt signoff --attest: evidence records that git can merge (one line per covered file, no committed aggregate), so concurrent chip PRs stop conflicting *(curated)*
- **#6219**: Publish the pinned xschem build as a release asset (credential-free install route) *(curated)*
- **#6389**: sim_validate._validate_noise_measurement:155 still hand-lists 7 of 14 measurement forms *(curated)*
- **#6526**: klt signoff --attest 9: 'dependency_binding' text line says '1 dependencies declared' *(curated)*
- **#6543**: gen-compose: a no-waypoint 2-pin leg between blocks[].cell ports reports routed but draws its via-drop pads at un-translated cell coordinates *(curated)*
- **#6681**: klt lvs category CHANGELOG guard is blind to changelog.d/ fragments, so the documented changelog convention fails it *(curated)*
- **#6897**: Publish the v0.10.0 bridge release for consumers pinned to PyPI *(curated)*
- **#6898**: place-and-route: no thread-count control, so OpenROAD's detailed route runs on one core whatever the host has and a large block cannot finish inside any executor cap *(curated)*
- **#6902**: signoff: a matching sky130 klayout-runset LVS report can never grade item 4 or 11 met (zero-instance declared classes, case-different census, sky130.lvs not pinned) *(curated)*
- **#6911**: Report failed release preparation and tagging, and recover the release branch on retry *(curated)*
- **#6914**: klt lvs klayout-runset (ihp-sg13g2): measure a terminal-order normalisation for mirrored inductor2 and route the item 4/11 grading question (split from #6903 part D) *(curated)*
- **#7799**: Repair remaining ASAP7 M1 pad-to-neighbor tip spacings on a routed slice *(curated)*
- **#7801**: klt sim: no regression guards the limits-less derived observation's error skip reason *(curated)*
- **#7807**: klt signoff: an evidence citation's content_hash pins a run-scope-invariant artifact, so a reduced-coverage run grades identically to the complete one *(curated)*
- **#7817**: place-and-route (asap7): router-emitted M4 can land off the deck's track grid (M4.AUX.1/2 vertices at half-pitch, M4.W.3 non-integer-multiple widths) and no request field reaches it *(curated)*
- **#7822**: lvs: gate-level-verilog reference rejects part-select operand inside an assign concatenation (0.9.0), after a full extraction *(curated)*
- **#7824**: Warn when klt extract uses a different KLayout engine than the release was tested with *(curated)*

## Proposed (Architect / Hermit)

- **#485**: Live digital fleet validation — measure T_o/t, derive the K-selection rule *(architect)*
- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark *(architect)*
- **#708**: Epic: finite-element full-field solver for klt (Rust) — volumetric fields, electrothermal, MoM cross-validation *(architect)*
- **#7710**: Remove unwired FLUTE/RUDY congestion pre-check spike (Rust crate + wrapper + CI leg) *(hermit)*
- **#7810**: Extract duplicated Rust Liberty parser shared by native/statime and native/techmap *(hermit)*

## Epics

- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark
- **#708**: Epic: finite-element full-field solver for klt (Rust) — volumetric fields, electrothermal, MoM cross-validation
- **#2570**: Friction: no ihp-sg13g2 remote-sim AMI, so klt sim's batch backend cannot run a PDK klt fully supports locally

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 4 |
| Very important | 1 |
| Important | 5 |
| Ready (`loom:issue`) | 11 |
| In Progress (`loom:building`) | 9 |
| PRs awaiting review | 16 |
| Approved PRs awaiting merge | 4 |
| Curated | 35 |
| Architect / Hermit proposals | 5 |
| Active epics | 3 |
<!-- guide:plan-body:end -->
