---
name: "Design Pipeline: Signoff Report (S11)"
description: "Generate a design-evidence qualification report for a block repo — grade it against the T1 sim-validated checklist (docs/design-evidence-tiers.md), with staleness and coverage-honesty checks. Aggregation tool tracked by #309; until it ships this skill specifies the hand-assembled report."
domain: design-pipeline
type: skill
user-invocable: true
---

# S11 — Signoff / qualification report

Source-of-truth chain: the evidence ladder and per-tier checklist live in
[`docs/design-evidence-tiers.md`](../../../docs/design-evidence-tiers.md);
the stage contract lives in
[`docs/design/design-pipeline.md`](../../../docs/design/design-pipeline.md)
(§2, S11). This skill operationalizes both into a report an agent can
produce today. Re-read those docs if anything here seems stale.

## Status

The mechanical aggregation tool does not exist yet — **#309** tracks it
(with concrete friction evidence from the public canary repos). Until it
ships, the report is hand-assembled per this skill; treat the output as the
provisional shape of the future `klt.pipeline.signoff/1` artifact, not as
that artifact.

## Producing a qualification report

Target: one markdown report grading a block repo against the **T1
checklist** in `docs/design-evidence-tiers.md`. Work through the ten items
in order; for each, record **PRESENT / PARTIAL / ABSENT**, the artifact
paths, and the pass condition's actual state.

### Gathering rules

1. **Find the newest artifact of each family, then check freshness.**
   Typical layouts (from the canary repos): DRC under
   `layout/drc/reports/`, LVS under `layout/lvs/reports/`, corner records
   under `sim/<experiment>/records/`, MC likewise, suite roll-ups under
   `sim/suite/summaries/`. Newest ≠ fresh: compare the report's provenance
   (input hashes where present — #335 — else record IDs vs the commits
   that last touched `design/netlist/` and `layout/`) and flag any report
   that predates its inputs as **STALE**, which fails the item.
2. **Read verdicts from JSON, not prose.** `status`/`violation_count` from
   `klt drc` JSON, `status`/mismatch severities from LVS JSON, per-row
   verdicts from suite records. README status lines drift optimistic —
   both canaries' did.
3. **Carry coverage caveats into the verdict.** Deck coverage metadata
   (rule-free layers, skipped rules), warning-level LVS mismatches,
   MC legs not combined with process corners, known false negatives
   documented in repo READMEs — each becomes a named caveat on that item,
   and an uncombined or undisclosed caveat downgrades PRESENT → PARTIAL.
4. **Spec ratification gates item 5.** If the spec table is draft, every
   sim verdict is provisional; say so at the top of the report.
5. **Completeness before quality.** A spec row with no testbench anywhere
   is a per-row ABSENT (verification rule: no claim without a testbench).

### Report shape

```markdown
# Qualification report — <block> on <pdk>
Generated <date> at repo revision <sha>. Spec status: ratified|draft.

## Verdict: T1 NOT MET — N of 10 items passing
(or: T1 MET — all items passing; caveats listed below)

| # | Item | Status | Evidence | Caveats |
|---|------|--------|----------|---------|
| 1 | Schematic | PRESENT | design/... | — |
| 3 | DRC clean | PARTIAL | layout/drc/reports/<latest> | 12 rule-free layers; #345 |
...

## Gaps, ordered by blocking depth
1. <item>: <what's missing> — <repo-side work | tool-side issue #NNN>
...

## Tool-side friction observed
<anything the toolkit should have provided — file as klayout-tools issues>
```

Split every gap into **repo-side** (design work, missing runs, stale
records) vs **tool-side** (capability the toolkit lacks — file or link a
klayout-tools friction issue: e.g. #343 netgen cross-check, #344 Monte
Carlo, #345 deck holes, #347 harness adoption, #309 aggregation).

Write the report into the block repo (e.g. `docs/qualification/<date>.md`)
only if the repo's conventions allow generated docs; otherwise deliver it
in-conversation. The report contains only public-safe content by
construction — it grades against the public tier doc.

## Contract (design doc §2, S11)

| | |
| --- | --- |
| Input artifact | Converged outputs of S8 (DRC/LVS) and S10 (post-extraction sim), plus S3's block spec — or, pre-convergence, whatever exists (the report then shows the gap map). |
| Output artifact | The qualification report above. `klt.pipeline.signoff/1` (proposed, #309) will mechanize the aggregation; entry criteria and shape mirror this skill. |
| Entry criteria | None hard — the report is most useful *before* convergence, as a gap map. |
| Exit criteria | Every S3 spec field has a recorded verdict or a named gap; no field silently unaddressed. |
| `klt` verbs | `drc`, `lvs`, `extract`, `sim`, `layout-metrics`, `report` outputs as inputs; none aggregate yet (#309). |
| Failure modes | Stale artifact pairing (staleness rule catches it); a spec field with no upstream check (surfaces as per-row ABSENT → backtrack per design doc §1). |

## Model-class assignment (design doc §3)

**small-fast** for the aggregation walk; **escalate** to a stronger tier
when a spec field has no corresponding upstream check anywhere — deciding
where that check belongs needs judgment, not templating.
