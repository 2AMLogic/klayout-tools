---
name: "Design Pipeline: Extraction (S9)"
description: "Stub — extract a parasitic-aware netlist from a DRC/LVS-clean layout for post-layout simulation. No klt verb exists; blocked on #54, bundled with LVS."
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
file only restates it for an agent entering the stage.

## Status: blocked — no extraction verb exists

No `klt` command extracts a parasitic-aware netlist from a layout. Issue
**#54** (open) tracks this gap, bundled with S8's LVS half — per its curator
note, extraction and LVS are likely one friction issue and one engine
(`pya.LayoutToNetlist` / `pya.NetlistComparer`). This is a **full stub**:
there is no partial tool support to fall back on, unlike S7 (which at least
has `klt render`/`klt layout-metrics` for inspection).

## Contract (design doc §2, S9)

| | |
| --- | --- |
| Input artifact | S8-clean layout stream — i.e. Loop B already converged (design doc §1). |
| Output artifact | Extracted netlist with parasitics, format TBD by #54's eventual contract — the artifact that makes the S10 pass here "post-layout" rather than schematic-level. **Not implemented.** |
| Entry criteria | Loop B converged (S8 exit criteria met) — extracting a DRC/LVS-dirty layout produces a netlist nothing downstream should trust. |
| Exit criteria | Extracted netlist elaborates cleanly and its device/net topology matches the S6 netlist it was extracted from (itself an LVS-shaped check). Per the design doc's own open question, `environment` should record which side of the loop — schematic vs. extracted — a given netlist represents, once a contract exists. |
| `klt` verbs | None — blocked on #54. |
| Failure modes | Parasitics that flip a measurement's pass/fail relative to the schematic-level S5 result — the entire reason S10 must re-run post-extraction rather than trusting the pre-layout sizing pass. |

## What an agent should do until #54 ships

Do not attempt to hand-derive parasitics or fabricate an "extracted" netlist
without a real extraction engine backing the claim — a fabricated parasitic
netlist is worse than an honestly-missing one, since it would silently
downgrade S10's post-extraction pass into a false "simulation-verified"
result (the exact failure mode S10's own contract warns against, design doc
§2 S10). If a pipeline run reaches this stage:

1. Report S9 as blocked on #54, not as skipped or complete.
2. Do not proceed to a post-extraction S10 pass — a schematic-level S10 pass
   (S6-sourced netlist) remains valid and should be reported as such, but
   explicitly labeled pre-layout, not final.
3. If this gap causes concrete friction driving a real block through the
   pipeline (Epic #105 Phase 3's worked example), that is exactly the signal
   #54 already exists to capture — comment there rather than filing a
   duplicate.

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
