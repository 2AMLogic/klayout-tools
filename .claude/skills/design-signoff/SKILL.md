---
name: "Design Pipeline: Signoff Report (S11)"
description: "Stub — aggregate klt drc/klt sim (and eventually LVS/extraction) outputs into one pass/fail signoff artifact against the block spec. No aggregation tool exists and no friction issue is filed yet."
domain: design-pipeline
type: skill
user-invocable: false
---

# S11 — Signoff report

This is a **thin loader**, not the source of truth. The full stage graph,
per-stage contracts, and model-class matrix live in
[`docs/design/design-pipeline.md`](../../../docs/design/design-pipeline.md)
(§1 stage graph, §2 per-stage contracts, §3 model-class matrix, §4 gap map).
Re-read that doc's S11 entries directly if anything here seems stale — this
file only restates it for an agent entering the stage.

## Status: blocked — no aggregation tool, and no friction issue filed

No `klt` command aggregates `klt drc`/`klt sim` (and, later, LVS/extraction)
JSON outputs into one signoff artifact. Unlike S8's LVS half or S9, this gap
has **no tracking issue yet** (design doc §4 gap map: "No friction issue
filed yet"). This is a full stub, and the gap itself is less concretely
scoped than #54's — there is no engine decision pending, just an unbuilt
aggregation/report tool.

## Contract (design doc §2, S11)

| | |
| --- | --- |
| Input artifact | Converged outputs of S8 (DRC/LVS clean) and S10 (post-extraction sim pass), plus S3's original block spec. |
| Output artifact | `klt.pipeline.signoff/1` (proposed, not shipped): pass/fail against every S3 spec field, with the corner/measurement/violation evidence each verdict rests on, and provenance hashes for every input artifact (mirroring `klt sim`'s `environment` block's reproducibility discipline). **Not implemented.** |
| Entry criteria | Both S8 and S10 converged on the same layout/netlist generation — not a stale mix of an old layout's DRC pass and a newer netlist's sim pass. |
| Exit criteria | Every S3 spec field has a recorded verdict; no field is silently unaddressed. |
| `klt` verbs | None currently. Available inputs once upstream stages are unblocked: `klt drc --format json` (`docs/cli/drc.md`), `klt sim` (`docs/cli/sim.md`); LVS/extraction outputs once #54 ships. |
| Failure modes | A spec field with no corresponding check anywhere upstream, discovered only here (the "signoff rejection" backtrack, design doc §1 → re-enters at whichever stage owns that spec); provenance mismatch between the layout and netlist being signed off (stale artifact pairing). |

## What an agent can do today

Until an aggregation tool exists, an agent reaching S11 must hand-assemble
the signoff comparison: read `klt drc --format json` and `klt sim`'s JSON
output directly, cross-reference each against the S3 block spec fields, and
record verdicts manually — there is no shortcut, and no shipped schema to
target. Treat any hand-assembled result as provisional, not as the
`klt.pipeline.signoff/1` artifact the design doc proposes (that name is not
authorized by anything shipped — see design doc "Out of scope for this
doc").

**If this gap causes real friction** driving a block through Epic #105's
Phase 3 worked example — e.g. repeatedly hand-assembling the same
comparison, or a spec field with no clean upstream source to check it
against — that is worth filing as a new friction issue at that point, since
none exists yet. Don't file speculatively; file when the worked example
actually hits the wall.

## Model-class assignment (design doc §3)

**small-fast.** Templated aggregation of already-converged, already-
structured JSON (`klt drc`/`klt sim` outputs) into a report; the checks
were already done upstream.

**Escalation rule:** escalate to mid-tier or frontier-reasoning when
aggregation surfaces a spec field with no corresponding upstream check (the
"signoff rejection" backtrack, design doc §1) — that gap needs judgment, not
templating.

## Failure modes (recap)

- A spec field with no corresponding check anywhere upstream, discovered
  only here.
- Provenance mismatch between the layout and netlist being signed off (a
  stale artifact pairing — e.g. DRC ran against an older layout revision
  than the one `klt sim`'s post-extraction pass used).
