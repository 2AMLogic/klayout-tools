---
name: "Design Pipeline: Layout Generation (S7)"
description: "Blocked — klt gen ships single-cell primitive generators (mos_array, res_array, guard_ring, diff_pair, bjt_array) but cannot compose multiple generated blocks into one placed-and-routed circuit. klt render and klt layout-metrics are usable today for inspecting a hand-edited or externally produced layout."
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

## Status: blocked — generators exist but don't compose

**This is not "no generator verb at all" anymore** — that was the correct
description before Epic #152 (`klt gen`, phases 1–4) shipped, but is now
stale. `klt gen` exists and ships five single-cell primitive generators —
`resistor_strip`, `mos_array`, `res_array`, `guard_ring`, `diff_pair`,
`bjt_array` (`docs/cli/gen.md`) — each producing a GDS/OASIS stream plus a
structured report (`ports[]`, `bbox_um`, `drc_hints`) per the contract
`docs/design/layout-generator-spike.md` (issue #104) settled.

**The current, accurate blocker is narrower: `klt gen` has no capability to
compose multiple generated primitives into one placed-and-routed circuit.**
A real block (e.g. a 5T OTA — a differential pair, a current-mirror load,
and a tail device, wired together) needs several generator outputs placed
relative to each other and connected by routed metal; nothing in `klt gen`
does that. This was a deliberate, explicitly-stated scope boundary of Epic
#152 at every phase, not an oversight — see
`docs/design/gen-composition-spike.md` (issue #186) for the survey of
placement/routing approaches and the build/wrap decision for closing this
specific gap. Until that capability ships, this stage is not runnable by an
agent unaided for any circuit needing more than one generator instance:
layout must be produced by some other means (hand-edited, imported, or
produced outside this pipeline) before S7's exit criteria can be evaluated
for such a circuit. A circuit realizable as exactly **one** `klt gen` call
(e.g. a single matched-device array with no further wiring to another
generated block) is already unblocked today.

## Contract (design doc §2, S7)

| | |
| --- | --- |
| Input artifact | S6's netlist (elaborated, matches S4/S5's intent) + S5's sized device parameters — a generator needs both connectivity and sizes. |
| Output artifact | GDSII/OASIS layout stream, plus a structured report (ports, bounding box, DRC-relevant metadata) — the shape `docs/cli/gen.md` documents for a single generator call. A multi-block, placed-and-routed circuit's equivalent output is proposed, not implemented, in `docs/design/gen-composition-spike.md` (#186). |
| Entry criteria | Netlist elaborates cleanly (S6 exit criteria met). |
| Exit criteria | Loop B's convergence criterion (design doc §1): zero DRC violations and clean LVS. |
| `klt` verbs | `klt gen` (`docs/cli/gen.md`) for a single primitive generator call — shipped and usable today. No verb for composing multiple generator outputs into one circuit yet — blocked (see above). `klt render` (visual inspection of an existing layout) and `klt layout-metrics` (area/utilization aggregation) are also shipped and usable today against whatever layout exists, regardless of how it was produced. |
| Failure modes | Loop B's stuck condition (design doc §1: violation count not monotonically decreasing, a "fixed" rule re-firing, or a DRC fix breaking LVS); a generator producing DRC-clean geometry whose connectivity doesn't match S6 — an LVS failure masquerading as a DRC pass. |

## What an agent can do today

1. **Run a single `klt gen` primitive generator.** `klt gen <generator>
   --pdk <variant> -o <path>` produces a DRC-clean single-cell block
   (`resistor_strip`, `mos_array`, `res_array`, `guard_ring`, `diff_pair`,
   `bjt_array` — `docs/cli/gen.md`). This is sufficient for any circuit that
   is exactly one matched-device group with no further wiring to another
   generated block; it is **not** sufficient for a multi-block circuit
   (composition is blocked — see above).
2. **Inspect an existing layout.** `klt render <file>` produces one PNG per
   non-empty layer for visual review (`docs/cli/render.md`).
3. **Aggregate block-level metrics.** `klt layout-metrics <block> [--deck
   sky130|gf180mcu]` normalizes `klt layers`/`klt cells`/`klt drc` output for
   a block directory into `layout.json`, including area/utilization-relevant
   counts and (with `--deck`) a DRC summary (`docs/cli/layout-metrics.md`).
4. **Enter S8 (DRC/LVS) against whatever layout exists** once one has been
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
parameter tweak). For a **single-generator** circuit this is now
conditional on the topology (a `klt gen` primitive may or may not fit).
For any circuit needing **more than one** generated block wired together,
escalation remains **unconditional** until #186's composition gap closes —
there is no way to place and route multiple generator outputs into one
circuit yet, regardless of how well each individual block's topology fits
an existing generator.

## Failure modes (recap)

- Loop B's stuck condition (design doc §1) once a generator exists.
- Reporting this stage as "done" against a hand-edited or externally
  produced layout without noting it did not go through a generator — loses
  the provenance later stages (and signoff, S11) would want.
- Treating `klt render`/`klt layout-metrics` output as evidence of
  generation success — they inspect a layout, they do not produce one.
