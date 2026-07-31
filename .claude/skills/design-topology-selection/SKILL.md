---
name: "Design: Topology Selection"
description: "S4 of the staged agent design pipeline (Epic #105) — select and record a topology reference against a block spec, KB-assisted where klt kb can help"
domain: design-pipeline
type: skill
user-invocable: false
---

# Design pipeline — S4: topology selection (KB-assisted)

Source: [`docs/design/design-pipeline.md`](../../../docs/design/design-pipeline.md)
§2 "S4 — topology selection (KB-assisted)" and §3 "Model-class matrix." This
skill is a thin loader of that contract — read the design doc's §1 ("Scope:
skills are procedure, not strategy") before using this skill: it tells you
*when* you're done choosing a topology, not *how* to choose well between two
plausible candidates. That judgment is the skill's job at runtime, not
something this file can substitute for.

Stage position: `S3 block specs -> S4 topology selection -> S5 sizing`
(design doc §1, stage graph).

## Model-class assignment

**Mid-tier.** Per §3: "a structured KB query plus fit-to-spec matching —
bounded search over documented candidates, not open-ended design."

**Escalation rule:** escalate to frontier-reasoning when `klt kb search`/`list`
returns no matching entry, or when multiple entries tie and the choice needs
first-principles judgment the KB doesn't encode (§3). Do not silently pick the
first tied candidate — record the tie and the reasoning that broke it, or
escalate.

## Status of the `klt kb` dependency (read this before starting)

The design doc's §2/§4 describe `klt kb` as "specified but not yet shipped"
(tracked as #110, Epic #102 Phase 2) at the time Phase 1 was written, and
instruct this stage to run on model knowledge alone until it lands. **That
has changed since**: `klt kb` shipped in
[#119](https://github.com/2AMLogic/klayout-tools/pull/119), closing #110,
concurrently with this skill's own authoring. The design doc's §2/§4 rows for
S4 are accordingly stale on this one point as of this writing — this skill
documents the **current, shipped** state rather than repeat the now-outdated
"not yet shipped" framing, per CLAUDE.md's contract-first rule (the shipped
CLI and its doc are the source of truth, not a design doc that predates it).

The corpus itself (`kb/entries/`) is still shallow and actively growing
(design doc §4: #106 sourcing playbook, #107/#108/#109 entry batches) — a
`klt kb search` miss is much more likely to mean "not yet catalogued" than
"no such topology exists." Treat an empty result as **inconclusive**, not as
evidence the KB has nothing to offer; the failure mode below exists precisely
because that distinction gets lost.

## Input / output artifact

| | |
| --- | --- |
| **Input** | S3's block spec (`klt.pipeline.blockspec/1`, proposed — not shipped; a per-block spec document: electrical specs, interface/pinout, process/environment targets, area/power ceiling). |
| **Output** | `klt.pipeline.topology/1` (**proposed, not shipped** — no schema file or `klt` verb reserves this name; this skill produces the shape below as a working artifact only): selected topology reference (a KB `id` when matched via `klt kb show`, or a free-text description when no KB entry matches), a rationale, and the spec fields the choice was evaluated against. |

Suggested working shape (not a shipped contract — do not present this as a
`klt` JSON output):

```json
{
  "schema": "klt.pipeline.topology/1",
  "block": "<name from S3 block spec>",
  "topology": {
    "kb_id": "sky130-bandgap-reference",
    "description": null
  },
  "rationale": "why this topology fits the S3 spec fields below",
  "matched_spec_fields": ["..."],
  "known_limitations": ["..."]
}
```

## Entry / exit criteria

- **Entry:** the block spec is complete per S3's own exit criteria (design
  doc §2, S3 row) — every field S4 needs is present or explicitly deferred to
  this stage's judgment. Do not start topology selection against a spec that
  is silently incomplete; that failure surfaces here as a topology chosen
  against the wrong target.
- **Exit:** a topology is chosen **and** its known limitations against the
  spec (if any) are recorded, not silently absorbed. A topology with zero
  recorded limitations should mean "checked and none found," not "not
  checked."

## Applicable `klt` verbs

`klt kb list` / `klt kb show <id>` / `klt kb search <query>` — shipped, full
contract in [`docs/cli/kb.md`](../../../docs/cli/kb.md) (this skill does not
restate that contract; read it for the exact request/response shape,
matching rules, and exit codes). Typical flow:

1. `klt kb search "<keyword from the block spec's topology-relevant terms>" --format json`
   — case-insensitive substring match over `title`, `topology`, `spec_class`,
   `layout_idioms`, `notes`. Try more than one keyword; a miss on one term is
   not evidence of "no match" (see the KB-status note above).
2. `klt kb list --format json` when a search misses, to scan the whole corpus
   by eye rather than trust one query's keyword choice.
3. `klt kb show <id> --format json` on a promising candidate — read
   `pdk_portability`, `sizing_approach`, and `layout_idioms` to judge fit
   against the S3 spec, not just the title match.
4. `klt kb validate --format json` is a corpus-integrity check, not part of
   this stage's normal flow — only relevant if `show`/`search` results look
   malformed.

None of these calls choose the topology for you; they narrow the candidate
set and supply citable prior art. The fit judgment against the specific S3
spec is this skill's runtime reasoning, per the "procedure, not strategy"
scope note above.

## Failure modes

(Design doc §2, S4 row.)

- **Unflagged no-match.** No KB entry matches and the agent proceeds on an
  unvalidated topology (model knowledge only) without flagging that the
  choice is unvalidated against the KB. Record this explicitly in the output
  artifact's `known_limitations`, don't let it read as KB-confirmed.
- **Familiarity over fit.** A topology chosen because it's the KB entry (or
  the model's own training-data topology) the agent has seen most often,
  rather than the one that best matches the S3 spec's actual fields
  (`spec_class`, `pdk_portability`, target electrical specs). A tie-break
  driven by familiarity rather than spec fit is exactly the case the
  escalation rule above exists to catch.
- **Corpus-miss misread as topology-doesn't-exist.** Given the corpus's
  present shallowness (see the KB-status note), treating an empty
  `klt kb search`/`list` result as "this topology has no prior art" instead
  of "not yet catalogued" understates the confidence gap that should be
  recorded in the output.

## Next stage

Hand the selected topology + the S3 block spec to **S5 sizing**
(`.claude/skills/design-sizing/SKILL.md`).
