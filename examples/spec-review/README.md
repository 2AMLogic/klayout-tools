# spec-review worked example

A worked example for the [`spec-review`
skill](../../.claude/skills/spec-review/SKILL.md): a small **synthetic**
draft spec for a fictional sky130 bandgap reference, and the review the
skill produces for it.

- [`draft-spec.md`](draft-spec.md) — the input: the fictional block's draft
  spec table, its decision records, and its device-characterization
  evidence. Everything in it is invented for this example; it is not any
  real project's spec.
- [`review.md`](review.md) — the output: the skill's structured review of
  that draft, in the format defined in the skill's `SKILL.md`.

To run the skill against a real draft, load the skill and give it the three
inputs it asks for (spec table, decision records, devchar evidence); the
review should come back in the same shape as `review.md`.
