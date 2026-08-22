# `sky130-modexp` fleet-canary signoff status (Epic #700 acceptance criterion 2)

Ground-truth check for [Epic #700](https://github.com/2AMLogic/klayout-tools/issues/700)'s
acceptance criterion 2 — *"Validated end-to-end on at least one fleet canary
(`sky130-modexp` is the cleanest digital target) ... with results compared
against OpenROAD as ground truth"* — filed as phase issue
[#1329](https://github.com/2AMLogic/klayout-tools/issues/1329).

**This is a status report, not a new run.** No new `klt place-and-route`,
`klt drc`, or `klt lvs` invocation was performed while producing this
document. `2AMLogic/sky130-modexp` is a separate, real repo this Builder
session does not have write access to (a probe collaborator-permission
check returned no push rights), and the sandbox this session ran in has no
`openroad` binary and no local sky130A PDK checkout — both required for
`klt place-and-route` (which orchestrates OpenROAD as a subprocess, per
`docs/design/openroad-invocation-survey.md`) or a plain-OpenROAD comparison
run. Fabricating a "run" without either would not be an honest completion of
this criterion, so instead this records exactly what `2AMLogic/sky130-modexp`'s
own committed evidence trail already establishes, as of `main`@`7859b48`
(2026-08-22), and precisely what remains open.

## What already exists: a real run, not the local proxy fixture

Confirmed by cloning `https://github.com/2AMLogic/sky130-modexp.git` (public,
reachable, no auth required) and reading its evidence trail directly — this
is the actual fleet canary repo, not `tests/corpus/place_and_route/` (this
repo's local test proxy, `docs/design/rsa-modexp-baseline.md`):

- **`klt place-and-route` has already been run against the real netlist.**
  `sky130-modexp#7` / PR #17 (2026-08-14) produced `layout/modexp.gds` and
  `layout/modexp.def` from `flow/.klt/synthesize/modexp_synth_tied.v` (the
  real synthesized modexp netlist) via `klt place-and-route` against
  `flow/par-modexp.json` — `sky130_fd_sc_hd`/`tt_025C_1v80`, 100 MHz, `seed:
  42`. Full provenance (`klt` git revision, OpenROAD build, sky130A PDK
  commit) is recorded in that repo's `layout/README.md`.
- **DRC is clean.** `sky130-modexp#55` / PR #65 (2026-08-16) re-ran `klt drc
  layout/modexp.gds --deck sky130 --format json` after bumping the `klt` pin
  past a merged upstream fix
  ([klayout-tools#998](https://github.com/2AMLogic/klayout-tools/pull/998)):
  `status: "clean"`, `violation_count: 0`. The prior 10 violations (all
  `diff.enclosing.licon.1` on `sky130_fd_sc_hd__and3_1` instances) were a
  `klt drc` check-engine limitation (unmerged same-layer `Region`), not a
  design defect — see `sky130-modexp/docs/signoff-claim.md`.

## What is not yet true: LVS is not clean, and the OpenROAD-ground-truth leg has not been attempted

- **LVS: `status: "mismatch"`, not clean.** The same PR #65 obtained, for
  the first time, an as-built reference netlist (via
  [klayout-tools#997](https://github.com/2AMLogic/klayout-tools/pull/997)'s
  `write_verilog` export) and ran `klt lvs` (engine `klayout`) against it.
  Result: **1324 mismatches** (888 `topology`, 434 `net.split`, 2
  `net.merged`) — attributed, with evidence, to two extraction-methodology
  gaps (`klt extract`'s net-naming not correlating to the reference's own
  signal names, and the `sky130` extraction deck's own documented
  layer-coverage limits), not to a real connectivity defect in the design.
  Neither is judged a silicon-correctness problem, but the literal
  acceptance-criterion bar — `klt lvs`-clean — is **not met**. Full detail:
  `sky130-modexp/docs/signoff-claim.md` and
  `sky130-modexp/verification/records/drc-lvs/records/20260816-174310-5e656e5.md`.
- **No OpenROAD-only ground-truth comparison exists.** Searched
  `sky130-modexp`'s full evidence trail (`docs/`, `layout/`,
  `verification/records/`, `WORK_LOG.md`) for any run of vanilla
  OpenROAD/ORFS on the same netlist+LEF/constraints outside of `klt
  place-and-route`'s own orchestration — found none. `sky130-modexp`'s
  `main` has had no substantive (non-Loom-resync) commit since PR #74
  (2026-08-19, a CI-only timeout tweak); the layout/DRC/LVS work stopped at
  PR #65 (2026-08-16). This leg of criterion 2 (Epic #700's "compared
  against OpenROAD as ground truth") has not been started for this canary.

## Why this is not actionable as ordinary dispatchable Builder work

`sky130-modexp#12` (that repo's own T1/bronze tracking epic) labels its
place-and-route/DRC/LVS work `loom:operator-only` — its own fleet explicitly
gates this class of run behind operator provisioning, not autonomous sweep
dispatch, because it needs a locally provisioned OpenROAD + sky130A PDK
build host (`sky130-modexp/docs/environment.md`'s `scripts/setup-env.sh` /
pinned `openroad/orfs` Docker route) that a stock Builder sandbox does not
carry. Landing new evidence there also requires push access to that repo,
which this identity does not have. Both blockers are orthogonal to anything
fixable inside `klayout-tools`.

## Bottom line against #1329's acceptance criteria

| # | Criterion | Status |
| --- | --- | --- |
| 1 | `klt place-and-route` run against the real `2AMLogic/sky130-modexp` netlist | **Met** — already done (`sky130-modexp#7`/PR#17, 2026-08-14), predating this issue |
| 2 | Result confirmed `klt lvs`-clean and `klt drc`-clean | **Partially met** — DRC clean; LVS still `status: "mismatch"` |
| 3 | Compared against a plain OpenROAD run as ground truth, documented | **Not met** — no such comparison exists anywhere in the canary repo's history |

Recommendation: this phase issue should not be closed as complete. The
remaining work (closing the LVS gap and running/documenting the
OpenROAD-only ground-truth comparison) needs an operator-provisioned
OpenROAD + sky130A environment and, for the LVS/ground-truth evidence itself,
write access to `2AMLogic/sky130-modexp` — flag `#1329` `loom:blocked`
pending that provisioning rather than claim completion here.

## Related

- Epic #700 (`klayout-tools#700`) — the parent epic this phase serves.
- `docs/design/rsa-modexp-baseline.md` — records that the modexp design
  itself (RTL, baseline) moved to `sky130-modexp`; this repo keeps only tool
  fixtures.
- `docs/design/openroad-invocation-survey.md` — how `klt place-and-route`
  orchestrates OpenROAD today (Phase 0 of Epic #700).
- `sky130-modexp/docs/signoff-claim.md` — the canonical, authoritative DRC/LVS
  claim for this canary, maintained in that repo.
