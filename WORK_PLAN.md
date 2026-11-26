# Work Plan

Current work from GitHub labels. Long-term direction lives in [ROADMAP.md](ROADMAP.md).

<!-- guide:plan-body:start -->
_Generated: 2026-11-26T02:03:17Z (snapshot; compare against live labels)_
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs carrying `loom:blocked` or `loom:operator`.

- **#6962**: chore(release): bump version to 0.10.0 — v0.10.0 bridge release for #6897
- **#7859**: feat: cadence-gated automatic patch releases via the fleet publisher — waits for bridge PR #6962 to merge, the `v0.10.0` tag to exist and all release bump PRs to close. [Champion’s release-order decision](https://github.com/2AMLogic/klayout-tools/pull/7859#issuecomment-7153141884)
- **#7900**: chore(loom): resync installed Loom to v0.20.33
- **#7908**: fix: point kept batch decks at the submitted netlist and note a missing engine log

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
- **#5726**: openroad docker wrapper leaks the container when klt is killed, so an orphaned route writes unattributable artefacts into the caller's repo hours later — open PR #6254 (approved)
- **#5730**: klt signoff --check reports 'drifted' for a provenance schema change, indistinguishable from real evidence drift — open PR #5987 (held)
- **#5829**: signoff attest envelopes: provenance.input shape changed incompatibly under an unchanged schema_version; no migration path or version-tolerant --check — open PR #6159 (held)
- **#5945**: klt signoff --attest: evidence records that git can merge (one line per covered file, no committed aggregate), so concurrent chip PRs stop conflicting — open PR #6269 (held)
- **#6389**: sim_validate._validate_noise_measurement:155 still hand-lists 7 of 14 measurement forms — open PR #6405 (held)
- **#6526**: klt signoff --attest 9: 'dependency_binding' text line says '1 dependencies declared' — open PR #6733 (held)
- **#6681**: klt lvs category CHANGELOG guard is blind to changelog.d/ fragments, so the documented changelog convention fails it — open PR #6739 (held)
- **#6897**: Publish the v0.10.0 bridge release for consumers pinned to PyPI — open PR #6962 (held)

## In Progress

Issues currently being built (`loom:building`).

- **#5005**: Release on the Loom model: cadence-gated 0.9.x patch bump via the fleet publisher, tag + PyPI on green main
- **#7860**: klt sim provenance hash covers only the top deck, not files it .include-s
- **#7879**: CI: the live canary manifest gate reds every in-flight PR whose branch predates a manifest sync (third recurrence)
- **#7882**: CI: nothing surfaces a failure of the daily canary-inventory check
- **#7898**: WORK_PLAN: installed Guide (v0.19.1302) lists issues that already have an open PR as Ready, and will overwrite hand-corrected plans
- **#7905**: klt sim/pex batch: keep_artifacts returns no per-corner ngspice log and a corner.cir that includes a non-retained job path
- **#7906**: klt sim: on_unreached "null" is refused on a window-miss corner that also carries stepping narration
- **#7916**: ASAP7 M4 byte-comparison tests can fail when GDS writes cross a second boundary
- **#7918**: klt lvs: commented xschem top header (**.subckt) breaks klayout-runset align and leaves .TOP with zero pins
- **#7919**: signoff: verify an item 1 source by Git repository, commit and digest
- **#7920**: Remove the simulation runner import from tightened-rerun validation
- **#7922**: Parse measurement windows without loading the simulation runner
- **#7926**: Report an unreadable recorded include as indeterminate in klt sim --check
- **#7928**: Consolidate the Xyce request gate in the existing refusal module

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
- **#7929**: Keep the ordered Xyce request checks in the refusal module *(held)*

## Approved (Awaiting Merge)

PRs that passed review (`loom:pr`). Rows marked *(held)* carry `loom:blocked` or `loom:operator`. Champion evaluates the other approved PRs for merge.

- **#6254**: fix: kill the openroad docker container when the wrapper dies; record launch-time engine evidence — closes #5726
- **#6962**: chore(release): bump version to 0.10.0 *(held)* — v0.10.0 bridge release for #6897
- **#7859**: feat: cadence-gated automatic patch releases via the fleet publisher — closes #5005 *(held)* — release order: wait for bridge PR #6962 to merge, the v0.10.0 tag and closure of the release bump PR
- **#7869**: feat: re-hash a cited klt sim report's .include closure in signoff — closes #7860
- **#7900**: chore(loom): resync installed Loom to v0.20.33 *(held)*
- **#7908**: fix: point kept batch decks at the submitted netlist and note a missing engine log — closes #7905 *(held)*
- **#7909**: fix(sim): grade an opted-in window miss not_reached beside stepping narration — closes #7906
- **#7917**: Use deterministic GDS output in ASAP7 M4 grid tests — closes #7916
- **#7921**: Validate tightened reruns without importing the simulation runner — closes #7920
- **#7923**: Parse measurement windows without importing the simulation runner — closes #7922
- **#7924**: feat: verify pinned Git blobs in item 1 signoff attestations — closes #7919
- **#7925**: fix: prepare commented xschem and implicit .TOP runset references — closes #7918
- **#7927**: Report unreadable recorded netlist files in klt sim --check — closes #7926

## Proposed

Issues carrying `loom:curated`.

- **#2835**: sky130 DRC: enforce licon1/mcon maximum size without rejecting compound-layer exceptions *(curated)*
- **#3962**: Split cli/parser.py: move per-verb parser registration into verb modules (190 commits / 20 conflict merges in 5 weeks) *(curated)* *(held)*
- **#4075**: test flake: Install the klayout binary fails on ASAP7 DRC deck fixtures (PR #4074, recurrence of #4059) *(curated)* *(held)*
- **#4351**: klt sim --backend batch: add --cancel JOB_ID (needs fleet-side cancel primitive) *(curated)* *(held)*
- **#4771**: Friction: klt sim --batch-set --dry-run submits real batch jobs; --wait-for-capacity is refused with --batch-set *(curated)* *(held)*
- **#5005**: Release on the Loom model: cadence-gated 0.9.x patch bump via the fleet publisher, tag + PyPI on green main *(curated)*
- **#5084**: [Epic #520] Sharded gf180mcuD run and CI sample *(curated)* *(held)*
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
- **#6914**: klt lvs klayout-runset (ihp-sg13g2): measure a terminal-order normalisation for mirrored inductor2 and route the item 4/11 grading question (split from #6903 part D) *(curated)* *(held)*
- **#7831**: Preserve SDF preflight instance checks when a gate-level assignment writes a part-select *(curated)* *(held)*
- **#7860**: klt sim provenance hash covers only the top deck, not files it .include-s *(curated)*
- **#7879**: CI: the live canary manifest gate reds every in-flight PR whose branch predates a manifest sync (third recurrence) *(curated)*
- **#7882**: CI: nothing surfaces a failure of the daily canary-inventory check *(curated)*
- **#7898**: WORK_PLAN: installed Guide (v0.19.1302) lists issues that already have an open PR as Ready, and will overwrite hand-corrected plans *(curated)*
- **#7905**: klt sim/pex batch: keep_artifacts returns no per-corner ngspice log and a corner.cir that includes a non-retained job path *(curated)*
- **#7906**: klt sim: on_unreached "null" is refused on a window-miss corner that also carries stepping narration *(curated)*
- **#7916**: ASAP7 M4 byte-comparison tests can fail when GDS writes cross a second boundary *(curated)*
- **#7918**: klt lvs: commented xschem top header (**.subckt) breaks klayout-runset align and leaves .TOP with zero pins *(curated)*
- **#7919**: signoff: verify an item 1 source by Git repository, commit and digest *(curated)*

## Proposed (Architect / Hermit)

- **#485**: Live digital fleet validation — measure T_o/t, derive the K-selection rule *(architect)* *(held)*
- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark *(architect)*
- **#7810**: Extract duplicated Rust Liberty parser shared by native/statime and native/techmap *(hermit)* *(held)*
- **#7930**: Read the netlist once when validating simulation supply overrides *(hermit)*

## Epics

- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark
- **#2570**: Friction: no ihp-sg13g2 remote-sim AMI, so klt sim's batch backend cannot run a PDK klt fully supports locally

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 4 |
| Very important | 1 |
| Important | 5 |
| Ready (`loom:issue`, net of holds/building/open-linked-PRs) | 0 |
| In Progress (`loom:building`) | 14 |
| Held, no other bucket | 29 |
| PRs awaiting review | 9 |
| Approved PRs awaiting merge | 13 |
| Curated | 34 |
| Architect / Hermit proposals | 4 |
| Active epics | 2 |
<!-- guide:plan-body:end -->
