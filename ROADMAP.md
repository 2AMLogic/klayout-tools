# Roadmap

The target capability: an agent can take a spec → schematic/generator →
sized circuit → layout → DRC/LVS clean → extracted netlist →
simulation-verified, on an open PDK, unaided — with every step headless
and JSON-contracted. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for
how the pieces fit and how new engines are adopted.

Work is tracked in GitHub issues (Loom-orchestrated). This document and
the architecture doc exist to keep that work aligned with the long-term
vision — they say where we are going, not who is doing what. When an
issue or epic contradicts them, one of the two gets updated.

Mirroring the kicad-tools build order, adapted to IC layout. Each phase ends
with something an agent can use unaided and a worked example in `examples/`.

## Phase 0 — scaffold (done)

Repo, license, CI skeleton, project site.

## Phase 1 — read (done — v0.1.0)

Parse GDSII/OASIS via KLayout's `pya` into clean Python objects. CLI: `klt
layers`, `klt cells`, `klt stats`, all with `--format json`. The test corpus
starts with open PDK example layouts (sky130).

## Phase 2 — check (shipped — `klt drc` / `precheck` / `socket-check` / `ring-check` / `layout-metrics`)

`klt drc` running KLayout DRC decks headless with structured violation
output; deck adapters for open PDKs first. This is the ERC/DRC moment from
kicad-tools: the step that lets an agent know whether it is wrong. The
check family has since grown beyond DRC: `klt precheck` (layout hygiene),
`klt socket-check` (pins/outline/budgets vs a socket descriptor),
`klt ring-check` (guard/tap ring closure), and `klt layout-metrics`
(normalized per-block metrics).

## Phase 3 — write (shipped — `klt gen` / `gen-compose` / `draw`)

Programmatic layout modification: cell instantiation, geometry ops,
parametric cells. Reusable blocks the way kicad-tools ships circuit blocks.

This phase was spiked in
[docs/design/layout-generator-spike.md](docs/design/layout-generator-spike.md)
(survey of BAG3/xbase, laygo2, gdsfactory, ALIGN, MAGICAL, KLayout PCells)
and implemented per its recommendation as a Python/pya reference
implementation: `klt gen` (single-cell primitive generators), `klt
gen-compose` (place + wire generated blocks), and `klt draw` (primitive
stream writing). A `pyo3`-backed Rust core remains later-and-only-where-
measured, per the rewrite rule.

## Phase 4 — extract & verify (shipped — `klt extract` / `klt lvs`; RC parasitics open, #217)

`klt lvs` and netlist/parasitic extraction, headless with structured
output — KLayout's LVS engine first, open-PDK decks (sky130). This is the
bridge from layout back to the electrical world: without an extracted
netlist there is nothing to simulate, so this phase gates the
simulation-verified end of the loop.

## Phase 5 — agent surface (partial — gallery live; MCP + reasoning module outstanding)

MCP server exposing the toolkit; LLM reasoning module for layout decisions
(strategy in the model, geometry in the tools); worked examples designed
end-to-end by agents, published in a gallery at klayout-tools.org — the
kicad-tools.org pattern, one layer down. The gallery half is live
(`blocks/` → klayout-tools.org); the MCP server and reasoning module are
not yet started.

## How progress is driven

The forcing function is real IP, not synthetic examples. We use
klayout-tools daily to push actual block designs forward — PHY-class blocks
on open PDKs, starting on sky130 — and every place the tools bind, chafe,
or fall short becomes a friction issue filed here. The block designs
themselves live in private repos; the friction is public. kicad-tools grew
its router and DRC surface exactly this way, board by board.

## Beyond the phases

Simulation (SPICE, E&M), optimization, and circuit generators are on the
path to the closed loop but are not phase-scheduled: each arrives by
spiking a design epic — candidate-engine survey, proposed JSON contract,
wrap/build decision — when the friction log demands it (see
docs/ARCHITECTURE.md, "How capabilities arrive").

The SPICE side has been spiked:
[docs/design/spice-corner-runner-spike.md](docs/design/spice-corner-runner-spike.md)
surveys ngspice against Xyce, proposes the JSON contract for running a
netlist across a PVT corner matrix, and recommends wrapping the engine
while building the corner orchestration ourselves. Implemented as
`klt sim` (ngspice, local and remote AWS backends).

The E&M side has also been spiked:
[docs/design/em-field-sim-spike.md](docs/design/em-field-sim-spike.md)
surveys geode-fem, strata-fdtd, and openEMS, proposes a `klt em` JSON
contract, and recommends wrapping geode-fem as the default full-wave engine
— validated against openEMS and analytic oracles — once it clears an
accuracy bar on geometry produced from a real sky130 layout. Also a
proposal, not a commitment.

Running alongside the tooling: a knowledge base of circuit designs from
published work — topologies, sizing strategies, layout idioms — for the
LLM reasoning module to draw on. Open sources only.

## Non-goals (for now)

Full-chip digital P&R and timing closure (block-level placement and
routing aids are fair game when an epic spikes them), and anything
requiring a proprietary PDK. Open PDKs are the arena; the point is the
agent-native surface, not competing with signoff tools.
