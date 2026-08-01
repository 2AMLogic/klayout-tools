---
name: "Design Pipeline: Extraction (S9)"
description: "klt extract ships a schematic-equivalent netlist (devices + connectivity); it has no RC parasitics yet — tracked by #216 (design decision, recorded) / #217 (implementation, unscheduled)."
domain: design-pipeline
type: skill
user-invocable: false
---

# S9 — Extraction

This is a **thin loader**, not the source of truth. The full stage graph,
per-stage contracts, and model-class matrix live in
[`docs/design/design-pipeline.md`](../../../docs/design/design-pipeline.md)
(§1 stage graph, §2 per-stage contracts, §3 model-class matrix, §4 gap map).
Re-read that doc's S9 entries directly if anything here seems stale — this
file only restates it for an agent entering the stage; as of this writing
that document still describes S9 as blocked on the now-closed #54 and has
not been reconciled with `klt extract` shipping (out of scope for this
skill file — see [`docs/cli/extract.md`](../../../docs/cli/extract.md) and
[`docs/cli/lvs.md`](../../../docs/cli/lvs.md) for the authoritative, current
CLI contracts).

## Status: partially shipped — schematic-equivalent extraction exists; RC parasitics do not

`klt extract` ([`docs/cli/extract.md`](../../../docs/cli/extract.md)) is
shipped and extracts a **schematic-equivalent** netlist (devices +
connectivity, sky130/gf180mcu) from a layout stream — the #54 gap this file
previously described is closed. What remains gapped is **RC parasitics**:
`klt extract`'s output carries no interconnect resistance/capacitance, so a
netlist produced today makes S9's output artifact only partially
"post-layout" (LVS-accurate, still schematic-accuracy for simulation
purposes). This specific gap is tracked by
[#216](https://github.com/2AMLogic/klayout-tools/issues/216) (friction
issue; design decision recorded in
[`docs/design/lvs-extraction-spike.md`](../../../docs/design/lvs-extraction-spike.md)
→ "Addendum (#216)") and
[#217](https://github.com/2AMLogic/klayout-tools/issues/217)
(implementation, unscheduled).

## Contract (design doc §2, S9 — RC-parasitics gap only; see note above)

| | |
| --- | --- |
| Input artifact | S8-clean layout stream — i.e. Loop B already converged (design doc §1). |
| Output artifact | `klt extract`'s SPICE netlist today: devices + connectivity, no parasitics (shipped). Extracted netlist *with* RC parasitics: format decided by #216's design record (`--parasitics` flag, additive to the existing contract); **not implemented** (#217). |
| Entry criteria | Loop B converged (S8 exit criteria met) — extracting a DRC/LVS-dirty layout produces a netlist nothing downstream should trust. |
| Exit criteria | Extracted netlist elaborates cleanly and its device/net topology matches the S6 netlist it was extracted from (itself an LVS-shaped check, `klt lvs`). Per the design doc's own open question, `environment` should record which side of the loop — schematic vs. extracted — a given netlist represents, once a contract exists. |
| `klt` verbs | `klt extract` (schematic-equivalent, shipped), `klt lvs` (shipped). RC-parasitic extraction: none yet — see #216/#217. |
| Failure modes | Parasitics that flip a measurement's pass/fail relative to the schematic-level S5 result — the entire reason S10 must re-run post-extraction rather than trusting the pre-layout sizing pass. Today's `klt extract` output cannot surface this class of failure at all (no parasitics), so an S10 pass against it remains schematic-accurate, not truly post-layout. |

## What an agent should do until RC parasitics ship (#216/#217)

Do not attempt to hand-derive parasitics or fabricate a parasitic-aware
"extracted" netlist without a real extraction engine backing the claim — a
fabricated parasitic netlist is worse than an honestly-missing one, since it
would silently downgrade S10's post-extraction pass into a false
"simulation-verified" result (the exact failure mode S10's own contract
warns against, design doc §2 S10). If a pipeline run reaches this stage:

1. Run `klt extract` for the schematic-equivalent netlist and `klt lvs` to
   confirm it matches the schematic (S9's topological half is not blocked).
2. Report the S10 pass run against that netlist as schematic-accurate, not
   as a post-layout/parasitic-verified pass — RC parasitics are not yet
   part of the loop (#216/#217).
3. If this gap causes concrete friction driving a real block through the
   pipeline (Epic #105 Phase 3's worked example), comment on #216 or #217
   rather than filing a duplicate.

## Model-class assignment (design doc §3)

**small-fast.** Tool invocation plus a structural netlist-match check; no
design judgment in a converging case.

**Escalation rule:** escalate to mid-tier when the extracted netlist
mismatches the pre-layout netlist unexpectedly (debugging a topology
discrepancy, not re-running the tool).

## Failure modes (recap)

- Parasitics flipping a measurement's pass/fail relative to the
  schematic-level S5 result, undetected because no post-extraction S10 pass
  ran.
- Silently downgrading a schematic-level ("simulated") result to a claimed
  "simulation-verified" result by skipping this stage's actual extraction.
