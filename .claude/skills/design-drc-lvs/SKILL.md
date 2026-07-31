---
name: "Design Pipeline: DRC/LVS (S8)"
description: "Iterate a layout against klt drc's violation report until clean (DRC half — fully specified and shipped); LVS half is a stub blocked on #54. Encodes Loop B's convergence and stuck criteria."
domain: design-pipeline
type: skill
user-invocable: false
---

# S8 — DRC/LVS

This is a **thin loader**, not the source of truth. The full stage graph,
per-stage contracts, and model-class matrix live in
[`docs/design/design-pipeline.md`](../../../docs/design/design-pipeline.md)
(§1 stage graph — Loop B — §2 per-stage contracts, §3 model-class matrix, §4
gap map). Re-read that doc's S8 entries directly if anything here seems
stale — this file only restates it for an agent entering the stage.

S8 is one half of **Loop B** (layout ↔ DRC/LVS) with S7 (layout generation).
An agent enters S8 whenever a layout stream exists — a converged S7 pass or
not, since S8's own findings are what tells S7 whether it converged.

## Contract (design doc §2, S8)

| | |
| --- | --- |
| Input artifact | S7's layout stream (GDSII/OASIS), plus S6's netlist for the LVS half. |
| Output artifact | DRC: `klt drc`'s shipped JSON report (`docs/cli/drc.md`). LVS: **no shipped contract** — proposed shape tracked with #54. |
| Entry criteria | A layout stream exists (any pass of S7, converged or not). |
| Exit criteria | Loop B's convergence criterion (below): zero DRC violations **and** clean LVS. |
| `klt` verbs | `klt drc` (shipped). LVS: **none — blocked on #54** (open). |
| Failure modes | DRC-clean but LVS-dirty (or the reverse, once LVS exists) — Loop B's tradeoff case; a curated DRC deck subset (`docs/cli/drc.md` → "Coverage") passing while the full foundry deck would not — a known fidelity gap, not a pipeline bug. |

## DRC half — fully specified, shipped

Run:

```
klt drc <layout> --deck sky130|gf180mcu --format json
```

See `docs/cli/drc.md` for the full contract. Key fields an agent iterates
against:

- `status` — `"clean"` or `"violations"`. Exit code `0` on clean, `3` on
  violations found, `1` on a run failure (bad file/deck), `2` on a usage
  error.
- `violation_count` / `rule_counts` — aggregate signal for tracking
  convergence across passes (§ "Loop B convergence" below).
- `violations[]` — one entry per violating geometry: `rule` (stable id,
  e.g. `"poly.width.1"`), `description`, `check` kind, `layer`, `cell`,
  `bbox`, `polygon`. Edit geometry to resolve each, then re-run.

**Coverage caveat** (from `docs/cli/drc.md`): both the `sky130` and
`gf180mcu` decks are curated starter subsets (10 rules each), not the full
foundry rule manual. A "DRC-clean" verdict from `klt drc` is clean *against
the curated subset*, not a full-deck signoff — do not conflate the two when
reporting S8's exit criteria met, especially at S11 (signoff).

## LVS half — stub, blocked on #54

No `klt` verb performs LVS (layout-vs-schematic netlist comparison). Issue
**#54** (open) tracks this gap, bundled with S9's extraction gap — per its
curator note, LVS and extraction are likely one friction issue and one
engine (`pya.LayoutToNetlist` / `pya.NetlistComparer`).

Until #54 ships, an agent cannot close Loop B end to end: DRC can be
iterated to zero violations, but "clean LVS" (this stage's other half of the
exit criterion) cannot be verified by tooling. Do not claim S8's exit
criteria are met on a DRC-clean-only result — report it as
"DRC-clean, LVS unverified (blocked on #54)" rather than as stage-complete.

## Loop B convergence / stuck criteria (design doc §1)

**Converged (exit criteria met):** `klt drc` reports zero violations
(`status: "clean"`, `violation_count: 0`) **and** LVS reports a clean
device/net compare (once #54 ships) — the literal "DRC/LVS clean" clause of
the vision sentence. Neither check alone is sufficient.

**Stuck, not iterating** — any of:

- Violation count is **not monotonically decreasing** across N consecutive
  passes (oscillating or worsening).
- The same rule fires again after being "fixed" — the fix moved the
  violation rather than resolving it.
- A DRC fix breaks LVS correspondence, or vice versa (once LVS exists) — a
  genuine DRC/LVS **tradeoff**, not a bug, and the layout-stage analogue of
  Loop A's sizing/measurement tradeoff case.

Any of these is the named escalation trigger (see below) — do not keep
iterating past N passes on the same stuck signature without escalating.

## Model-class assignment (design doc §3)

**small-fast.** Rule-by-rule violation fixing against a structured, itemized
report is close-to-mechanical for a converging loop.

**Escalation rule:** escalate to mid-tier, then frontier-reasoning, after N
consecutive Loop B passes show non-monotonic violation counts or a DRC/LVS
tradeoff (the stuck criteria above).

## Failure modes (recap)

- DRC-clean but LVS-dirty (or the reverse, once LVS exists) — Loop B's
  tradeoff case.
- A curated DRC deck subset passing while the full foundry deck would not.
- Reporting S8 as converged on DRC alone while #54 is unresolved — always
  flag LVS as unverified rather than silently dropping it.
