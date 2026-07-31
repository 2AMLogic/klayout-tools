---
name: "Design: Proposal Intake"
description: "S1 of the staged agent design pipeline — normalize a free-form circuit request into a structured proposal before architecture partitioning"
domain: design-pipeline
type: skill
user-invocable: false
---

# Design: Proposal Intake (S1)

Stage S1 of the eleven-stage pipeline in
[`docs/design/design-pipeline.md`](../../../docs/design/design-pipeline.md)
(§2 "S1 — design proposal", §3 model-class matrix). This skill is a thin
loader of that stage's contract — it does not restate pipeline strategy and
it does not tell you *how* to design a circuit. If the design doc and this
skill ever disagree, the design doc is authoritative; update this file to
match it, not the other way around.

**Scope reminder** (design doc § "Scope: skills are procedure, not
strategy"): this stage is navigation only. It never judges whether a
proposed circuit is a good idea, picks a topology, or resolves an
engineering tradeoff — it only decides whether the *proposal* is complete
enough to hand to S2.

## Input artifact

Free-form spec: prose requirements, a target application, or a reference to
a KB entry (`klt kb search`/`show`, once #110 ships) or published design.
No required structure — this stage exists precisely because the input
arrives unstructured.

## Output artifact

`klt.pipeline.proposal/1` (**proposed** — not shipped, no schema file
exists yet; see design doc §2's framing note and §"Out of scope for this
doc"). A normalized problem statement: target function, top-level specs as
known, constraints, and open questions.

## Entry criteria

Always available — this is the pipeline's start state.

## Exit criteria

Every top-level spec field is either a concrete number/range, or explicitly
marked "to be resolved in S2/S3." No silent gaps: a field left blank
without that marker is not a valid S1 exit.

## Applicable `klt` verbs

None — this stage is pure intake, out of tool scope by design.
`klt kb search`/`show` (once #110 ships) may inform what's achievable, but
is advisory, not required.

## Failure modes

- **Underspecification passed downstream as if resolved.** The single most
  expensive failure to catch late — an unresolved field masquerading as a
  decided one propagates through every later stage before anyone notices.
- **Scope too broad for the pipeline's target.** Full-chip digital is an
  explicit `ROADMAP.md` non-goal; a proposal reaching for it should be
  flagged here, not discovered at S7.

## Model class

**Frontier-reasoning** (design doc §3). Underspecified, open-ended
requirements; the cost of a misunderstood proposal compounds through every
later stage.

**Escalation rule:** none upward — S1 already runs at the ceiling class.
Escalate to a human after N clarification rounds fail to close the
open-questions list.

## Next stage

Exit criteria met → hand the `klt.pipeline.proposal/1` artifact to S2
(system architecture & budget partition — see the
`design-architecture-partition` skill).
