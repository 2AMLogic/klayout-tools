---
name: "Design Pipeline: Signoff Report (S11)"
description: "Generate a design-evidence qualification report for a block repo — grade it against the T1 sim-validated checklist (docs/design-evidence-tiers.md), with staleness and coverage-honesty checks. `klt signoff` (#309) mechanically aggregates the drc/lvs/extract/sim pieces of this walk; this skill still drives the parts it can't (no S3 spec schema, design-hygiene items) and the full report."
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

`klt signoff` (#309, `docs/cli/signoff.md`) now mechanically aggregates
`klt drc`/`klt lvs`/`klt extract`/`klt sim` JSON envelopes into one
pass/fail verdict, and refuses to combine inputs whose `provenance` blocks
disagree (mismatched PDK, deck, or input-layout identity) rather than
silently producing a wrong verdict — the two checklist items this skill
graded entirely by hand before (items 3/4 "DRC/LVS clean" and the
provenance-consistency half of "fresh") now have a tool behind them. Run it
against a block's latest reports and fold its `checks[]`/
`provenance_consistency` output into the table below instead of
re-deriving pass/fail by eye.

`klt signoff` does **not** yet diff against a block's declared spec (S3 in
`docs/design/design-pipeline.md` has no machine-readable schema — see that
doc's §4 gap map). This skill still hand-assembles the full report and
everything `klt signoff` doesn't cover; treat its output as the provisional
shape of a wider `klt.pipeline.signoff/1` artifact once a spec-diff
capability exists, not as that artifact yet.

`klt signoff --manifest <file>` (#722, Phase 0 of epic #706) now also
renders the **full T1-T4 item skeleton**, mechanically parsed from
`docs/design-evidence-tiers.md`, and grades each item against a block
manifest's declared kind and per-item evidence citations -- a strict
superset of the checklist table this skill hand-assembles below, including
the T2-T4 ladder rows and every T1 item, not only the drc/lvs/extract/sim
ones. An item is `"met"` only when the manifest's evidence resolves to a
passing, fresh `klt` JSON envelope for it -- either a pre-existing envelope
file (Phase 0), or (#825, Phase 1 of epic #706) a `klt
drc`/`lvs`/`extract`/`sim` command `klt signoff` actually runs and grades
against that run's own exit status and stdout, rather than only reading a
file someone else already produced. Items with no `klt` verb JSON to cite
at all (design-source/layout hygiene, README/license/CI, testbench-shipped)
still render `"unmet"` in its output today, exactly as this skill still
grades them by hand -- see `docs/cli/signoff.md`'s "Tier-verdict report"
section for the manifest shape and JSON schema, including the
command-backed evidence entry's shape.

When qualifying more than one block at once, `klt signoff --fleet <file>`
(#827, Phase 1c of epic #706) grades every block named in a **fleet
manifest** (a list of block manifests, inline or by path) and reports each
one's current tier plus, for any block not yet T1, the single item still
blocking it -- one query across a fleet of canaries instead of re-running
this skill per block. See `docs/cli/signoff.md`'s "Fleet roll-up" section.

## Producing a qualification report

Target: one markdown report grading a block repo against the **T1
checklist** in `docs/design-evidence-tiers.md`, selected for the block's
declared **kind** (`analog` / `digital` / `mixed-signal` — see that doc's
"Block kind" subsection). Work through the kind's ten-item checklist in
order; for each, record **PRESENT / PARTIAL / ABSENT**, the artifact paths,
and the pass condition's actual state.

### Determining the block's kind

The kind selects which column of items 1, 2, 5, and 7 applies, and which
caveats items 6 and 8 carry (items 3, 4, 9, and 10 are kind-independent and
apply as written regardless of kind — see `docs/design-evidence-tiers.md`).
Resolve it in this order:

1. **Explicit invocation argument.** If the caller states a kind
   (`analog`, `digital`, or `mixed-signal`) when invoking this skill, use
   it — this is the cheapest signal to provide and the recommended way to
   declare kind going forward.
2. **Repo declaration.** Otherwise, look for a `Block kind: <analog|
   digital|mixed-signal>` line in the block repo's `README.md` (mirroring
   the "Block kind" claim language in `docs/design-evidence-tiers.md`).
3. **Default: analog.** If neither is present, treat the block as
   `analog`. Every canary repo predating this convention (e.g.
   `gf180-bandgap`, `sky130-bandgap`, `sky130-ota-5t`) has no kind
   declared; defaulting to `analog` reproduces their reports unchanged —
   no regression for existing analog block repos.

With the kind resolved:

- **`analog`** — walk items 1–10 using the *Analog* column for items 1, 2,
  5, and 7 (item 6 and item 8 apply as written, using their analog-relevant
  language). This is the original, unmodified ten-item walk — nothing about
  it changes from before this skill supported other kinds.
- **`digital`** — walk items 1–10 using the *Digital* column for items 1,
  2, 5, and 7; item 6 applies only to whichever spec rows are actually
  statistical (state explicitly if none are — most digital spec rows are
  not, per the doc); item 8 includes Fmax, area, and power across the
  corner set. Items 3, 4, 9, and 10 apply as written. No analog-only
  artifact (schematic capture, PVT corner sweeps, Monte Carlo runs) is
  required or graded for a block with no reason to produce it.
- **`mixed-signal`** — the claim states the partition boundary (which
  nets/pins/cells are analog vs. digital). Produce **two** ten-item
  gradings, one per partition, each walked exactly as its own kind above —
  grade each partition against its own checklist, never one blended list.
  Items 3, 4, 9, and 10 are kind-independent: if a single artifact (e.g.
  one chip-level DRC report) covers both partitions, cite that same
  evidence in both tables rather than omitting the row from either.

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
   both canaries' did. When DRC/LVS/extract/sim JSON reports for the same
   revision are on hand, `klt signoff <file>...` (docs/cli/signoff.md) does
   both this step and step 1's provenance-freshness comparison for you in
   one pass — read its `checks[]`/`provenance_consistency` output instead
   of re-deriving pass/fail and staleness by eye, and cite its `status`
   (`pass`/`fail`/`refused`) directly on items 3/4/7.
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

For an `analog` or `digital` block (one checklist to grade):

```markdown
# Qualification report — <block> on <pdk>
Generated <date> at repo revision <sha>. Block kind: analog|digital.
Spec status: ratified|draft.

## Verdict: T1 NOT MET — N of 10 items passing
(or: T1 MET — all items passing; caveats listed below)

| # | Item | Status | Evidence | Caveats |
|---|------|--------|----------|---------|
| 1 | Schematic | PRESENT | design/... | — |               <!-- analog -->
| 1 | RTL + gate netlist | PRESENT | rtl/... | — |          <!-- digital -->
| 3 | DRC clean | PARTIAL | layout/drc/reports/<latest> | 12 rule-free layers; #345 |
...

## Gaps, ordered by blocking depth
1. <item>: <what's missing> — <repo-side work | tool-side issue #NNN>
...

## Tool-side friction observed
<anything the toolkit should have provided — file as klayout-tools issues>
```

For a `mixed-signal` block, repeat the verdict + table **once per
partition** — each headed by its own kind and graded against that kind's
checklist — ahead of one shared gaps/friction section:

```markdown
# Qualification report — <block> on <pdk>
Generated <date> at repo revision <sha>. Block kind: mixed-signal.
Partition boundary: <analog nets/pins/cells> vs. <digital nets/pins/cells>.
Spec status: ratified|draft.

## Analog partition — Verdict: T1 NOT MET — N of 10 items passing
| # | Item | Status | Evidence | Caveats |
|---|------|--------|----------|---------|
| 1 | Schematic | PRESENT | design/analog/... | — |
...

## Digital partition — Verdict: T1 NOT MET — M of 10 items passing
| # | Item | Status | Evidence | Caveats |
|---|------|--------|----------|---------|
| 1 | RTL + gate netlist | PRESENT | rtl/... | — |
...

## Gaps, ordered by blocking depth
1. <partition>/<item>: <what's missing> — <repo-side work | tool-side issue #NNN>
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
| Output artifact | The qualification report above. `klt signoff`'s JSON (`docs/cli/signoff.md`, #309) now mechanizes the drc/lvs/extract/sim half of the aggregation (per-check pass/fail + provenance-consistency refusal); a wider `klt.pipeline.signoff/1` spec-diff artifact remains proposed until S3 has a schema. |
| Entry criteria | None hard — the report is most useful *before* convergence, as a gap map. |
| Exit criteria | Every S3 spec field has a recorded verdict or a named gap; no field silently unaddressed. |
| `klt` verbs | `drc`, `lvs`, `extract`, `sim`, `layout-metrics`, `report` outputs as inputs; `klt signoff` (#309) aggregates the drc/lvs/extract/sim subset into one pass/fail/refused verdict — no verb yet diffs against an S3 spec. |
| Failure modes | Stale artifact pairing (`klt signoff`'s provenance-consistency check now catches the drc/lvs/extract/sim case mechanically — a mismatch produces `status: "refused"`, not a silently wrong verdict); a spec field with no upstream check (surfaces as per-row ABSENT → backtrack per design doc §1). |

## Model-class assignment (design doc §3)

**small-fast** for the aggregation walk; **escalate** to a stronger tier
when a spec field has no corresponding upstream check anywhere — deciding
where that check belongs needs judgment, not templating.
