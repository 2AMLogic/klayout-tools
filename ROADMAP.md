# Roadmap

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

## Phase 4 — agent surface

MCP server exposing the toolkit; LLM reasoning module for layout decisions
(strategy in the model, geometry in the tools); worked examples designed
end-to-end by agents, published in a gallery at klayout-tools.org — the
kicad-tools.org pattern, one layer down.

## How progress is driven

The forcing function is real IP, not synthetic examples. We use
klayout-tools daily to push actual block designs forward, and every place
the tools bind, chafe, or fall short becomes a friction issue filed here.
kicad-tools grew its router and DRC surface exactly this way, board by
board.

## Non-goals (for now)

Full P&R, timing closure, and anything requiring a proprietary PDK. Open
PDKs are the arena; the point is the agent-native surface, not competing
with signoff tools.
