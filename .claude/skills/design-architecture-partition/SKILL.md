---
name: "Design: Architecture & Budget Partition"
description: "S2 of the staged agent design pipeline — decompose a normalized proposal into a block list with a closed power/area/noise budget partition"
domain: design-pipeline
type: skill
user-invocable: false
---

# Design: System Architecture & Budget Partition (S2)

Stage S2 of the eleven-stage pipeline in
[`docs/design/design-pipeline.md`](../../../docs/design/design-pipeline.md)
(§2 "S2 — system architecture & budget partition", §3 model-class matrix).
This skill is a thin loader of that stage's contract — it does not restate
pipeline strategy and it does not tell you *how* to trade area against
power in a partition. If the design doc and this skill ever disagree, the
design doc is authoritative; update this file to match it, not the other
way around.

**Scope reminder** (design doc § "Scope: skills are procedure, not
strategy"): this stage is navigation only. Choosing *how* to trade
power/area/noise/yield across blocks is strategy, out of scope here — that
judgment is the job of the LLM reasoning module named in `ROADMAP.md`'s
Phase 5. This skill only defines what a complete, closed partition looks
like and when the stage is done.

## Input artifact

S1's normalized proposal (`klt.pipeline.proposal/1`) — no unresolved
top-level spec fields.

## Output artifact

`klt.pipeline.architecture/1` (**proposed** — not shipped, no schema file
exists yet). A block list with interfaces, plus a budget partition (power,
area, noise, and other system specs allocated per block) that sums to the
system-level target.

## Entry criteria

S1 output has no unresolved top-level spec fields.

## Exit criteria

Every block has a complete budget allocation, and the allocations are
internally consistent — they close against the *system-level* spec, not
just against each other. A partition that balances block-to-block but
overshoots the system budget has not met this exit criterion.

## Applicable `klt` verbs

None currently — no optimization/partition tool exists (design doc §4 gap
map: no friction issue filed yet for this stage; a candidate future
optimization/partition capability per `docs/ARCHITECTURE.md`'s
"optimization" scope line).

## Failure modes

- **Partition that doesn't close.** Allocations sum past the system-level
  budget — a bookkeeping failure this stage's exit criterion exists to
  catch before it reaches S3.
- **A block given an allocation later stages prove infeasible.** Not
  necessarily an S2 bug — it is the "spec infeasibility" non-loop backtrack
  named in the design doc §1: S3 or S5 discovering a block's allocation is
  unmeetable routes back here (or to S1, if the top-level spec itself needs
  renegotiating) with the discovered constraint as new input.

## Model class

**Frontier-reasoning** (design doc §3). Multi-objective tradeoffs
(power/area/noise/yield) across blocks with no single correct partition —
the epic's own "frontier reasoning models are wasted on mechanical work,
small models fail at architecture partitioning" case.

**Escalation rule:** none upward — S2 already runs at the ceiling class.
Escalate to a human when no partition closes the system budget after N
attempts (a genuine infeasibility, not a search failure).

## Next stage

Exit criteria met → hand the `klt.pipeline.architecture/1` artifact to S3,
one block at a time (block specs — see the `design-block-spec` skill).
