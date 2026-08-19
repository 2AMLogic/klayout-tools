# klayout-tools

[![CI](https://github.com/2AMLogic/klayout-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/2AMLogic/klayout-tools/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![PyPI](https://img.shields.io/pypi/v/klayout-tools)](https://pypi.org/project/klayout-tools/)
[![Status: early alpha](https://img.shields.io/badge/status-early%20alpha-yellow.svg)](ROADMAP.md)

**Tools for AI agents to work with IC layout.**

🌐 **[klayout-tools.org](https://klayout-tools.org)** — project site.

The [kicad-tools](https://github.com/rjwalters/kicad-tools) playbook, one
layer down the stack: standalone Python tools that let AI agents (LLMs,
autonomous coding assistants) parse, analyze, and manipulate chip layouts —
GDSII/OASIS streams, DRC decks, LVS — programmatically, headless, with
machine-readable JSON everywhere. Built on [KLayout](https://www.klayout.de)'s
Python API the way kicad-tools builds on KiCad's file formats: the heavy
lifting stays in the proven engine; the agent-native surface is ours.

The target capability: **an agent can take a spec through one of three
peer paths on an open PDK, unaided, with every step headless and
JSON-contracted — analog (spec → schematic/generator → sized circuit →
layout → DRC/LVS clean → extracted netlist → simulation-verified),
digital (spec → RTL → synthesis → place-and-route → DRC/LVS clean →
timing-closed), and mixed-signal (both paths plus the signoff seam
between them).** [ROADMAP.md](ROADMAP.md) holds the build order,
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) how the pieces fit; the
work itself is tracked in GitHub issues.

Built in the open by [2AM Logic](https://2amlogic.com).

## Why agent-focused?

Chip design tooling assumes a human at a GUI. `klayout-tools` provides what
an agent needs instead:

- **Structured data access** — layouts parsed into clean Python objects
- **Machine-readable output** — every CLI command supports `--format json`
- **Programmatic layout writing** — generate and edit layouts without a GUI
  (`klt gen`, `klt gen-compose`, `klt draw`)
- **MCP server** (planned) — expose the toolkit directly to agent frameworks
- **LLM reasoning interface** (planned) — purpose-built module for layout
  decisions, with geometric execution handled by tools, not tokens

## Status

Early alpha — [v0.2.0 is on PyPI](https://pypi.org/project/klayout-tools/)
with all 24 verbs — see [`docs/cli/`](docs/cli/). The
pattern is proven (see the kicad-tools [gallery](https://kicad-tools.org) of
boards designed end-to-end by agents); this repo is where it meets silicon.
See [ROADMAP.md](ROADMAP.md) for the build order and [CLAUDE.md](CLAUDE.md)
if you are an agent working here.

## Install

```bash
uv tool install klayout-tools
```

Or with `pip`:

```bash
pip install klayout-tools
```

`klt` is now on `PATH`. For the latest development version, install from
source instead:

```bash
uv tool install git+https://github.com/2AMLogic/klayout-tools
```

> **`klt yield` (and its `yield-campaign`/`yield-sensitivity` siblings) need
> an extra step.** Both install commands above ship the pure-Python package
> only — `klt yield`'s statistics run in a Rust extension
> (`klt_yield_native`) that is not published as a prebuilt wheel and is not
> in scope for a single-package install. It requires a full repo checkout
> plus a Rust toolchain; see
> [`docs/cli/yield.md#building-the-native-extension`](docs/cli/yield.md#building-the-native-extension).
> `klt mom` and `klt synthesize --restructure-timing` have the same
> from-source gap for their own Rust extensions; every other verb works
> from the commands above alone.

## Quick start

```bash
klt layers design.gds                    # enumerate layers, JSON out
klt cells design.gds --top               # cell hierarchy
klt drc design.gds --deck sky130        # run a DRC deck, structured results
klt precheck design.gds --grid-um 0.005  # off-grid/zero-area/naming hygiene checks
klt ring-check design.gds --layers '[[22,0],[34,0]]'  # guard/tap ring is a closed annulus
klt components design.gds --conductors '[{"name":"m1","layer":[68,20]}]'  # connected components, no deck
klt stats design.gds --per-layer         # densities, bbox, polygon counts
klt economy design.gds                   # utilization, whitespace map, bbox tightness, area-budget check
klt pdk find --pdk sky130A               # locate an installed PDK, JSON out
klt render design.gds                    # per-layer PNGs, headless
klt sim request.json                     # SPICE PVT corner sweep (ngspice), JSON out
klt size request.json                    # gm/Id sizing (single device or coupled diff-pair+mirror+tail), ngspice-scored
klt yield mc.json --limits spec.json     # MC sample set + spec limits -> yield estimate with CIs, Cpk, sample-size verdict (Rust core)
klt yield-campaign spec.json             # launch + manage the MC campaign itself, sharded via klt sim, then yield's own pipeline unmodified
klt yield-sensitivity campaign.json      # campaign parameter draws + output values -> ranked contribution to the spread (Rust core)
klt design-centering request.json        # yield-sensitivity ranking + sized device -> re-centering candidates
klt layout-metrics design.gds            # normalized layout.json per block
klt kb search bandgap                    # query the circuit-design knowledge base
klt gen resistor_strip --pdk sky130A     # generate a parametrized cell (headless PCell)
klt draw --params shapes.json -o out.gds # write a primitive stream (no rule checking)
klt extract design.gds --deck sky130     # layout -> schematic-equivalent netlist
klt pex design.gds request.json --deck sky130  # extracted (parasitic-annotated) netlist + schematic-vs-extracted delta report
klt mom design.gds stackup.json          # quasi-static capacitance matrix (Method of Moments, Rust core)
klt lvs request.json                     # compare extracted vs reference netlist
klt synthesize request.json              # RTL -> gate-level netlist (Yosys), JSON out
klt place-and-route request.json         # netlist -> placed+routed DEF/GDS (OpenROAD), JSON out
klt sta request.json                     # standalone timing/power analysis of an already-routed DEF (OpenSTA), no re-implementation
klt power routed.gds power.json          # routed power/ground nets -> resistive network + static IR-drop map
klt erc routed.gds erc.json --pdk sky130 # per-gate connectivity model + antenna-ratio verdict + core ERC findings (floating gate, unconnected/shorted net, missing tie)
klt functional-verification verify.json  # cocotb regression (Icarus/Verilator) -> pass/fail + coverage
klt equiv request.json                   # combinational equivalence (Yosys miter/SAT) -> proof or counterexample
klt eval descriptor.json --candidate '{"layout": "..."}'  # score a candidate: valid + one objective
klt gen-compose plan.json                # place + wire generated blocks into one circuit
klt socket-check design.gds --socket socket.json  # pins/outline/budgets vs a socket descriptor
klt lef-abstract design.gds --socket socket.json --macro-name m --cell-library sky130_fd_sc_hd  # layout+socket -> LEF MACRO abstract
klt report result.json                   # render a klt JSON report as markdown summary
klt signoff drc.json lvs.json            # aggregate drc/lvs/extract/sim JSON into one pass/fail verdict
klt trajectory run.jsonl --plot t.svg    # optimization trajectory -> milestone table + plot
klt deck resolve --content-hash sha256:... # pinned deck hash -> klayout-tools tag/version that shipped it
klt deck hash --deck sky130              # the deck content hash this build will use, no layout needed
klt deck info --deck gf180mcu            # this install's own deck hash, device coverage, release status -- no input layout needed
klt version --format json                # which build is this: version, commit, release or not
```

Every verb is documented in [`docs/cli/`](docs/cli/), one page per verb.
All 24 verbs ship in PyPI 0.2.0; the from-source install above tracks
`main`, which may be ahead of the latest release.

## Development

Dependencies are managed with [uv](https://docs.astral.sh/uv/); the `klayout`
pip wheel provides the headless Python API (no GUI, no source build needed).

```bash
uv sync --locked --extra dev    # create/refresh .venv from uv.lock

uv run --extra dev ruff check .     # lint
uv run --extra dev pytest           # tests

npm run check:ci                    # lint + tests — the same gate CI runs
```

`.github/workflows/ci.yml` runs `ruff check` plus `pytest` on Python
3.10–3.13 for every pull request and every push to `main`, so a red check is
the signal that a PR is not mergeable.

## GitHub Action

Run `klt` in a downstream block repo's CI with a few lines of workflow YAML
— `action.yml` at this repo's root installs `klt`, runs the verbs you
choose against your layout, and publishes a step summary + JSON/render
artifacts, exactly like a local `klt` invocation:

```yaml
- uses: 2AMLogic/klayout-tools@v0.2.0
  with:
    layout: layout/my_block.gds
    verbs: drc,layout-metrics
    deck: sky130
```

See [`docs/guides/github-action.md`](docs/guides/github-action.md) for the
full inputs/outputs reference and a complete worked example.

## Guides

- [Building KLayout from source on macOS](docs/guides/building-klayout-macos.md)
  — full walkthrough (Homebrew Qt6/Python/Ruby, `build4mac.py`, deploy,
  headless verification), tested on Apple Silicon with KLayout v0.30.10.
- [The `klt verify` GitHub Action](docs/guides/github-action.md) — reusable
  composite Action wrapping `klt` for downstream block repo CI: inputs,
  outputs, and a worked example.

## Agent skills

Curated procedures (with reference data) that agents working in this repo
load on demand:

- [spec-review](.claude/skills/spec-review/SKILL.md) — expert-EE opinion on
  a block's draft target spec: per-line achievability against published
  best practice (open literature, cited), evidence checks against the
  repo's device characterization, block-class completeness and
  corner-binding checks, and a ratify / ratify-with-amendments / defer
  verdict. Worked example: [`examples/spec-review/`](examples/spec-review/).
- **Staged design pipeline (S1–S6 + back-end)** — one skill per stage of
  [the design pipeline](docs/design/design-pipeline.md), from
  [proposal intake](.claude/skills/design-proposal-intake/SKILL.md) through
  [architecture partition](.claude/skills/design-architecture-partition/SKILL.md),
  [block spec](.claude/skills/design-block-spec/SKILL.md),
  [topology selection](.claude/skills/design-topology-selection/SKILL.md),
  [sizing](.claude/skills/design-sizing/SKILL.md), and
  [netlist authoring](.claude/skills/design-netlist-authoring/SKILL.md), plus
  the back-end stages ([DRC/LVS](.claude/skills/design-drc-lvs/SKILL.md),
  [layout generation](.claude/skills/design-layout-generation/SKILL.md),
  [extraction](.claude/skills/design-extraction/SKILL.md), and
  [signoff](.claude/skills/design-signoff/SKILL.md) — layout generation,
  extraction, and signoff's drc/lvs/extract/sim aggregation (`klt signoff`,
  #309) now run against shipped `klt` verbs; the skill still hand-assembles
  the parts `klt signoff` can't yet: the S3 spec diff and design-hygiene
  checklist items).

## Design notes

Spikes and engine surveys — proposals and findings, not commitments.
Full index: [`docs/design/`](docs/design/). What those surveys (and every
other mined resource — papers, courses, upstream repos) actually changed
here, one entry per resource with impact links, is indexed in the
[resource library](docs/library/README.md).

- [Staged agent design pipeline](docs/design/design-pipeline.md) — the
  spec-to-simulation-verified stage graph, per-stage input/output contracts,
  a vendor-neutral model-class matrix, and a gap map against today's `klt`
  verbs.
- [SPICE PVT corner runner](docs/design/spice-corner-runner-spike.md) —
  ngspice vs. Xyce, a proposed JSON contract for sweeping a netlist across a
  corner matrix, and the wrap/build call.
- [sc-leflib evaluation](docs/design/sc-leflib-evaluation.md) — whether
  siliconcompiler's LEF parser fills a gap that KLayout's own LEF/DEF reader
  leaves. Verdict: use `pya`, no new dependency.
- [Mixed-signal co-simulation approach](docs/design/co-simulation-approach-survey.md) —
  RNM vs. ngspice XSPICE `d_process` vs. Verilog-AMS/VHDL-AMS, a proposed
  co-simulation JSON contract with an additive backend selector, and the
  recommendation: RNM for v1.

## License

MIT. © 2026 Two AM Logic, Inc.
