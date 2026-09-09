# RTL review guide: spec compliance

> **Ported content, Apache-2.0.** Ported from
> [boldaxolotl/booley](https://github.com/boldaxolotl/booley)'s
> `src/booley/data/refs/code_review/rtl/spec.md`
> ([Apache License 2.0](https://github.com/boldaxolotl/booley/blob/main/LICENSE)),
> commit [`1fdc706e`](https://github.com/boldaxolotl/booley/commit/1fdc706e2c54c3b9f80ee4a9472e2889a83d722f),
> fetched 2026-09-09 — see [`NOTICE`](NOTICE) for the full attribution
> record. Reworded from booley's own Flow/Target/Ticket/Specialist
> vocabulary and its machine-parsed findings-JSON schema into this repo's
> `klt` verbs, request/response fields, and plain PR-review-comment
> convention; the severity/confidence contract and the checklist content
> carry over unchanged. See [`README.md`](README.md) for when this guide
> applies and which `klt` verb (if any) supplies the tool evidence it asks
> a reviewer to check.

Your ONLY job here is to verify that the RTL implements exactly what the
specification says — no more, no less. Do NOT evaluate code quality,
style, synthesis hazards, protocol correctness, or `ifdef` usage — the
sibling guides in [the index](README.md) handle those.

You are NOT a hardware design reviewer for this pass. Do NOT judge whether
the spec is good engineering. If the spec says outputs stay inactive when
data doesn't change, the RTL must not generate activity — even if you
think the hardware *should* behave differently.

## Scope boundaries

- **Functional correctness as hardware**: not your responsibility here —
  [`rtl-bugs.md`](rtl-bugs.md) handles this.
- **Protocol / CDC**: not your responsibility —
  [`rtl-protocol-cdc.md`](rtl-protocol-cdc.md) handles this.
- **Style, naming, optimization**: not your responsibility — see the
  sibling guides.
- **Synthesis metrics, area reduction, cell/wire counts**: not your
  responsibility. `klt synthesize`'s own `structural`/`warnings` fields and
  its optional `baseline` QoR-delta block (issue #1605) check area/cell/
  wire reduction targets mechanically — see the index's evidence table. Do
  not run `klt synthesize` yourself as part of this review, and do not
  report findings duplicating what its structural verdict already reports.
- **Your sole question**: does the RTL match the spec's **behavioral**
  requirements?

## Inputs

The specification is whatever the PR or its linked issue names as the
source of truth: spec text quoted directly in the issue/PR description, a
linked `docs/design/*.md` design doc, or the canary's own README/spec
section. Compare it against the RTL files the PR changes.

If the PR description or issue records decisions for points the spec does
not settle (a "Documented Assumptions" section or equivalent), read that
before filing anything under section B below: a behavior explained there
is a documented judgement call, not an invention.

## Procedure

1. Read the specification text carefully. Identify every **behavioral**
   requirement: edge cases, signal descriptions, FSM states, timing
   requirements, and example operations. Ignore any synthesis/area/cell/
   wire targets — those are evaluated mechanically, not by review.
2. Read every RTL file the PR changes.
3. For each behavioral spec requirement, verify the RTL implements it.
4. Check for RTL behaviors that have no basis in the spec, then grade each
   one with the silence test below — most are decisions, not defects.
5. Report findings as normal PR review comments — one per finding, each
   citing `file:line` and quoting the spec text at stake (see "Quoting
   requirement" below).

## Severity model

- **CRITICAL** — RTL behavior **contradicts** the spec. The spec
  explicitly says X; the RTL does not-X. Always HIGH confidence.
- **MAJOR** — RTL adds behavior that **changes what the spec does
  define**. The addition alters a spec'd interface, or changes the
  observable result on inputs and conditions the spec covers. The spec's
  silence elsewhere does not license breaking what it states.
- **MINOR** — the RTL resolves a point the spec leaves open, and the
  choice is reasonable but not the only valid reading. Also covers
  genuinely ambiguous spec wording.

## Spec silence is not a defect

A specification cannot enumerate every input, parameter value, and corner.
The RTL author is expected to pick the most reasonable interpretation for
whatever the spec does not settle and to record the call — the reviewer's
job is not to punish that.

So when the RTL handles something the spec never mentions — an
out-of-range index, a parameter bound, an undefined opcode, a reset value
for a signal the spec never discusses — ask **which** of these it is:

- The choice **breaks something the spec does state** (wrong width on a
  spec'd port, changes a spec'd output on a spec'd input, adds a handshake
  condition that stalls a spec'd transaction) → **CRITICAL or MAJOR**.
  Quote the spec text it breaks.
- The choice is listed under "Documented Assumptions" → **not a finding**.
  Report it only if the recorded reasoning itself contradicts spec text,
  and then quote that text.
- The choice is undocumented but reasonable, and touches only what the
  spec leaves open → **MINOR**, phrased as "spec is silent on X; RTL chose
  Y" so the author can record or revise it.
- You cannot point to spec text the behavior breaks, and the behavior is a
  plainly sensible engineering default (a reset value, a guard on an
  illegal input, a saturating bound) → **omit it**. "The spec does not
  mention this" is not, by itself, a finding.

Never demand that the RTL *remove* handling for a case the spec is silent
about. Deleting a defensive default is how a design that passed review
starts failing on inputs the spec forgot.

## Quoting requirement

Every finding MUST quote the **exact spec text** it references, e.g.
``spec says "o_valid pulses one cycle" but o_valid stays high``. For
invention-beyond-spec findings, quote the most relevant surrounding spec
text and state why the behavior is unsupported.

**Quality over quantity:** only report findings where you can quote the
specific spec text at stake. Do not speculate about unstated requirements
— an unstated requirement is not a requirement.

---

## Checklist

### A. Stated behaviors (CRITICAL if violated)

- **Reset behavior**: Does the RTL reset state match the spec's reset
  description? Check every output and internal register mentioned in the
  spec.
- **Edge cases**: Walk each edge case listed in the spec. Does the RTL
  handle it as described?
- **Example operations**: Trace each example through the RTL. Does the
  output match?
- **FSM states**: Does the RTL FSM match the spec's state descriptions?
  Correct transitions, correct actions per state?
- **Signal semantics**: Does each output signal behave as the spec
  describes? Timing, polarity, pulse width, idle values?
- **Pipeline latency**: If the spec says N-cycle latency, verify `i_valid`
  sampled at posedge T → `o_valid` high at posedge T+N. Count every `<=`
  on the datapath: pipeline stages AND output registers. N stages +
  registered output = N+1 observable cycles, not N. A directed
  `klt functional-verification` test asserting this latency numerically
  (see the index's evidence table) is stronger evidence than a review
  read of the RTL alone — check whether one exists.
- **Interface contract**: If the spec or issue provides port names,
  parameter names, directions, or widths, verify the RTL uses them
  exactly. Renamed ports or parameters break external testbenches that
  rely on the spec-defined interface.

### B. Added behaviors

Run each of these through the "Spec silence is not a defect" test above
before assigning severity. MAJOR requires naming the spec text the
addition breaks; if you cannot name it, the ceiling is MINOR, and an
undocumented-but-sensible default is usually best omitted.

- **Extra states or modes**: Does the RTL have states, flags, or modes not
  mentioned in the spec? MAJOR only if they change a spec'd transition or
  output; otherwise MINOR.
- **Unsolicited activity**: Does the RTL generate output activity in a
  situation where the spec **says** it should be idle? That is a stated
  requirement being broken — CRITICAL. Activity in a situation the spec
  simply never describes is not.
- **Additional internal signals**: Do added internal signals (validity
  flags, counters) alter a spec'd behavior? Internal signals that leave
  the spec'd behavior intact are an implementation choice, not a finding.
- **Modified interfaces**: Does the RTL change signal directions, widths,
  or names from what the spec defines? Always report — an interface the
  spec pins down is never open. Adding a parameter the spec omits is MINOR
  unless it changes a spec'd port's width or default.
