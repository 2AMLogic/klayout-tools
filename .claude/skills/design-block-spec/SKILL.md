---
name: "Design: Block Spec Authoring"
description: "S3 of the staged agent design pipeline — expand one block's budget allocation into a complete per-block spec ready for topology selection"
domain: design-pipeline
type: skill
user-invocable: false
---

# Design: Block Specs (S3)

Stage S3 of the eleven-stage pipeline in
[`docs/design/design-pipeline.md`](../../../docs/design/design-pipeline.md)
(§2 "S3 — block specs", §3 model-class matrix). This skill is a thin loader
of that stage's contract — it does not restate pipeline strategy and it
does not tell you *how* to choose a topology or size a circuit against the
spec it produces. If the design doc and this skill ever disagree, the
design doc is authoritative; update this file to match it, not the other
way around.

**Scope reminder** (design doc § "Scope: skills are procedure, not
strategy"): this stage is navigation only. It transcribes and completes an
already-decided budget allocation into a spec S4 can select a topology
against — it does not choose the topology itself, and it does not
re-litigate the S2 partition.

## Input artifact

One block's budget allocation from S2 (`klt.pipeline.architecture/1`).

## Output artifact

`klt.pipeline.blockspec/1` (**proposed** — not shipped, no schema file
exists yet). A per-block spec document: electrical specs, interface/pinout,
process/environment targets, and area/power ceiling.

## Entry criteria

Block has a closed budget allocation from S2.

## Exit criteria

Spec is complete enough to select a topology against — every field S4
needs is present, or explicitly deferred to S4's own judgment. A field
silently missing (not marked deferred) is not a valid S3 exit.

## Applicable `klt` verbs

None currently. `klt kb search`/`show`/`list` (once #110 ships, design doc
§4) is S4's tool, not S3's — this stage produces the spec S4 will query
against, it does not itself query the KB.

## Failure modes

- **Spec silently narrower than the KB entries it will be matched
  against.** Mismatched units, or a missing corner/PVT range — S4's match
  against the KB is only as good as this spec's coverage.
- **Spec copied from S2 without resolving system-level assumptions into
  block-local terms.** E.g. an implicit supply voltage inherited from the
  system spec but never made explicit at the block level — a transcription
  gap, not a partition error.

## Model class

**Mid-tier** (design doc §3). Mostly disciplined transcription of an
already-decided budget allocation into a complete per-block spec, with some
judgment on filling gaps S2 left open.

**Escalation rule:** escalate to frontier-reasoning when a block's
allocation is internally inconsistent or clearly infeasible on inspection —
this routes back to S2 per the design doc §1 non-loop backtracking (spec
infeasibility discovered downstream of where it originated).

## Next stage

Exit criteria met → hand the `klt.pipeline.blockspec/1` artifact to S4
(topology selection, KB-assisted). No skill for S4 ships as part of this
issue — see Epic #105's other Phase 2 issues.
