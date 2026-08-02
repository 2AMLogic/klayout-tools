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
| Exit criteria | Extracted netlist elaborates cleanly and its device/net topology matches the S6 netlist it was extracted from (itself an LVS-shaped check, `klt lvs`). `klt sim`'s optional `request.netlist_source` field (`"schematic"` \| `"extracted"`, echoed into the response's `environment.netlist_source` when provided) now closes the design doc's open question — see "Post-layout re-verification workflow" below. |
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

## Post-layout re-verification workflow (netlist_source)

Once Loop B converges (S8 clean) and S9 extraction/LVS pass, re-run the
**same** S5 sizing testbench/corner-matrix request through `klt sim` against
the extracted netlist — same stimuli, same measurement limits — labeled with
`request.netlist_source: "extracted"` (contract: `docs/cli/sim.md`, "Post-layout
verification"):

1. `klt extract` the S8-clean layout to produce the SPICE netlist; `klt lvs`
   to confirm it matches the S6 schematic topologically (this section's
   "Contract" table, above).
2. Take the S5/S10 request that already converged against the schematic
   netlist (Loop A's exit criteria) and point its `netlist` field at the
   extracted netlist instead — do not write a new testbench or relax any
   `measurements[].limits`; the whole point is an apples-to-apples rerun.
3. Add `"netlist_source": "extracted"` to that request (the schematic-side
   run may set `"schematic"`, or omit the field — both are equivalent by
   convention) and run `klt sim`.
4. Compare the two runs' `status`/`measurements[]`: a pass-to-fail flip
   between the schematic-labeled and extracted-labeled runs signals
   device-parameter drift the layout introduced (e.g. a resistor drawn at
   the wrong length) — catchable *today*, even before RC parasitics land.

**Honesty constraint (per this file's "Status" section above):** until RC
parasitic extraction ships (#216/#217), `klt extract`'s netlist carries no
interconnect R/C, so an `"extracted"`-labeled pass mostly re-proves LVS
correctness rather than a true parasitic-aware post-layout result — still
worth running (it catches device-parameter drift and gives the signoff shape
a real second data point), just don't overstate what it proves. `klt sim`
applies no extra gating for this field; a corner still passes or fails on
exactly the per-corner measurement limits it always has.

Aggregating the two runs into a single signoff verdict (S11) is out of
scope here — `design-signoff`'s SKILL.md documents that stage as an
intentional stub.

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
