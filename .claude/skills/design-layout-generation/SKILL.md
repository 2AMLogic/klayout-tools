---
name: "Design Pipeline: Layout Generation (S7)"
description: "Stub — map a sized netlist onto generator parameters to produce a GDSII/OASIS layout. No generation verb exists yet; klt render and klt layout-metrics are usable today for inspecting a hand-edited or externally produced layout."
domain: design-pipeline
type: skill
user-invocable: false
---

# S7 — Layout generation

This is a **thin loader**, not the source of truth. The full stage graph,
per-stage contracts, and model-class matrix live in
[`docs/design/design-pipeline.md`](../../../docs/design/design-pipeline.md)
(§1 stage graph, §2 per-stage contracts, §3 model-class matrix, §4 gap map).
Re-read that doc's S7 entries directly if anything here seems stale — this
file only restates it for an agent entering the stage.

## Status: blocked — no layout-generation verb exists

There is no `klt` command that maps a sized netlist onto a generator and
emits layout. The framework survey and generator-contract proposal are
settled in `docs/design/layout-generator-spike.md` (issue #104 — **closed**
as a design spike; the survey/decision work is done, but no generator has
shipped from it). The underlying gap this skill exists to flag —
"an agent cannot generate a layout from a netlist" — is still open and is
what blocks Loop B (layout ↔ DRC/LVS, design doc §1) from running end to end.

Until a generator ships, this stage is not runnable by an agent unaided:
layout must be produced by some other means (hand-edited, imported, or
produced outside this pipeline) before S7's exit criteria can be evaluated.

## Contract (design doc §2, S7)

| | |
| --- | --- |
| Input artifact | S6's netlist (elaborated, matches S4/S5's intent) + S5's sized device parameters — a generator needs both connectivity and sizes. |
| Output artifact | GDSII/OASIS layout stream, plus a structured report (ports, bounding box, DRC-relevant metadata) — the shape proposed in #104's spike. **Not implemented.** |
| Entry criteria | Netlist elaborates cleanly (S6 exit criteria met). |
| Exit criteria | Loop B's convergence criterion (design doc §1): zero DRC violations and clean LVS. |
| `klt` verbs | None for generation — blocked (see above). `klt render` (visual inspection of an existing layout) and `klt layout-metrics` (area/utilization aggregation) are shipped and usable today against whatever layout exists, regardless of how it was produced. |
| Failure modes | Loop B's stuck condition (design doc §1: violation count not monotonically decreasing, a "fixed" rule re-firing, or a DRC fix breaking LVS); a generator producing DRC-clean geometry whose connectivity doesn't match S6 — an LVS failure masquerading as a DRC pass. |

## What an agent can do today

1. **Inspect an existing layout.** `klt render <file>` produces one PNG per
   non-empty layer for visual review (`docs/cli/render.md`).
2. **Aggregate block-level metrics.** `klt layout-metrics <block> [--deck
   sky130|gf180mcu]` normalizes `klt layers`/`klt cells`/`klt drc` output for
   a block directory into `layout.json`, including area/utilization-relevant
   counts and (with `--deck`) a DRC summary (`docs/cli/layout-metrics.md`).
3. **Enter S8 (DRC/LVS) against whatever layout exists** once one has been
   produced by some other means — S8's skill (`../design-drc-lvs/SKILL.md`)
   picks up from there.

Do not attempt to synthesize layout geometry from a netlist without a
generator tool backing the claim — that is exactly the failure mode this
stage exists to prevent (an unaided agent hand-drawing layout is not what
"generation" means in the vision sentence, and hand-drawn geometry has no
generator-contract provenance for later stages to trust).

## Model-class assignment (design doc §3)

**mid-tier.** Mapping sized devices onto generator parameters (grid,
matching, guard rings) requires layout-idiom judgment even when the
generator itself is mechanical — not a frontier-reasoning task once a
generator exists, but not small-fast mechanical transformation either.

**Escalation rule:** escalate to frontier-reasoning when no generator
primitive fits the block's topology (a floorplan-level decision, not a
parameter tweak). Presently **unconditional** — every entry into this stage
escalates (or is deferred to a human) until #104's generator gap closes,
since there is no generator to select a primitive from at all.

## Failure modes (recap)

- Loop B's stuck condition (design doc §1) once a generator exists.
- Reporting this stage as "done" against a hand-edited or externally
  produced layout without noting it did not go through a generator — loses
  the provenance later stages (and signoff, S11) would want.
- Treating `klt render`/`klt layout-metrics` output as evidence of
  generation success — they inspect a layout, they do not produce one.
