---
name: "Design Pipeline: Layout Generation (S7)"
description: "klt gen ships single-cell primitive generators (mos_array, res_array, guard_ring, diff_pair, bjt_array); klt gen-compose places and wires multiple generated blocks into one circuit (row placement + two-pin routing), verified end to end against a real sky130 5T OTA. klt render and klt layout-metrics are usable today for inspecting a hand-edited or externally produced layout."
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

## Status: unblocked — single- and multi-block circuits both runnable

**This used to be "blocked" for any circuit needing more than one generated
block** — that was accurate before Epic #191 (`klt gen-compose`) shipped,
but is now stale. `klt gen` ships five single-cell primitive generators —
`resistor_strip`, `mos_array`, `res_array`, `guard_ring`, `diff_pair`,
`bjt_array` (`docs/cli/gen.md`) — each producing a GDS/OASIS stream plus a
structured report (`ports[]`, `bbox_um`, `drc_hints`) per the contract
`docs/design/layout-generator-spike.md` (issue #104) settled. `klt
gen-compose` (`docs/cli/gen-compose.md`) then places a set of already-
generated blocks into one row and routes two-pin nets between their named
ports, per `docs/design/gen-composition-spike.md` (issue #186)'s
build-native-not-wrap decision. Phase 3 (#196) proved this against the real
5T OTA case this stage's own design doc names as the motivating example (a
differential pair, a current-mirror load, and a tail current source), taken
cleanly through `klt gen-compose` -> `klt extract` -> `klt lvs`.

**Use `klt gen-compose` for any circuit needing more than one generated
block wired together.** Two caveats to know before composing, both found
during #196's bring-up:

- **Wire opposite-facing port pairs only, and disable
  `add_guard_ring`.** Every phase-2 generator's `_D` ports face east and
  `_S` ports face west regardless of a block's row position; connecting two
  same-facing ports (e.g. `_D` to `_D` across adjacent blocks — the naive
  "tie both drains together" reading of a current-mirror load) or routing
  into a guard-ringed block from outside both draw a route straight through
  intervening geometry, which would produce a spurious device-level short.
  `klt gen-compose` now **detects and rejects** both cases (`routed: false`
  in `unrouted_nets[]` with a `drc_hints.notes[]` reason — #199, fixed) —
  neither case is *routable* yet, only caught, so the workaround is still
  required: `docs/cli/gen-compose.md`'s worked example shows the working
  pattern: place blocks so a connection only ever needs to reach the
  *nearer* (outer) same-row pin of its target, and pass
  `"add_guard_ring": false` to any block an external net reaches into.
- **A composed circuit's connectivity nets are not addressable from `klt
  sim`.** `klt gen-compose` draws no net labels, so `klt extract`'s
  pin-promotion keeps only the deck's one globally-connected net (the
  substrate tie) as a real `.SUBCKT` pin — every other net, including the
  ones `klt gen-compose` just wired, is unreachable from a `klt sim`
  testbench (#200). Loop B (DRC/LVS) is unaffected; **post-extraction `klt
  sim` bias/measurement is still blocked** for a composed circuit's own
  gates/bias/supply nodes until #200 closes.

## Contract (design doc §2, S7)

| | |
| --- | --- |
| Input artifact | S6's netlist (elaborated, matches S4/S5's intent) + S5's sized device parameters — a generator needs both connectivity and sizes. |
| Output artifact | GDSII/OASIS layout stream, plus a structured report (ports, bounding box, DRC-relevant metadata) — the shape `docs/cli/gen.md` documents for a single generator call, or `docs/cli/gen-compose.md`'s composed-cell shape (per-block offsets, per-net routing result) for multiple blocks wired together. |
| Entry criteria | Netlist elaborates cleanly (S6 exit criteria met). |
| Exit criteria | Loop B's convergence criterion (design doc §1): zero DRC violations and clean LVS. |
| `klt` verbs | `klt gen` (`docs/cli/gen.md`) for a single primitive generator call, and `klt gen-compose` (`docs/cli/gen-compose.md`) to place + wire multiple generator outputs into one circuit — both shipped and usable today (see caveats above). `klt render` (visual inspection of an existing layout) and `klt layout-metrics` (area/utilization aggregation) are also shipped and usable today against whatever layout exists, regardless of how it was produced. |
| Failure modes | Loop B's stuck condition (design doc §1: violation count not monotonically decreasing, a "fixed" rule re-firing, or a DRC fix breaking LVS); a generator producing DRC-clean geometry whose connectivity doesn't match S6 — an LVS failure masquerading as a DRC pass; for a composed circuit, a same-facing-port pair or an inbound route to a guard-ringed block, both reported unroutable by `klt gen-compose` itself (`unrouted_nets[]`, exit `3` — #199, fixed) rather than needing a later `klt extract` pass to notice. |

## What an agent can do today

1. **Run a single `klt gen` primitive generator.** `klt gen <generator>
   --pdk <variant> -o <path>` produces a DRC-clean single-cell block
   (`resistor_strip`, `mos_array`, `res_array`, `guard_ring`, `diff_pair`,
   `bjt_array` — `docs/cli/gen.md`). For any of the matched-device
   generators (`mos_array`/`res_array`/`bjt_array`/`diff_pair`) sized
   against a stated matching/offset spec, convert that spec into a minimum
   unit-device `W·L` (and hence `w_um`/`l_um`/multiplier-count parameters)
   via Pelgrom's law **before** picking generator params — see
   [`docs/design/matching-and-floorplanning.md`](../../../docs/design/matching-and-floorplanning.md)
   for the law, where to source the process's real matching constants, and
   the worked spec-to-parameter conversion (including the
   `topology="common_centroid"` vs. plain-`"array"` choice).
2. **Compose multiple generated blocks into one placed-and-routed
   circuit.** `klt gen-compose <request.json>` places a row of already-
   generated blocks and routes two-pin nets between their named ports
   (`docs/cli/gen-compose.md`) — read that document's "Known limitations"
   section and worked example before wiring a real circuit (the
   opposite-facing-ports / no-guard-ring caveats above).
3. **Inspect an existing layout.** `klt render <file>` produces one PNG per
   non-empty layer for visual review (`docs/cli/render.md`).
4. **Aggregate block-level metrics.** `klt layout-metrics <block> [--deck
   sky130|gf180mcu]` normalizes `klt layers`/`klt cells`/`klt drc` output for
   a block directory into `layout.json`, including area/utilization-relevant
   counts and (with `--deck`) a DRC summary (`docs/cli/layout-metrics.md`).
5. **Enter S8 (DRC/LVS) against whatever layout exists** once one has been
   produced by some other means — S8's skill (`../design-drc-lvs/SKILL.md`)
   picks up from there.

Do not attempt to synthesize layout geometry from a netlist without a
generator tool backing the claim — that is exactly the failure mode this
stage exists to prevent (an unaided agent hand-drawing layout is not what
"generation" means in the vision sentence, and hand-drawn geometry has no
generator-contract provenance for later stages to trust).

## Model-class assignment (design doc §3)

**mid-tier.** Mapping sized devices onto generator parameters (grid,
matching, guard rings) — and, for a multi-block circuit, choosing a
`placement.order` and `connectivity[]` that both realise the intended
topology and avoid the routing gaps above — requires layout-idiom judgment
even when the generator itself is mechanical — not a frontier-reasoning
task once a generator exists, but not small-fast mechanical transformation
either.

**Escalation rule:** escalate to frontier-reasoning when no generator
primitive fits the block's topology (a floorplan-level decision, not a
parameter tweak), **or** when a multi-block circuit's needed connectivity
cannot be expressed as `klt gen-compose`'s two-pin, opposite-facing-port
routing (e.g. a genuine >2-pin bundle net, or a topology requiring
`"grid"` placement) — both are floorplan-level gaps this phase's tooling
does not cover, not parameter tweaks.

## Failure modes (recap)

- Loop B's stuck condition (design doc §1) once a generator exists.
- Reporting this stage as "done" against a hand-edited or externally
  produced layout without noting it did not go through a generator — loses
  the provenance later stages (and signoff, S11) would want.
- Treating `klt render`/`klt layout-metrics` output as evidence of
  generation success — they inspect a layout, they do not produce one.
- Treating `klt gen-compose`'s `routed: true`/exit `0` as a DRC-clean
  guarantee — geometry is advisory; `klt drc`/`klt extract` remain the
  rule-compliance and connectivity authorities. `klt gen-compose` itself
  now catches the two known routing-collision shorts (same-facing port
  pairs, guard-ring crossings — #199, fixed), but a residual gap (e.g.
  `"grid"`-placement obstacle avoidance) could still slip through
  undetected — verifying a composed circuit with `klt extract`/`klt drc`
  before trusting it remains good practice.
