---
name: "Design Pipeline: Extraction (S9)"
description: "klt extract ships a schematic-equivalent netlist (devices + connectivity) and, behind --parasitics, a first-order lumped RC model (#217). Net-to-net coupling is still not modelled."
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

## Status: shipped, with a stated accuracy boundary

`klt extract` ([`docs/cli/extract.md`](../../../docs/cli/extract.md)) is
shipped for sky130/gf180mcu and extracts a **schematic-equivalent** netlist
(devices + connectivity) from a layout stream — the #54 gap this file
previously described is closed. Since
[#217](https://github.com/2AMLogic/klayout-tools/issues/217) it also carries
**first-order interconnect parasitics** behind the opt-in `--parasitics`
flag (the interface decided by
[#216](https://github.com/2AMLogic/klayout-tools/issues/216), recorded in
[`docs/design/lvs-extraction-spike.md`](../../../docs/design/lvs-extraction-spike.md)
→ "Addendum (#216)"): one lumped series R plus one capacitance-to-ground per
net, from curated per-PDK sheet-resistance and area/perimeter-capacitance
coefficients.

**What is still not modelled** (read `docs/cli/extract.md` → "Parasitic
extraction" before making any accuracy claim): net-to-net **coupling**
capacitance, IR drop / driver-to-receiver wire delay (the series R is in
series with the net's own capacitance, not in the signal path), contact/via
resistance, and any calibration against silicon.

## Contract (design doc §2, S9)

| | |
| --- | --- |
| Input artifact | S8-clean layout stream — i.e. Loop B already converged (design doc §1). |
| Output artifact | `klt extract`'s SPICE netlist: devices + connectivity, plus lumped R/C primitives when `--parasitics` is given (one file either way, directly consumable by `klt sim`). |
| Entry criteria | Loop B converged (S8 exit criteria met) — extracting a DRC/LVS-dirty layout produces a netlist nothing downstream should trust. |
| Exit criteria | Extracted netlist elaborates cleanly and its device/net topology matches the S6 netlist it was extracted from (itself an LVS-shaped check, `klt lvs` — run against the netlist extracted **without** `--parasitics`; a parasitic-annotated netlist is isomorphic to no schematic reference). Per the design doc's own open question, `environment` should record which side of the loop — schematic vs. extracted — a given netlist represents, once a contract exists. |
| `klt` verbs | `klt extract` (schematic-equivalent + `--parasitics`), `klt lvs`. |
| Failure modes | Parasitics that flip a measurement's pass/fail relative to the schematic-level S5 result — the entire reason S10 must re-run post-extraction rather than trusting the pre-layout sizing pass. A `--parasitics` run surfaces the *net-capacitance-driven* half of that class; a coupling-driven failure is still invisible. |

## What an agent should do at this stage

Do not hand-derive parasitics or fabricate a parasitic-aware "extracted"
netlist — a fabricated parasitic netlist is worse than an honestly-missing
one, since it would silently downgrade S10's post-extraction pass into a
false "simulation-verified" result (the exact failure mode S10's own contract
warns against, design doc §2 S10). Instead:

1. Run `klt extract` (no flag) for the schematic-equivalent netlist and
   `klt lvs` against it to confirm it matches the schematic.
2. Re-run `klt extract --parasitics` for the netlist S10 simulates, and
   report that pass as *first-order parasitic-aware*, qualified by the
   not-modelled list above — never as full sign-off PEX.
3. If the missing terms (coupling capacitance in particular) cause concrete
   friction driving a real block through the pipeline (Epic #105 Phase 3's
   worked example), comment on #216 rather than filing a duplicate — that is
   the recorded home of the deferred second increment.

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
- Claiming full post-layout accuracy from a `--parasitics` run: the model is
  first-order and coupling-free by design.
- Silently downgrading a schematic-level ("simulated") result to a claimed
  "simulation-verified" result by skipping this stage's actual extraction.
