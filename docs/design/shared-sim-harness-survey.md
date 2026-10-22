# Survey: six forked chip sim harnesses vs `klt`

**Status:** survey (issue [#4042](https://github.com/2AMLogic/klayout-tools/issues/4042)).
Measurement and classification only. It changes no `klt` behaviour and no
chip repo, and it does not migrate anything.

Six `2AMLogic` chip repos each carry their own Python sim harness under
`sim/harness/`. The module sets overlap heavily and the file hashes differ in
every repo, so these are forks drifting apart rather than stamped copies. The
issue's working assumption was that much of each harness duplicates `klt`
("inference, not yet measured"). This document measures it.

## Method and its limits

- Source: each repo's `sim/harness/*.py` at the commit SHA below (default
  branch HEAD on 2026-10-22), fetched read-only with `gh api` into a scratch
  directory outside this repo. Nothing from the chip repos is vendored here.
- Module lists were re-derived from those SHAs. They match the issue's table.
- Every module was classified from its module docstring, its full public
  function/class inventory, and the matching `klt` contract
  (`docs/cli/sim.md`, `pdk.md`, `yield-campaign.md`, `env-provenance.md`,
  `docs/design/sim-evidence-discipline-spike.md`). Modules that decide a
  borderline case were read in more detail; **this is not a line-by-line
  audit**, and a class is a judgment from that reading, not a proof of
  behavioural equivalence. A migration PR has to re-verify per module.
- Each module gets **one** class, by its dominant content. Mixed modules say
  so in the basis column. LOC is `wc -l` of the file, `__init__.py` excluded.
  Class (b) is therefore an upper bound: it includes chip-specific record text
  inside `report.py`.
- Classes: **(a)** already covered by a named `klt` verb/flag; **(b)** generic
  and missing from `klt` (upstream candidate); **(c)** chip-specific, stays
  local.

| repo | SHA | harness modules (excluding `__init__.py`) |
|---|---|---|
| 2AMLogic/gf180-bandgap | `5617e263a01b8056fa2fd2122a1c062ba326182a` | cli, corners, dctable, evidence_lint, fmt, paths, pdk, report, runner, stats, testbench |
| 2AMLogic/gf180-gate-driver | `a3fb3afcd48d091c83aaba364425e676eeea6d7e` | campaign, cli, corners, evidence_lint, montecarlo, pdk, report, runner, script_helpers, testbench |
| 2AMLogic/gf180-tmds-tx | `0e7d0291b3b5ce84f36355bc33f14d5c66c66947` | cli, corners, evidence_lint, pdk, report, runner, testbench |
| 2AMLogic/gf180-usb2-phy | `e0a168b76090072bf34df041dfb020cd74f25a22` | cli, corners, evidence_lint, klt_backend, pdk, report, runner, testbench |
| 2AMLogic/sky130-usb2-phy | `d4c9b21f9f022e529dbde91611678596f77d9515` | evidence_lint, matrix, mismatch, mismatch_lint, results, runner, trim |
| 2AMLogic/sg13g2-stdcells | `f450813c6a5eecf3cf67aaf899db90d11f4ee68c` | cli, corners, evidence, pdk, report, runner, testbench, toolchain |

All six repos were readable; no row is "not surveyed".
`sg13g2-serdes` imports the `sg13g2-stdcells` harness (`reuse.lock.json`) and
runs `klt sim` directly; its copy is not counted again here.

## Measured duplication

| repo | (a) covered | (b) generic gap | (c) chip-specific | total | (a)+(b) share |
|---|---:|---:|---:|---:|---:|
| gf180-bandgap | 1284 | 1661 | 443 | 3388 | 86% |
| gf180-gate-driver | 1754 | 2160 | 392 | 4306 | 90% |
| gf180-tmds-tx | 1217 | 1536 | 412 | 3165 | 86% |
| gf180-usb2-phy | 1603 | 1209 | 518 | 3330 | 84% |
| sky130-usb2-phy | 0 | 1347 | 550 | 1897 | 71% |
| sg13g2-stdcells | 1027 | 878 | 228 | 2133 | 89% |
| **all six** | 6885 | 8791 | 2543 | 18219 | 86% |

Reading the numbers:

- About **38% of harness LOC (6,885 of 18,219) is already covered by `klt`**,
  and a further **48% (8,791) is generic but missing from `klt`**. Only about
  14% (2,543) is chip-specific. The "much of each harness duplicates `klt`"
  inference holds, and the larger share is the generic evidence layer, not the
  simulation engine.
- `sky130-usb2-phy` shows (a) = 0 only because it has no deck-composition
  layer at all: its `runner.py` already drives `klt sim --backend batch`, and
  its `mismatch.py` already drives `klt sim --batch-set`, `klt yield-samples`
  and `klt yield`. It is the existing proof that a chip can sit directly on
  `klt`; its remaining weight is evidence handling (b).
- `gf180-usb2-phy` carries both a local deck runner and a `klt_backend.py`
  adapter, so it pays for the harness and `klt` at once.
- The unit of duplication is the **evidence layer** (`report.py`,
  `evidence_lint.py`, and sky130's `matrix`/`results`/`*_lint`): roughly
  7,500 LOC of record minting, lint and matrix checking across the six. The
  evidence-discipline spike
  ([sim-evidence-discipline-spike.md](sim-evidence-discipline-spike.md))
  chose "documented convention" for it in #347, so the forks are what
  "convention only" produced. G1/G2 below ask to revisit that decision with
  this measurement.

## Module-by-module table

| repo | module | LOC | class | basis |
|---|---|---:|:-:|---|
| gf180-bandgap | `cli.py` | 400 | c | per-repo experiment UX (`list`/`check-env`/`run <slug>` over `sim/<slug>/`); `print-env` is `klt pdk env` |
| gf180-bandgap | `corners.py` | 217 | a | `corners.process` bundles (`{name, sections}`) x `supply_v` x `temperature_c` x `params` in `klt sim`; named bundle presets stay in G3 |
| gf180-bandgap | `dctable.py` | 148 | a | DC `print`-table parsing for hand-driven ngspice benches; `klt sim` `analysis.kind: dc` + `measurements[].expr` / `klt wave` |
| gf180-bandgap | `evidence_lint.py` | 687 | b | format/append-only/supersession lint of `sim/<slug>/records/`: G1 |
| gf180-bandgap | `fmt.py` | 21 | c | 21-line scalar formatter for the record text |
| gf180-bandgap | `paths.py` | 22 | c | 22-line repo-relative path helper (the hygiene *rule* is `klt env-provenance`; the helper is trivial) |
| gf180-bandgap | `pdk.py` | 173 | a | `klt pdk find`/`env` + `models.pdk` resolution in `klt sim` (`docs/cli/pdk.md`) |
| gf180-bandgap | `report.py` | 974 | b | append-only record minting (record id, never-overwrite, git provenance, dirty-tree flag, netlist snapshot, corner logs): G2; per-measurement summary/limit checks inside are `klt sim` `measurements[].limits` |
| gf180-bandgap | `runner.py` | 384 | a | deck composition + per-point ngspice + `.meas` parsing = `klt sim` local/local-parallel/batch backends |
| gf180-bandgap | `stats.py` | 60 | a | MC `op`-series parse + mean/stdev; `klt sim` `monte_carlo` + `klt yield` |
| gf180-bandgap | `testbench.py` | 302 | a | manifest + netlist-fragment rules = `klt sim` request + 'circuit body, not a full deck' convention (`docs/cli/sim.md`) |
| gf180-gate-driver | `campaign.py` | 651 | b | MC campaign shape: plain baseline + two zero-sigma controls per PVT point, derived-seed draws, per-draw CSV sidecar, non-converged draws disclosed: G5 (draw execution/CSV overlap `klt sim monte_carlo`, `klt yield-csv`) |
| gf180-gate-driver | `cli.py` | 392 | c | per-repo experiment UX (`list`/`check-env`/`run <slug>` over `sim/<slug>/`); `print-env` is `klt pdk env` |
| gf180-gate-driver | `corners.py` | 347 | a | `corners.process` bundles (`{name, sections}`) x `supply_v` x `temperature_c` x `params` in `klt sim`; named bundle presets stay in G3 |
| gf180-gate-driver | `evidence_lint.py` | 686 | b | format/append-only/supersession lint of `sim/<slug>/records/`: G1 |
| gf180-gate-driver | `montecarlo.py` | 231 | a | per-sample deterministic seeds + mc point relabel; `klt sim` `monte_carlo` derives `rndseed`/`process_seed`/`mismatch_seed` |
| gf180-gate-driver | `pdk.py` | 177 | a | `klt pdk find`/`env` + `models.pdk` resolution in `klt sim` (`docs/cli/pdk.md`) |
| gf180-gate-driver | `report.py` | 823 | b | append-only record minting (record id, never-overwrite, git provenance, dirty-tree flag, netlist snapshot, corner logs): G2; per-measurement summary/limit checks inside are `klt sim` `measurements[].limits` |
| gf180-gate-driver | `runner.py` | 417 | a | deck composition + per-point ngspice + `.meas` parsing = `klt sim` local/local-parallel/batch backends |
| gf180-gate-driver | `script_helpers.py` | 151 | a | DC-table parse, corner shim deck, raw `ngspice -b` for standalone scripts; `klt sim` dc analysis / `decks` requests (a few chip-local reference-log helpers inside) |
| gf180-gate-driver | `testbench.py` | 431 | a | manifest + netlist-fragment rules = `klt sim` request + 'circuit body, not a full deck' convention (`docs/cli/sim.md`) |
| gf180-tmds-tx | `cli.py` | 412 | c | per-repo experiment UX (`list`/`check-env`/`run <slug>` over `sim/<slug>/`); `print-env` is `klt pdk env` |
| gf180-tmds-tx | `corners.py` | 269 | a | `corners.process` bundles (`{name, sections}`) x `supply_v` x `temperature_c` x `params` in `klt sim`; named bundle presets stay in G3 |
| gf180-tmds-tx | `evidence_lint.py` | 786 | b | format/append-only/supersession lint of `sim/<slug>/records/`: G1 |
| gf180-tmds-tx | `pdk.py` | 185 | a | `klt pdk find`/`env` + `models.pdk` resolution in `klt sim` (`docs/cli/pdk.md`) |
| gf180-tmds-tx | `report.py` | 750 | b | append-only record minting (record id, never-overwrite, git provenance, dirty-tree flag, netlist snapshot, corner logs): G2; per-measurement summary/limit checks inside are `klt sim` `measurements[].limits` |
| gf180-tmds-tx | `runner.py` | 353 | a | deck composition + per-point ngspice + `.meas` parsing = `klt sim` local/local-parallel/batch backends |
| gf180-tmds-tx | `testbench.py` | 410 | a | manifest + netlist-fragment rules = `klt sim` request + 'circuit body, not a full deck' convention (`docs/cli/sim.md`) |
| gf180-usb2-phy | `cli.py` | 518 | c | per-repo experiment UX (`list`/`check-env`/`run <slug>` over `sim/<slug>/`); `print-env` is `klt pdk env` |
| gf180-usb2-phy | `corners.py` | 191 | a | `corners.process` bundles (`{name, sections}`) x `supply_v` x `temperature_c` x `params` in `klt sim`; named bundle presets stay in G3 |
| gf180-usb2-phy | `evidence_lint.py` | 607 | b | format/append-only/supersession lint of `sim/<slug>/records/`: G1 |
| gf180-usb2-phy | `klt_backend.py` | 579 | a | adapter that already shells to `klt sim` (corners or `decks` transport) and maps results back to `PointResult`; retires when the harness does |
| gf180-usb2-phy | `pdk.py` | 181 | a | `klt pdk find`/`env` + `models.pdk` resolution in `klt sim` (`docs/cli/pdk.md`) |
| gf180-usb2-phy | `report.py` | 602 | b | append-only record minting (record id, never-overwrite, git provenance, dirty-tree flag, netlist snapshot, corner logs): G2; per-measurement summary/limit checks inside are `klt sim` `measurements[].limits` |
| gf180-usb2-phy | `runner.py` | 297 | a | deck composition + per-point ngspice + `.meas` parsing = `klt sim` local/local-parallel/batch backends |
| gf180-usb2-phy | `testbench.py` | 355 | a | manifest + netlist-fragment rules = `klt sim` request + 'circuit body, not a full deck' convention (`docs/cli/sim.md`) |
| sky130-usb2-phy | `evidence_lint.py` | 264 | b | toolchain-free lint incl. append-only git check and re-derivation of PASS verdict from the raw envelope: G1 |
| sky130-usb2-phy | `matrix.py` | 230 | b | one canonical PVT matrix file (`sim/corners.json`) + request-vs-matrix conformance check + PDK-pin check: G3 (pin part G4) |
| sky130-usb2-phy | `mismatch.py` | 470 | c | receiver-specific MC mismatch runner; already built on `klt sim --batch-set` + `klt yield-samples` + `klt yield`, remaining logic is the chip's receiver/limits wiring (negative-control pairing: G5) |
| sky130-usb2-phy | `mismatch_lint.py` | 218 | b | toolchain-free lint of MC mismatch evidence (SHA-256 evidence dir, verdict re-derivation): G1 |
| sky130-usb2-phy | `results.py` | 230 | b | 'complete grid' definition: every matrix tuple once, simulated, measurements finite and inside bounds: G3 |
| sky130-usb2-phy | `runner.py` | 405 | b | mixed: `klt sim --backend batch` invocation (a), PDK/`.lib` section include evidence + env check (G4), evidence dir write (G2) |
| sky130-usb2-phy | `trim.py` | 80 | c | D+ pull-up trim-code selection: pure chip function |
| sg13g2-stdcells | `cli.py` | 228 | c | per-repo experiment UX (`list`/`check-env`/`run <slug>` over `sim/<slug>/`); `print-env` is `klt pdk env` |
| sg13g2-stdcells | `corners.py` | 206 | a | `corners.process` bundles (`{name, sections}`) x `supply_v` x `temperature_c` x `params` in `klt sim`; named bundle presets stay in G3 |
| sg13g2-stdcells | `evidence.py` | 133 | b | base evidence-record extension slot (`BaseExtensions`, record kinds): G1/G2 |
| sg13g2-stdcells | `pdk.py` | 241 | a | `klt pdk find`/`env` + `models.pdk` resolution in `klt sim` (`docs/cli/pdk.md`) |
| sg13g2-stdcells | `report.py` | 538 | b | append-only record minting (record id, never-overwrite, git provenance, dirty-tree flag, netlist snapshot, corner logs): G2; per-measurement summary/limit checks inside are `klt sim` `measurements[].limits` |
| sg13g2-stdcells | `runner.py` | 295 | a | deck composition + per-point ngspice + `.meas` parsing = `klt sim` local/local-parallel/batch backends |
| sg13g2-stdcells | `testbench.py` | 285 | a | manifest + netlist-fragment rules = `klt sim` request + 'circuit body, not a full deck' convention (`docs/cli/sim.md`) |
| sg13g2-stdcells | `toolchain.py` | 207 | b | pinned PDK/ngspice versions checked before any point is simulated (`sim/toolchain.json`): G4; the ngspice-major pin alone is `klt sim` `engine_version` |

Notes on single-repo modules (classified, not skipped):

- `trim` (sky130-usb2-phy) is (c): a pure D+ pull-up trim-code function.
- `mismatch_lint` (sky130-usb2-phy) is (b): the same lint concern as
  `evidence_lint`, applied to Monte Carlo evidence (SHA-256 evidence
  directory, verdict re-derived from raw files).
- `toolchain` (sg13g2-stdcells) is (b): the PDK + simulator pin and drift
  check run before any point is simulated. Only the ngspice-major part is
  covered today (`engine_version` in a `klt sim` request).
- `evidence` (sg13g2-stdcells) is (b): the base record-kind/extension slot.
  It is the closest any harness comes to a reusable core, and is the natural
  starting shape for G1/G2.

## Capability gaps (the (b) set)

Each is filed as its own `klt` issue (`loom:triage`). Candidates the issue
proposed were each confirmed or dropped against the survey.

| id | capability | motivating modules | status vs the issue's candidates |
|---|---|---|---|
| G1 ([#4043](https://github.com/2AMLogic/klayout-tools/issues/4043)) | evidence-record lint (format, append-only, supersession, evidence-dir hashes, verdict re-derivation) | `evidence_lint` x5 (bandgap, gate-driver, tmds-tx, usb2-phy gf180, usb2-phy sky130), `mismatch_lint`, `evidence` | confirmed; revisits #347's "convention only" |
| G2 ([#4044](https://github.com/2AMLogic/klayout-tools/issues/4044)) | evidence-record writer (record id, never-overwrite, git provenance + dirty-tree flag, netlist snapshot, per-corner logs) | `report` x5, sky130 `runner.write_evidence` | new; implied by "evidence-record lint" but a separate producer |
| G3 ([#4045](https://github.com/2AMLogic/klayout-tools/issues/4045)) | declared corner matrix + completeness/conformance check (and named process-corner bundle presets) | sky130 `matrix`, `results`; `report.matrix_conformance`; `corners` bundles | confirmed ("corner/PVT matrix definition"); the *axis mechanics* are already (a) |
| G4 ([#4046](https://github.com/2AMLogic/klayout-tools/issues/4046)) | toolchain/PDK pin + drift check before running | `toolchain`, sky130 `runner` env check, `matrix.pin_problem` | new; not in the issue's list |
| G5 ([#4047](https://github.com/2AMLogic/klayout-tools/issues/4047)) | Monte Carlo negative-control campaign shape (baseline + zero-sigma/skew control, draw CSV, disclosed non-convergence) | gate-driver `campaign`, sky130 `mismatch` | confirmed in part; draw execution, seeds and sample CSV are already (a) |

Dropped: nothing from the issue's candidate list was dropped. "Monte Carlo/
mismatch campaign helpers" narrowed to G5, because seeds, per-sample runs,
`yield-samples`, `yield-csv` and `yield-campaign` already cover the execution
half.

Overlap to resolve in each gap issue, not here: #347's spike and its
convention scripts (`check-pdk-pin.sh`, spread checks) already describe G1, G3
and G4 as repo-side conventions.

## Proposed migration order

Smallest harness first, each chip in its own PR **in the chip repo**, after
the (a) coverage is confirmed and the gaps it needs have landed:

1. `gf180-tmds-tx` (pilot; 3,165 LOC, no Monte Carlo, ported straight from
   bandgap, so the least bespoke). Needs only (a) plus G1/G2 for its
   `report`/`evidence_lint`.
2. `sg13g2-stdcells` (2,133 LOC; already generic by design and consumed by
   `sg13g2-serdes`). Needs G1, G2, G4.
3. `gf180-bandgap` (3,388 LOC; source of the fork lineage). Needs G1, G2, G3.
4. `gf180-usb2-phy` (3,330 LOC; already has `klt_backend`, so this is mostly
   deleting the local runner).
5. `sky130-usb2-phy` (1,897 LOC; already on `klt`; only evidence layers move).
6. `gf180-gate-driver` (4,306 LOC; largest, and the only one needing G5).

Rules for every chip PR:

- One chip per PR.
- Evidence outputs byte-identical to the pre-migration harness for the same
  inputs, or a recorded and explained diff in the PR.
- Never edit ratified evidence or measured results; the migration changes
  tooling only.
- Re-verify each (a) module's behavioural equivalence in that PR; this survey
  classifies, it does not prove equivalence.

The pilot PR is filed in the chip repo (at `loom:triage`) after this survey
lands. It is out of scope for #4042.
