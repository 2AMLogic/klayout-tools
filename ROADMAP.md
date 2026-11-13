# Roadmap

The target capability: an agent can take a spec through one of three peer
paths on an open PDK, unaided, with every step headless and
JSON-contracted — analog (spec → schematic/generator → sized circuit →
layout → DRC/LVS clean → extracted netlist → simulation-verified),
digital (spec → RTL → synthesis → place-and-route → DRC/LVS clean →
timing-closed), and mixed-signal (both paths plus the signoff seam
between them). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how
the pieces fit and how new engines are adopted.

**Status as of 2026-11-13, `main` @ `d9bc36811`.** Work is tracked in GitHub issues (Loom-orchestrated);
[WORK_PLAN.md](WORK_PLAN.md) is the generated per-label queue. This document says where we are going
and in what order, not who is doing what. When an issue or epic contradicts it, one of the two gets updated.

## Where it stands

Latest tag `v0.9.1`; `site/src/data/cli.generated.json` records 68 verbs, 95 MCP tools and 5 curated
decks, and `src/klayout_tools/pdk_families.py` names five families: sky130, gf180mcu, sg13g2,
sg13cmos5l, asap7. The build order mirrored kicad-tools, adapted to IC layout; the phases are kept here
as the record of how the surface was built.

| Phase | State | Evidence |
|---|---|---|
| 0 scaffold | done | repo, license, CI, project site |
| 1 read | done (v0.1.0) | `klt layers` / `cells` / `stats` |
| 2 check | shipped | `klt drc`, `precheck`, `socket-check`, `ring-check`, `seam-check`, `keepout-check`, `layout-metrics`; since joined by `erc`, `array-check`, `tap-distance`, `interface-check` (`docs/cli/`) |
| 3 write | shipped | `klt gen` / `gen-compose` / `draw`, per [layout-generator-spike](docs/design/layout-generator-spike.md); `layout-plan` family added since |
| 4 extract & verify | shipped | `klt extract` / `lvs` / `extract --parasitics` (#217) / `pex` (Epic #709, closed) |
| 5 agent surface | partial | gallery (`blocks/` → klayout-tools.org) and the generic MCP bridge (`klt mcp serve`) live; reasoning module not started |

Beyond the phases, each capability arrived by a design spike (docs/ARCHITECTURE.md, "How capabilities arrive"):

- **Analog path, end to end:** [`examples/design-pipeline/`](examples/design-pipeline/) runs a sky130 5T
  OTA from proposal through DRC, LVS, extraction, post-extraction sim and a `klt mom` cross-check.
  `klt sim` (ngspice; local, remote and batch backends) came from the
  [SPICE corner-runner spike](docs/design/spice-corner-runner-spike.md).
- **Digital path:** the Yosys + OpenROAD engine class (Epic #391, closed) ships as `klt synthesize`,
  `techmap`, `place-and-route`, `sta`, `equiv`, `formal`, `functional-verification` and `power`.
- **Mixed-signal seam:** Epic #393 is closed; `klt lef-abstract`, `interface-check` and `power-intent`
  exist. Co-simulation remains a proposal
  ([survey](docs/design/co-simulation-approach-survey.md), recommends real-number modeling for v1).
- **Extraction fidelity** ([roadmap](docs/design/extract-fidelity-roadmap.md)): Stage 0, Stage 2a (#760),
  scoped first increments of 2b (#976) and 3 (#977), a resistance graph (#2982), and the Method of
  Moments solver `klt mom` (Epic #701, closed). Stage 2c is not started.
- **Field simulation:** [em-field-sim-spike](docs/design/em-field-sim-spike.md) is still a proposal;
  the finite-element epic (#708) is back with the Architect for revision.
- **Changed since the last edit of this file (2026-10-27):** ERC now evaluates `erc.supply_short` through
  the substrate (#5342–#5345, signoff item 11 grading in PR #5366); `klt sim` batch runs gained shard
  splitting, partial collection and resume (#5364–#5387); `klt characterize` gained capacity wait,
  re-attach, concurrency and `--split-grid` (#5348–#5411); `run_extract` and `run_lvs` are being split
  into stage helpers (PR #5414, #5144).

## Now

Can start today; one PR each. Items marked *claimed* had a Builder lease on 2026-11-13.

| What | Done when | Issue |
|---|---|---|
| Report deck M1 tip violations on ASAP7 router V1 landing pads | `place-and-route` report counts `M1.S.2`–`M1.S.5` hits instead of `route_drc_violation_count: 0` | #4495 *claimed* |
| ASAP7 DRC fixture job breaches the CI wall-clock budget | budget check green on three consecutive `main` runs, or the measured budget is re-derived per `docs/guides/ci-wall-clock-budget.md` | #4372 *claimed* |
| Live-verify `floating_gate_allowed` on ASAP7 `DFFASRHQNx1` | `klt erc` envelope recorded on the issue showing the three named gates reach M1 | #4827 *claimed* |
| sky130 native-Vt NMOS in `klt gen` and `klt lvs` | a generated `nfet_05v0_nvt` device reaches LVS `match` in a test | #2912 |
| gf180mcu DRC on a composed layout-plan GDS | `klt drc --deck gf180mcu` run recorded with no `*.ongrid.1` outside resistor cells | #4308 |
| Signoff item 11: live ERC/LVS agreement | side-by-side `klt erc` / `klt lvs` envelopes on a real layout cited on the issue (the CI-checkable half merged in PR #5366) | #5346 |
| README status is stale (says v0.5.0, 44 verbs) | README status and quick-start figures match `cli.generated.json`, with a test that fails when they drift; four unindexed `examples/` directories listed | seed 1 |
| MCP resources for request schemas | `klt mcp serve` lists each `docs/schemas/*.schema.json` as an MCP resource; test covers list and read | seed 2 |
| Extraction-fidelity roadmap status is stale | its header and summary table mark what shipped (#760, #976, #977, #2982) and what has not (2c) | seed 3 |

## Next (unblocked by a named item above or by curation)

- **Trim protruding ASAP7 `VIA_*` M1 pad tips** (#4508) after #4495 lands. Done: routed block clean on `M1.S.2`–`M1.S.5`.
- **Close out the substrate ERC work** (#5336, #5334) once #5346's live run is recorded.
- **`run_lvs` stage 4** (#5160) and the **next `run_extract` slice** (#5196): land in order, one
  behaviour-preserving move per PR; done when the file-size baseline tightens with each.
- **One shared SPICE inline-comment stripper** (#5417, a Hermit proposal awaiting promotion). Done: six local copies deleted.
- **`gen-compose` layer/track assignment across many mutually crossing nets** (#1962, in triage;
  design input in [gen-compose-track-assignment](docs/design/gen-compose-track-assignment.md)). Needs curation into builder-sized pieces first.
- **Extraction Stage 2c** (side-overlap fringe): the last unread coefficient family; done when per-net
  total capacitance is graded against the magic oracle per the fidelity roadmap. No issue yet; needs a scope note first because 2b is only partly shipped.
- **Hierarchical LVS of large regular arrays** (#5322): the CI-size criteria merged in PR #5326; the remaining full-size run awaits a decision on where it runs.

## Later

Direction; each needs its question answered first.

- **LLM reasoning module** (strategy in the model, geometry in the tools). Question: what does it add
  as a contract beyond the MCP bridge plus `klt kb`, and which friction issues would it close?
- **Curated per-verb MCP tools, prompts, HTTP transport** (out of scope of the first increment per
  `docs/guides/mcp-server.md`). Question: which verbs do agents misuse through the generic schema?
- **Tiny Tapeout corpus as regression and benchmark** (Epic #520; phase 1 is 3 of 4 closed). Question: #5084's open decision on where sharded runs execute and whether a DRC-only CI sample is acceptable.
- **Finite-element full-field solver** (#708). Question: does an existing engine clear the accuracy bar on real layout geometry before anything is built?
- **Mixed-signal co-simulation.** Question: commit to the surveyed real-number-modeling contract, or wait for a block that demands it?
- **A digital worked example** beside the analog one in `examples/`. Question: which CI host carries the real OpenROAD/Yosys binaries (see #4417)?
- **Knowledge base** of published circuit designs (`kb/`, open sources only) keeps growing alongside.

## Blocked and waiting

- **Operator-held (20 open issues carry `loom:operator`).** Live verifications that need real tool
  binaries or the batch fleet: #3289, #4409, #4417, #4449, #4845, #5153. Decisions: #3962 (how the
  `cli/parser.py` split continues), #4002, #4091, #5084. Reported defects awaiting a ruling or
  reproduction: #4675, #4902, #5002, #5074, #5311, #5313, #5332, #5406. #4891 is fixed and awaits closing.
- **Parked PRs.** #2771, #3531, #4334, #4602, #4737 and #4772 were last reviewed in October with CI red
  for reasons the reviewers judged unrelated to the diff. Each needs a rebase onto current `main` and a
  fresh run; until then their issues (#2340, #3462, #4312, #2835, #4771, #3420) do not move.
- **Release automation** (#5005): cadence-gated 0.9.x patch releases wait on a publisher target outside this repo.
- **Remote-sim image for ihp-sg13g2** (#2570, #2574): operator-only build.
- **Fleet measurements:** #378, #485, #527, #528, #3522 need live fleet runs; #3073 and #4351 need a fleet-side primitive.
- **Tool-version or environment gated:** #3147 and #4075 (ASAP7 deck), #4277 and #4371 (real `sv2v`),
  #4397 and #5405 (SG13G2 runset, needs KLayout >= 0.29), #4410, #4424.

## How progress is driven

The forcing function is real IP, not synthetic examples. We use
klayout-tools daily to push actual block designs forward — PHY-class blocks
on open PDKs, starting on sky130 — and every place the tools bind, chafe,
or fall short becomes a friction issue filed here. Block-design repos start
private and go public once they clear a Tier-2 audit — the 2026-07-31
disclosure split for open-PDK canary repos — and wave 1 has already
flipped public; the friction is public throughout, win or lose on
disclosure timing. kicad-tools grew its router and DRC surface exactly
this way, board by board.

## Not planned (for now)

Full-chip digital P&R and timing closure (block-level placement and
routing aids are fair game when an epic spikes them), and anything
requiring a proprietary PDK. Open PDKs are the arena; the point is the
agent-native surface, not competing with signoff tools. A `pyo3`-backed
Rust core stays later-and-only-where-measured, per the rewrite rule.
