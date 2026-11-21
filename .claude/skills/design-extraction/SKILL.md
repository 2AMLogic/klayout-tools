---
name: "Design Pipeline: Extraction (S9)"
description: "klt extract ships schematic-equivalent extraction plus opt-in first-order lumped RC parasitics (--parasitics); klt pex re-runs S10 testbenches against the parasitic-annotated netlist and reports a per-corner schematic-vs-extracted delta. The RC model is first-order, not signoff-grade — open fidelity gaps are tracked in docs/design/extract-fidelity-roadmap.md and open issues."
domain: design-pipeline
type: skill
user-invocable: false
---

# S9 — Extraction

This is a **thin loader**, not the source of truth. The full stage graph,
per-stage contracts, and model-class matrix live in
[`docs/design/design-pipeline.md`](../../../docs/design/design-pipeline.md)
(§1 stage graph, §2 per-stage contracts, §3 model-class matrix, §4 gap map).
The authoritative, current CLI contracts are
[`docs/cli/extract.md`](../../../docs/cli/extract.md) (including its
"Parasitic (RC) extraction (`--parasitics`)" section),
[`docs/cli/pex.md`](../../../docs/cli/pex.md), and
[`docs/cli/lvs.md`](../../../docs/cli/lvs.md). This file does not restate
those contracts — read them for flags, fields, and exit codes.

## Status: shipped — schematic-equivalent extraction, RC parasitics, and the post-layout delta

- `klt extract` extracts a **schematic-equivalent** netlist (devices +
  connectivity) from a layout stream (#54, closed).
- `klt extract --parasitics` additionally emits **first-order lumped RC
  interconnect parasitics** as extra `R`/`C` cards plus a `parasitics` JSON
  block (#216 design decision, #217 implementation — both closed). It is
  opt-in and additive: without the flag the output is byte-identical to a
  schematic-equivalent extraction.
- `klt pex` drives that `--parasitics` extraction itself, re-runs one or
  more already-converged `klt sim` testbenches against both the schematic
  DUT and the extracted netlist (the extracted leg labelled
  `netlist_source: "extracted"`), and reports a **per-corner, per-spec-row
  schematic-vs-extracted `delta[]`** (Epic #709, closed). A `klt pex` report
  is the only evidence `klt signoff` accepts for an analog block's T1
  post-layout verification item (`docs/design-evidence-tiers.md` item 7).

So S9 → S10 can now produce a genuinely post-layout, parasitic-aware result
— within the limits of the model described under "Honesty constraint"
below.

## Contract (design doc §2, S9)

| | |
| --- | --- |
| Input artifact | S8-clean layout stream — i.e. Loop B already converged (design doc §1). |
| Output artifact | `klt extract`'s SPICE netlist: devices + connectivity, and with `--parasitics` the lumped-RC cards plus a `parasitics` JSON block whose `model` object (issue #728) states the model's scope machine-readably. Contract: [`docs/cli/extract.md`](../../../docs/cli/extract.md). |
| Entry criteria | Loop B converged (S8 exit criteria met) — extracting a DRC/LVS-dirty layout produces a netlist nothing downstream should trust. |
| Exit criteria | Extracted netlist elaborates cleanly and its device/net topology matches the S6 netlist (`klt lvs`); for the post-layout half, a `klt pex` run whose `delta[]` rows are comparable (see the diagnostics below) and whose `status` is read against the same `measurements[].limits` the schematic run used. |
| `klt` verbs | `klt extract` (with `--parasitics`), `klt lvs`, `klt pex` ([`docs/cli/pex.md`](../../../docs/cli/pex.md)), `klt sim` with `request.netlist_source` ([`docs/cli/sim.md`](../../../docs/cli/sim.md) → "Post-layout verification (`netlist_source`)"). For one critical net, `klt mom` can supply a directly-solved R/L/C that `--mom-rlc-net` substitutes into the extraction. |
| Failure modes | Parasitics that flip a measurement's pass/fail relative to the schematic-level S5 result — the reason S10 must re-run post-extraction rather than trust the pre-layout sizing pass. Also: a post-layout comparison that ran but is not actually comparable (pin-list, model, or body-bias divergence — see below). |

## What an agent should do at this stage

1. **Extract and LVS.** Run `klt extract` on the S8-clean layout and `klt
   lvs` to confirm the extraction matches the S6 schematic topologically.
2. **Run `klt pex`** with the S5/S10 testbench(es) that already converged
   against the schematic netlist (Loop A's exit criteria). Do not write a new
   testbench or relax any `measurements[].limits` — the point is an
   apples-to-apples rerun. Pass through the extraction flags the layout needs
   (`--deck-option`, `--pins`, `--label-layer`), and use `--critical-net` /
   `--distributed-rc` for the nets whose coupling or distributed R actually
   binds a spec. For a spec row no single `.meas` card can express, use
   `--measure-command` (see `docs/cli/pex.md`).
3. **Check comparability before reading the deltas.** `pin_count_mismatch`,
   `flat_dut_mismatch`, `model_mismatch`, `body_bias`, and
   `extraction.parasitic_integrity` each describe a way the two legs can
   fail to be the same circuit. Several are reported but never folded into
   `status` — a `"pass"` with a non-null `model_mismatch` or an `"unbiased"`
   `body_bias` is not a valid post-layout pass. Fix the cause (pin list,
   device flavour, well ties) and re-run.
4. **Gate on `status`, not the exit code**, and report the `delta[]` rows —
   a measured degradation per spec row — rather than a bare "post-layout
   OK". Worked examples: `examples/design-pipeline/` README, "S10 pex delta
   proof" and the sections that follow it.

The manual equivalent (`klt extract --parasitics`, then re-run the same `klt
sim` request with `"netlist_source": "extracted"` and compare the two runs'
`measurements[]`) is still valid and is documented in `docs/cli/sim.md` →
"Post-layout verification (`netlist_source`)"; `klt pex` is that workflow
packaged, with the comparability diagnostics above added.

## Honesty constraint: the RC model is first-order, not signoff-grade

`--parasitics` is a first-order lumped reduction built in-process, not a
wrapped PEX engine. Read `parasitics.model` (or `klt pex`'s
`extraction.model`) for the exact scope; in summary it models per-net ground
C and a star-distributed series R, vertical-overlap coupling, and lateral
coupling / a distributed ladder only for nets named with `--critical-net`.
It is quasi-static (no inductance, no frequency dependence) and uncalibrated
to silicon. State this when reporting an S10 post-layout result: the result
is "post-layout with first-order lumped RC," not a signoff-extractor result.

Do not hand-derive parasitics or edit an extracted netlist to make it look
more complete — a fabricated parasitic netlist would turn S10's
post-extraction pass into a false "simulation-verified" claim.

Known open fidelity gaps (verify state before citing; this list was checked
2026-10-12 and is a starting point, not exhaustive):

- [#2458](https://github.com/2AMLogic/klayout-tools/issues/2458) — per-level
  lumped R is summed in series, so via-stitched strapping raises reported
  resistance instead of lowering it.
- [#3053](https://github.com/2AMLogic/klayout-tools/issues/3053) — curated
  parasitics decks carry no via/contact cut resistance and no
  interconnect-resistance corners.
- [#3021](https://github.com/2AMLogic/klayout-tools/issues/3021) —
  vertical-overlap coupling double-counts a recognised MiM capacitor inside
  its own top-plate footprint.
- [#3090](https://github.com/2AMLogic/klayout-tools/issues/3090) — the MiM
  capacitor is emitted as a fixed-value `C` card, so PDK process corners
  cannot move it.
- [#3089](https://github.com/2AMLogic/klayout-tools/issues/3089) — no control
  over the substrate DC tie (fixed 1e12 shunt).
- [#3056](https://github.com/2AMLogic/klayout-tools/issues/3056) — no
  attribution mode to drop one net's R/C (or all coupling C) and re-measure.
- [#2957](https://github.com/2AMLogic/klayout-tools/issues/2957) — series R
  changed between releases without a disclosed model change.

The staged plan for the rest — full-layout lateral coupling with fringe
shielding, geometry-decomposed distributed RC, and field-solver
cross-checks — is
[`docs/design/extract-fidelity-roadmap.md`](../../../docs/design/extract-fidelity-roadmap.md).
If a fidelity gap causes concrete friction driving a real block through the
pipeline, comment on the matching open issue rather than filing a duplicate.

Aggregating the post-layout result with DRC/LVS and the rest into a signoff
verdict is S11 — see `.claude/skills/design-signoff/SKILL.md` and `klt
signoff` ([`docs/cli/signoff.md`](../../../docs/cli/signoff.md)).

## Model-class assignment (design doc §3)

**small-fast.** Tool invocation plus a structural netlist-match check; no
design judgment in a converging case.

**Escalation rule:** escalate to mid-tier when the extracted netlist
mismatches the pre-layout netlist unexpectedly, or when a `klt pex`
comparability diagnostic fires and the cause is not obvious (debugging a
topology or model discrepancy, not re-running the tool).

## Failure modes (recap)

- Parasitics flipping a measurement's pass/fail relative to the
  schematic-level S5 result, undetected because no post-extraction S10 pass
  ran.
- Reporting a `klt pex` `"pass"` as post-layout evidence while a
  comparability diagnostic (`model_mismatch`, `body_bias`, pin-list
  mismatch) says the two legs are not the same circuit.
- Overstating the first-order lumped-RC result as signoff-grade extraction.
