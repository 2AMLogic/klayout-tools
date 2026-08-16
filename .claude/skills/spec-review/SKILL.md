---
name: spec-review
description: "Expert-EE review of an IC block's draft target spec — per-line achievability vs. published best practice, evidence check against repo device characterization, block-class completeness checklist, corner-binding check, and a ratify / ratify-with-amendments / defer verdict. Use when reviewing a draft block spec table before operator ratification."
---

# spec-review — expert-EE opinion on block target specs

Render a structured, literature-grounded second opinion on a draft IC block
spec so the operator can ratify it (or amend it) with confidence. The closed
loop starts at "spec": an unratifiable or physically unrealistic spec poisons
every downstream stage (see `docs/design/design-pipeline.md`, stage S3 —
block specs). This skill is the quality gate at the loop's entry.

**The skill renders opinions. Ratification always stays with the operator.**
Never mark a spec ratified, never edit the spec table in place as part of a
review, and never present the verdict as a decision — it is a recommendation
with rationale.

## Inputs

Gather all three before writing anything:

1. **The draft spec table** — the block's target spec lines (parameter,
   target value/range, units, conditions). Usually a markdown table in the
   block's repo or issue.
2. **Decision records** — any DR-/ADR-style records accompanying the spec
   (topology choices, corner policies, trim strategy). These explain *why* a
   line reads the way it does; review against the recorded intent, not just
   the number.
3. **Device-characterization evidence** — measured/simulated device data in
   the repo (Pelgrom mismatch coefficients, device corner spreads, jitter
   measurements, leakage data). This is what separates an evidence-grounded
   review from generic literature quotation.

If an input is genuinely absent, say so explicitly in the review ("no devchar
evidence available for X; achievability assessed from literature only") —
never silently pretend it was consulted.

## Grounding sources (in priority order)

1. **Repo evidence** — device characterization data and decision records in
   the repo under review. Strongest ground: it is *this* PDK, *this* flow.
2. **Bundled references** — the per-block-class files under
   [`references/`](references/) in this skill directory. Curated checklists,
   typical published ranges for a 130–180 nm-class open PDK, and known
   spec-writing pitfalls, each with citations to open literature. These are
   the offline baseline; every file is dated so staleness is visible.
3. **`kb/entries/*.json`** — this repo's knowledge base of published circuit
   designs (topologies, sizing strategies, sourced citations). Enumerate
   with a glob over `kb/entries/` and match on `spec_class`; see
   `kb/README.md`.
4. **`klt kb` (future hook)** — issue #110 adds
   `klt kb list|show|search|validate` through the shared JSON envelope. It
   had not landed when this skill was written. **At review time, probe for
   it** (`klt kb --help`); if present, prefer it over globbing `kb/entries/`
   directly and treat KB entries' reported performance as grounding data.
   Until then, the bundled reference files carry curated literature ranges
   directly.
5. **WebSearch (optional refresh)** — when the harness allows web access,
   you MAY refresh achievability anchors against current literature
   (open-access papers, public surveys such as Murmann's ADC survey or
   Makinwa's temperature-sensor survey). Cite anything you use.

**Graceful degradation is mandatory.** When web access is unavailable, the
bundled references are sufficient — proceed with them and note in the review
header that literature anchors are from the bundled references as of their
stated date. Never block a review on network access.

**Sourcing rules** (same bar as `kb/SOURCING.md` and `CLAUDE.md`): open
literature only — open-access papers, peer-reviewed work cited for restated
facts (never reproduced figures or text), public surveys, standards, and
open-silicon projects. No NDA'd or proprietary-PDK material, and cite by
reference, not reproduction.

## Reference files

| Block class | File |
|---|---|
| Bandgap / voltage reference | [`references/bandgap-voltage-reference.md`](references/bandgap-voltage-reference.md) |
| LDO regulator | [`references/ldo.md`](references/ldo.md) |
| Temperature sensor + POR | [`references/temp-sensor-por.md`](references/temp-sensor-por.md) |
| PLL / clock generation | [`references/pll.md`](references/pll.md) |
| SAR ADC | [`references/sar-adc.md`](references/sar-adc.md) |
| TRNG | [`references/trng.md`](references/trng.md) |
| OTA / operational amplifier | [`references/ota-amplifier.md`](references/ota-amplifier.md) |
| Comparator | [`references/comparator.md`](references/comparator.md) |

For a block class with no reference file, apply the same review structure,
ground achievability in `kb/entries/` and (if available) WebSearch, state
plainly that no curated checklist exists for the class, and recommend filing
an issue to add one.

## Procedure

1. **Identify the block class and node.** Map the block to a reference file
   above. Note the PDK/node (sky130, gf180mcu, …) — achievability anchors in
   the references are stated for a 130–180 nm class and must be re-derived,
   not assumed, for other nodes.
2. **Read the bundled reference file** for the class, plus any matching
   `kb/entries/` records (via `klt kb` if it exists).
3. **Per spec line, assess:**
   - **Achievability** — compare the target against published best practice
     for the block class and node. Cite the anchor (paper, survey,
     standard). Classify: *comfortable* (well inside published typical),
     *aggressive* (near published best practice — achievable but eats
     margin), or *not credible* (beyond published best practice for the
     node, or contradicts device physics — say why, with the budget math
     when possible).
   - **Evidence check** — does the repo's own device characterization
     support or contradict the line? (E.g. measured Pelgrom coefficients
     vs. a claimed untrimmed accuracy; measured RO jitter vs. a claimed
     entropy rate.) State *supports* / *contradicts* / *no evidence*.
4. **Completeness checklist** — walk the canonical spec-line checklist in
   the reference file and list every canonical line the draft is missing.
   Missing lines are findings, not footnotes: an absent current-limit row on
   an LDO is a spec bug.
5. **Corner-binding check** — every line must state which PVT corner it
   binds at (which corner is worst-case for *that* line). Flag every line
   that doesn't. The model is the gf180-trng DR-0003 pattern: throughput
   binds at the slowest corner, entropy at the fastest — one block, opposite
   binding corners on adjacent lines.
6. **Verdict** — exactly one of:
   - **ratify** — every line achievable and evidenced, checklist complete,
     corners bound.
   - **ratify-with-amendments** — sound overall, but enumerate each required
     amendment (numbered A1, A2, …: changed values, added lines, corner
     bindings). The operator can ratify by accepting the amendments.
   - **defer** — a load-bearing line is not credible or a prerequisite
     (devchar evidence, a decision record) is missing; state exactly what
     must exist before re-review.

## Output format

Emit the review as markdown in this shape (see
`examples/spec-review/review.md` for a complete worked example):

```markdown
# Spec review: <block name> (<block class>, <PDK/node>)

Reviewed: <date> · Skill references dated: <date(s) from reference files used>
Grounding: <bundled references | + kb entries | + klt kb | + web refresh> ·
Devchar evidence: <consulted files, or "none available">

## Per-line review

### <spec line name> — <target>
- **Achievability**: <comfortable | aggressive | not credible> — <rationale
  with citation>
- **Evidence**: <supports | contradicts | no evidence> — <what was checked>
- **Corner binding**: <stated: OK | missing — should bind at <corner>>

<...one section per spec line...>

## Completeness

Missing canonical lines for <block class>:
- <line> — <why it matters>

## Verdict

**<ratify | ratify-with-amendments | defer>**

<If amendments: numbered list A1, A2, ...>
<Rationale paragraph. Close with: "Ratification is the operator's call; this
review is an opinion.">
```

## Rules

- **Opinion, not authority.** Never ratify, never edit the spec under
  review, never soften a "not credible" finding to be agreeable.
- **Numbers over adjectives.** Every achievability call cites an anchor
  (paper, survey, standard, or repo measurement). "Seems tight" is not a
  finding; "±0.5 % untrimmed 3σ is beyond the ±1–3 % untrimmed range typical
  of CMOS bandgaps (Ge et al., JSSC 2011 needed a trim to reach ±0.15 %)"
  is.
- **Flag your own staleness.** Reference files are dated; if a file is more
  than ~2 years old and web access is available, spend a search refreshing
  its anchors before leaning on it for a "not credible" call.
- **Never use private-repo spec content as an example or anchor.** Canary
  block specs live in private repos; reviews of them are fine, but nothing
  from them may be copied into this public repo's examples or references.
