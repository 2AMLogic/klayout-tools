# Work Plan

Current work from GitHub labels. Long-term direction lives in [ROADMAP.md](ROADMAP.md).

<!-- guide:plan-body:start -->
_Generated: 2026-11-25T11:20:06Z (snapshot; compare against live labels)_
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — a human release decision is missing. #7859 also has a release-order prerequisite (see its row).

- **#6254**: fix: kill the openroad docker container when the wrapper dies; record launch-time engine evidence
- **#6962**: chore(release): bump version to 0.10.0 — the v0.10.0 bridge PR that #7859 waits on
- **#7047**: fix(release): gate PR setup pre-push, recover pushed branch, report failed runs
- **#7547**: build(deps): bump docker/login-action from 4.7.0 to 4.8.0 in the github-actions group across 1 directory
- **#7859**: feat: cadence-gated automatic patch releases via the fleet publisher — release order: do not release this hold until bridge PR #6962 has merged, the `v0.10.0` tag exists, and no release bump PR remains open (Champion's merge-risk hold, [#7859 comment](https://github.com/2AMLogic/klayout-tools/pull/7859#issuecomment-7153141884))
- **#7869**: feat: re-hash a cited klt sim report's .include closure in signoff
- **#7890**: refactor: retire the experimental congestion pre-check

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

- **#6897**: Publish the v0.10.0 bridge release for consumers pinned to PyPI

## Important

`loom:important`; taken before unlabelled work.

- **#4891**: batch fleet runner's ngspice cannot evaluate sky130A model-library expr() parameters (runner image gap), engine_version still null *(held)*
- **#5322**: lvs: no working GDS-to-verdict route for hierarchical compare of a large regular array (stub flattened, abstract-cell pin labels promoted) *(held)*
- **#6898**: place-and-route: no thread-count control, so OpenROAD's detailed route runs on one core whatever the host has and a large block cannot finish inside any executor cap *(held)*
- **#6902**: signoff: a matching sky130 klayout-runset LVS report can never grade item 4 or 11 met (zero-instance declared classes, case-different census, sky130.lvs not pinned) *(held)*
- **#7831**: Preserve SDF preflight instance checks when a gate-level assignment writes a part-select *(held)*

## Ready

Human-approved issues (`loom:issue`) ready for implementation — excluding held (`loom:blocked`/`loom:operator`/`loom:frozen`), `loom:building`, and issues with an open linked PR (native admission `OPEN_PR`; listed under Issues with Open PRs).

_None._

## Issues with Open PRs (Not Dispatchable)

`loom:issue` issues an open linked PR already holds — native admission would refuse them (`OPEN_PR`); listed with the PR and its stage, not counted above.

- **#2835**: sky130 DRC: enforce licon1/mcon maximum size without rejecting compound-layer exceptions — open PR #4737 (held)
- **#5494**: klt pex: multi-top mode so one graded run can extract every drawn cell a link testbench instantiates — open PR #5523 (held)
- **#5630**: lvs: SIGSEGV (exit 139, no output) on a routed ASAP7 block under the documented #4610 recipe; each half of the recipe completes — open PR #6122 (held)
- **#5726**: openroad docker wrapper leaks the container when klt is killed, so an orphaned route writes unattributable artefacts into the caller's repo hours later — open PR #6254 (held)
- **#5730**: klt signoff --check reports 'drifted' for a provenance schema change, indistinguishable from real evidence drift — open PR #5987 (held)
- **#5829**: signoff attest envelopes: provenance.input shape changed incompatibly under an unchanged schema_version; no migration path or version-tolerant --check — open PR #6159 (held)
- **#5945**: klt signoff --attest: evidence records that git can merge (one line per covered file, no committed aggregate), so concurrent chip PRs stop conflicting — open PR #6269 (held)
- **#6389**: sim_validate._validate_noise_measurement:155 still hand-lists 7 of 14 measurement forms — open PR #6405 (held)
- **#6526**: klt signoff --attest 9: 'dependency_binding' text line says '1 dependencies declared' — open PR #6733 (held)
- **#6681**: klt lvs category CHANGELOG guard is blind to changelog.d/ fragments, so the documented changelog convention fails it — open PR #6739 (held)
- **#6897**: Publish the v0.10.0 bridge release for consumers pinned to PyPI — open PR #6962 (held)
- **#6911**: Report failed release preparation and tagging, and recover the release branch on retry — open PR #7047 (held)

## In Progress

Issues currently being built (`loom:building`).

- **#5005**: Release on the Loom model: cadence-gated 0.9.x patch bump via the fleet publisher, tag + PyPI on green main
- **#5084**: [Epic #520] Sharded gf180mcuD run and CI sample
- **#7710**: Remove unwired FLUTE/RUDY congestion pre-check spike (Rust crate + wrapper + CI leg)
- **#7840**: Share session constraint and reporting Tcl assembly between _sta_script_lines and _sta_netlist_script_lines
- **#7860**: klt sim provenance hash covers only the top deck, not files it .include-s
- **#7879**: CI: the live canary manifest gate reds every in-flight PR whose branch predates a manifest sync (third recurrence)
- **#7882**: CI: nothing surfaces a failure of the daily canary-inventory check
- **#7892**: [Epic #708] Expose steady heat and Joule self-heating through klt em

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`). Rows marked *(held)* also carry `loom:blocked` or `loom:operator` and are not dispatchable until a human releases the hold.

- **#4737**: feat(drc): enforce sky130 licon1/mcon max size with exemptions *(held)*
- **#4772**: fix: honor --dry-run and --wait-for-capacity under klt sim --batch-set *(held)*
- **#5781**: fix: remove the dead keep_artifacts parameter from _build_batch_job_spec *(held)*
- **#6159**: feat(signoff): name a cross-release provenance.input shape mismatch in --attest --check *(held)*
- **#6405**: fix(sim): drop hand-listed forms from the noise exclusivity refusal *(held)*
- **#6473**: docs: record asap7 residual M5.W.3 as route-dependent router metal *(held)*
- **#6733**: fix: pluralize the signoff dependency count correctly *(held)*
- **#6739**: test: accept changelog.d fragments in the lvs CATEGORY_* changelog guard *(held)*
- **#7843**: Share Tcl assembly for DEF and netlist timing sessions

## Approved (Awaiting Merge)

PRs that passed review (`loom:pr`). Only rows without *(held)* are queued for Champion auto-merge; a row marked *(held)* carries `loom:blocked` or `loom:operator` and waits on a human release decision.

- **#6254**: fix: kill the openroad docker container when the wrapper dies; record launch-time engine evidence — closes #5726 *(held)*
- **#6962**: chore(release): bump version to 0.10.0 *(held)* — the v0.10.0 bridge PR; the `v0.10.0` tag does not exist yet, and #7859 waits on this PR
- **#7047**: fix(release): gate PR setup pre-push, recover pushed branch, report failed runs — closes #6911 *(held)*
- **#7547**: build(deps): bump docker/login-action from 4.7.0 to 4.8.0 in the github-actions group across 1 directory *(held)*
- **#7859**: feat: cadence-gated automatic patch releases via the fleet publisher — closes #5005 *(held)* — release order: merging this before bridge PR #6962 lands and the `v0.10.0` tag exists removes the release path the bridge relies on
- **#7869**: feat: re-hash a cited klt sim report's .include closure in signoff — closes #7860 *(held)*
- **#7890**: refactor: retire the experimental congestion pre-check — closes #7710 *(held)*

## Proposed

Issues carrying `loom:curated`.

- **#2835**: sky130 DRC: enforce licon1/mcon maximum size without rejecting compound-layer exceptions *(curated)*
- **#3962**: Split cli/parser.py: move per-verb parser registration into verb modules (190 commits / 20 conflict merges in 5 weeks) *(curated)* *(held)*
- **#4075**: test flake: Install the klayout binary fails on ASAP7 DRC deck fixtures (PR #4074, recurrence of #4059) *(curated)* *(held)*
- **#4351**: klt sim --backend batch: add --cancel JOB_ID (needs fleet-side cancel primitive) *(curated)* *(held)*
- **#4771**: Friction: klt sim --batch-set --dry-run submits real batch jobs; --wait-for-capacity is refused with --batch-set *(curated)* *(held)*
- **#5005**: Release on the Loom model: cadence-gated 0.9.x patch bump via the fleet publisher, tag + PyPI on green main *(curated)*
- **#5084**: [Epic #520] Sharded gf180mcuD run and CI sample *(curated)*
- **#5160**: Extract compare-setup and klayout-engine branch from run_lvs (stage 4/6, after #4126) *(curated)* *(held)*
- **#5313**: place-and-route: macro-edge congestion control (keep-out independent of the PDN halo, per-channel overflow report) *(curated)* *(held)*
- **#5494**: klt pex: multi-top mode so one graded run can extract every drawn cell a link testbench instantiates *(curated)*
- **#5630**: lvs: SIGSEGV (exit 139, no output) on a routed ASAP7 block under the documented #4610 recipe; each half of the recipe completes *(curated)*
- **#5726**: openroad docker wrapper leaks the container when klt is killed, so an orphaned route writes unattributable artefacts into the caller's repo hours later *(curated)*
- **#5730**: klt signoff --check reports 'drifted' for a provenance schema change, indistinguishable from real evidence drift *(curated)*
- **#5796**: klt lvs (klayout-runset): no device map for subckt-call references — reference side converts ~nothing, silently *(curated)* *(held)*
- **#5829**: signoff attest envelopes: provenance.input shape changed incompatibly under an unchanged schema_version; no migration path or version-tolerant --check *(curated)*
- **#5945**: klt signoff --attest: evidence records that git can merge (one line per covered file, no committed aggregate), so concurrent chip PRs stop conflicting *(curated)*
- **#6389**: sim_validate._validate_noise_measurement:155 still hand-lists 7 of 14 measurement forms *(curated)*
- **#6526**: klt signoff --attest 9: 'dependency_binding' text line says '1 dependencies declared' *(curated)*
- **#6543**: gen-compose: a no-waypoint 2-pin leg between blocks[].cell ports reports routed but draws its via-drop pads at un-translated cell coordinates *(curated)* *(held)*
- **#6681**: klt lvs category CHANGELOG guard is blind to changelog.d/ fragments, so the documented changelog convention fails it *(curated)*
- **#6897**: Publish the v0.10.0 bridge release for consumers pinned to PyPI *(curated)*
- **#6898**: place-and-route: no thread-count control, so OpenROAD's detailed route runs on one core whatever the host has and a large block cannot finish inside any executor cap *(curated)* *(held)*
- **#6902**: signoff: a matching sky130 klayout-runset LVS report can never grade item 4 or 11 met (zero-instance declared classes, case-different census, sky130.lvs not pinned) *(curated)* *(held)*
- **#6911**: Report failed release preparation and tagging, and recover the release branch on retry *(curated)*
- **#6914**: klt lvs klayout-runset (ihp-sg13g2): measure a terminal-order normalisation for mirrored inductor2 and route the item 4/11 grading question (split from #6903 part D) *(curated)* *(held)*
- **#7831**: Preserve SDF preflight instance checks when a gate-level assignment writes a part-select *(curated)* *(held)*
- **#7860**: klt sim provenance hash covers only the top deck, not files it .include-s *(curated)*
- **#7879**: CI: the live canary manifest gate reds every in-flight PR whose branch predates a manifest sync (third recurrence) *(curated)*
- **#7882**: CI: nothing surfaces a failure of the daily canary-inventory check *(curated)*

## Proposed (Architect / Hermit)

- **#485**: Live digital fleet validation — measure T_o/t, derive the K-selection rule *(architect)* *(held)*
- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark *(architect)*
- **#708**: Epic: finite-element full-field solver for klt (Rust) — volumetric fields, electrothermal, MoM cross-validation *(architect)*
- **#7893**: [Epic #708] Validate electrothermal temperature rise on a real power device *(architect)* *(held)*
- **#7810**: Extract duplicated Rust Liberty parser shared by native/statime and native/techmap *(hermit)* *(held)*

## Epics

- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark
- **#708**: Epic: finite-element full-field solver for klt (Rust) — volumetric fields, electrothermal, MoM cross-validation
- **#2570**: Friction: no ihp-sg13g2 remote-sim AMI, so klt sim's batch backend cannot run a PDK klt fully supports locally

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 7 |
| Very important | 1 |
| Important | 5 |
| Ready (`loom:issue`, net of holds/building/open-linked-PRs) | 0 |
| In Progress (`loom:building`) | 8 |
| Held, no other bucket | 29 |
| PRs awaiting review | 9 |
| Approved PRs awaiting merge | 7 |
| Curated | 29 |
| Architect / Hermit proposals | 5 |
| Active epics | 3 |
<!-- guide:plan-body:end -->
