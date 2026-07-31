# klayout-tools

[![CI](https://github.com/2AMLogic/klayout-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/2AMLogic/klayout-tools/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Status: scaffold](https://img.shields.io/badge/status-scaffold-orange.svg)](ROADMAP.md)

**Tools for AI agents to work with IC layout.**

🌐 **[klayout-tools.org](https://klayout-tools.org)** — project site.

The [kicad-tools](https://github.com/rjwalters/kicad-tools) playbook, one
layer down the stack: standalone Python tools that let AI agents (LLMs,
autonomous coding assistants) parse, analyze, and manipulate chip layouts —
GDSII/OASIS streams, DRC decks, LVS — programmatically, headless, with
machine-readable JSON everywhere. Built on [KLayout](https://www.klayout.de)'s
Python API the way kicad-tools builds on KiCad's file formats: the heavy
lifting stays in the proven engine; the agent-native surface is ours.

The target capability: **an agent can take a spec → schematic/generator →
sized circuit → layout → DRC/LVS clean → extracted netlist →
simulation-verified, on an open PDK, unaided — with every step headless
and JSON-contracted.** [ROADMAP.md](ROADMAP.md) holds the build order,
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) how the pieces fit; the
work itself is tracked in GitHub issues.

Built in the open by [2AM Logic](https://2amlogic.com).

## Why agent-focused?

Chip design tooling assumes a human at a GUI. `klayout-tools` provides what
an agent needs instead:

- **Structured data access** — layouts parsed into clean Python objects
- **Machine-readable output** — every CLI command supports `--format json`
- **Programmatic modification** (planned) — edit layouts without a GUI
- **MCP server** (planned) — expose the toolkit directly to agent frameworks
- **LLM reasoning interface** (planned) — purpose-built module for layout
  decisions, with geometric execution handled by tools, not tokens

## Status

Scaffold. The pattern is proven (see the kicad-tools
[gallery](https://kicad-tools.org) of boards designed end-to-end by agents);
this repo is where it meets silicon. See [ROADMAP.md](ROADMAP.md) for the
build order and [CLAUDE.md](CLAUDE.md) if you are an agent working here.

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

## Quick start

```bash
klt layers design.gds                    # enumerate layers, JSON out
klt cells design.gds --top               # cell hierarchy
klt drc design.gds --deck sky130        # run a DRC deck, structured results
klt stats design.gds --per-layer         # densities, bbox, polygon counts
klt pdk find --pdk sky130A               # locate an installed PDK, JSON out
```

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

## Guides

- [Building KLayout from source on macOS](docs/guides/building-klayout-macos.md)
  — full walkthrough (Homebrew Qt6/Python/Ruby, `build4mac.py`, deploy,
  headless verification), tested on Apple Silicon with KLayout v0.30.10.

## Design notes

Spikes and engine surveys — proposals and findings, not commitments.

- [SPICE PVT corner runner](docs/design/spice-corner-runner-spike.md) —
  ngspice vs. Xyce, a proposed JSON contract for sweeping a netlist across a
  corner matrix, and the wrap/build call.
- [sc-leflib evaluation](docs/design/sc-leflib-evaluation.md) — whether
  siliconcompiler's LEF parser fills a gap that KLayout's own LEF/DEF reader
  leaves. Verdict: use `pya`, no new dependency.

## License

MIT. © 2026 Two AM Logic, Inc.
