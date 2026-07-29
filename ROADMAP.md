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

## Phase 0 — scaffold (now)

Repo, license, CI skeleton, project site. No functional code yet; the README
states the interface contract we are building toward.

## Phase 1 — read

Parse GDSII/OASIS via KLayout's `pya` into clean Python objects. CLI: `klt
layers`, `klt cells`, `klt stats`, all with `--format json`. The test corpus
starts with open PDK example layouts (sky130).

## Phase 2 — check

`klt drc` running KLayout DRC decks headless with structured violation
output; deck adapters for open PDKs first. This is the ERC/DRC moment from
kicad-tools: the step that lets an agent know whether it is wrong.

## Phase 3 — write

Programmatic layout modification: cell instantiation, geometry ops,
parametric cells. Reusable blocks the way kicad-tools ships circuit blocks.

## Phase 4 — extract & verify

`klt lvs` and netlist/parasitic extraction, headless with structured
output — KLayout's LVS engine first, open-PDK decks (sky130). This is the
bridge from layout back to the electrical world: without an extracted
netlist there is nothing to simulate, so this phase gates the
simulation-verified end of the loop.

## Phase 5 — agent surface

MCP server exposing the toolkit; LLM reasoning module for layout decisions
(strategy in the model, geometry in the tools); worked examples designed
end-to-end by agents, published in a gallery at klayout-tools.org — the
kicad-tools.org pattern, one layer down.

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

Running alongside the tooling: a knowledge base of circuit designs from
published work — topologies, sizing strategies, layout idioms — for the
LLM reasoning module to draw on. Open sources only.

## Non-goals (for now)

Full-chip digital P&R and timing closure (block-level placement and
routing aids are fair game when an epic spikes them), and anything
requiring a proprietary PDK. Open PDKs are the arena; the point is the
agent-native surface, not competing with signoff tools.
