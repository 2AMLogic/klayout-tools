# Architecture

## Vision

An agent can take a spec through one of three peer paths on an open PDK,
unaided, with every step headless and JSON-contracted — analog (spec →
schematic/generator → sized circuit → layout → DRC/LVS clean →
extracted netlist → simulation-verified), digital (spec → RTL →
synthesis → place-and-route → DRC/LVS clean → timing-closed), and
mixed-signal (both paths plus the signoff seam between them).

That closed loop, across all three paths, is the target capability.
Everything below is in service of it. This is a multiyear effort;
complex and difficult work is in scope when it serves the loop. Analog
is the path with tooling today; digital and mixed-signal enter the same
way every engine class does — contract-first, wrapped rather than
absorbed, through a spiked epic (#391, #393) — not by loosening the
headless rule or the wrap-don't-absorb gate.

## Scope

The name means "tools an agent uses to do IC design with KLayout in the
loop," not "wrappers around KLayout." KLayout anchors the repo the same
way KiCad anchors kicad-tools: it is the file-format substrate
(GDSII/OASIS) and the visualization surface a human reaches for, and it
happens to also be the first geometry/DRC/LVS engine behind the
contracts. The repo's capabilities are not bounded by what KLayout
itself does — kicad-tools ships a router, a placement optimizer, and an
LLM reasoning module that KiCad has no equivalent of, and the same
pattern applies here.

Concretely in scope, in one repo, when the loop demands them: open-PDK
data management, extracted device/parasitic lookup tables, SPICE and
E&M simulation, circuit generators, optimization, and the knowledge
base. What keeps this from sprawling is not the repo boundary but the
gates below: every capability arrives contract-first, engines are
wrapped rather than absorbed, and new engine classes enter through a
spiked epic. The headless rule is unchanged — the KLayout GUI is a
viewer humans (and agents, via screenshots) benefit from, never a
dependency any command requires.

## Layers

```
agent surface     CLI (`klt`) + MCP server + LLM reasoning module
contracts         JSON schemas — the stable API
engines           KLayout, SPICE simulators, extractors, solvers, …
```

**Contract-first.** The JSON schemas are the API; engines are
implementation details behind them. A capability is defined by its
contract (inputs, outputs, error shape), not by the engine that happens
to implement it today. This is what makes engines swappable — and what
makes later rewrites safe rather than bet-the-project.

**Wrap the proven engine.** The heavy lifting stays in an existing,
battle-tested engine; the agent-native surface is ours. KLayout is the
first engine, not a submodule or fork — a dependency behind the contract.
Simulation, extraction, and E&M engines join the same way: wrapped, not
vendored. Forking or vendoring is justified only when we are actually
patching the engine, and then as a fork under our org.

## Rewrite rule

The long-term intent is to replace wrapped engines with clean, modern
implementations (Rust, Python bindings via pyo3) where that pays. Rewrites
are sequenced by a decision rule, not by ambition. Rewrite an engine only
when all three hold:

1. **Bottleneck or ceiling** — it is a measured bottleneck or capability
   ceiling for the agent loop.
2. **Oracle exists** — the domain is spec-testable against the wrapped
   engine (run both, diff the JSON).
3. **Unlock** — the rewrite enables something the wrapper structurally
   cannot (e.g. incremental DRC inside an agent's edit loop).

**Where Rust code lives**: `native/<engine>/`, one self-contained
`maturin`-built [pyo3](https://pyo3.rs/) crate per engine, each with its own
`Cargo.toml` + `pyproject.toml` and reached from the top-level
`pyproject.toml` through a PEP 735 dependency group plus `[tool.uv.sources]`.
That keeps the Rust toolchain an **optional** build dependency — the rest of
`klt` installs and runs without it — and keeps every other command's
packaging untouched. `native/mom/` (the `klt mom` capacitance solver, issue
#718) established this convention and is the reference for the next one.

Good early targets: the geometry layer — GDSII/OASIS parsing, polygon
ops, hierarchy traversal, eventually a DRC engine. Poor targets: SPICE
numerics and device models (BSIM4/PSP are a moving target maintained by
the Compact Model Coalition; convergence heuristics encode decades of
edge cases). Where the outside world already ships the modern
implementation — OpenVAF is a Rust Verilog-A compiler — we leverage it,
we don't compete with it.

## Mining the outside world

We constantly survey published tools, frameworks, and papers for ideas
and engines: BAG-style generators, OpenVAF, openEMS, magic/netgen, Xyce,
and whatever else the field produces. No single framework is assumed to
be the answer; we take engines where they are sound and ideas where the
code is not. Survey findings land as issues or epics, not as unexamined
dependencies.

## In-house prior art

Two sibling repos are first-call sources when their epics are spiked:

- **[geode-fem](https://github.com/rjwalters/geode-fem)** (`../geode-fem`)
  — Burn-based Rust FEM/DG electromagnetic solver (with sister FDTD
  project strata-fdtd). Driven + eigenmode solves, wave ports,
  S-parameter extraction, validated against analytic oracles — including
  spiral-inductor L/Q benchmarks, exactly the passives an IC PHY needs.
  When the E&M simulation epic is spiked, geode-fem is a candidate
  engine, not just an idea source; being differentiable end-to-end, it
  also feeds the optimization/inverse-design story.
- **[kicad-tools](https://github.com/rjwalters/kicad-tools)**
  (`../kicad-tools`) — beyond being the playbook for this repo's shape,
  its router (GPU-accelerated, native backend), CMA-ES placement
  optimizer, and LLM reasoning module solve PCB versions of problems
  ASIC layout shares: placement, routing, design-rule-constrained
  geometry. Port the ideas and architecture; the geometry and rule
  regimes differ enough that code moves case by case.

## Knowledge base

A workstream, parallel to the tooling: a structured knowledge base of
circuit designs from published work — topologies, sizing strategies,
layout idioms — that the LLM reasoning module can draw on. Open sources
only; same rule as PDKs.

## How capabilities arrive

New major capabilities — simulation, optimization, layout tooling beyond
the current phase — arrive by spiking a design epic first: a scoped
survey of candidate engines, a proposed JSON contract, and a build/wrap
decision, filed as an epic before implementation starts. The friction log
from real block designs (see ROADMAP) decides when an epic is spiked.
