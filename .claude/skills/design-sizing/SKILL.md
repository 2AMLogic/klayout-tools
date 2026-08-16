---
name: "Design: Sizing"
description: "S5 of the staged agent design pipeline (Epic #105) — iterate a sized device parameter set against klt sim corner feedback (Loop A)"
domain: design-pipeline
type: skill
user-invocable: false
---

# Design pipeline — S5: sizing

Source: [`docs/design/design-pipeline.md`](../../../docs/design/design-pipeline.md)
§1 "Loop A — sizing <-> simulation," §2 "S5 — sizing," and §3 "Model-class
matrix." Per the design doc's §1 scope note, this skill defines navigation
(when you're converged, when you're stuck, what to call next) — not circuit
strategy (which device to widen, how to trade a pole against noise). That
judgment stays in the reasoning module named in ROADMAP.md's Phase 5.

Stage position: `S4 topology selection -> S5 sizing <-> S10 simulation
(Loop A) -> S6 schematic/netlist`. The first passes of Loop A legitimately
run schematic-level, `S5 -> S6 -> S10` directly, skipping S7-S9 (layout/DRC/
extraction), to get a fast pre-layout sizing signal before paying for a
layout iteration (design doc §1, "Members").

## Model-class assignment

**Mid-tier.** Per §3, this is the design doc's own worked example: "Iterative
numeric optimization against simulator feedback — mechanical for a
converging loop... sizing runs mid-tier, escalates to frontier after N
failed corner iterations."

**Escalation rule:** escalate to frontier-reasoning after N consecutive
Loop-A passes show non-monotonic margins or an unresolved measurement
tradeoff (§3; see the stuck-loop checklist below for the precise
conditions). Pick a concrete N (e.g. 3-5) before starting and stop at it —
an escalation rule with no numeric trigger is not a rule.

## Input / output artifact

| | |
| --- | --- |
| **Input** | S4's topology (`klt.pipeline.topology/1`, proposed) + S3's block spec. |
| **Output** | `klt.pipeline.sizing/1` (**proposed, not shipped** — no schema file or `klt` verb reserves this name): device parameter set (W/L, multiplier, bias currents, passive values) bound to the topology, plus the last `klt sim` result that produced it. |

Suggested working shape (not a shipped contract):

```json
{
  "schema": "klt.pipeline.sizing/1",
  "block": "<name>",
  "topology_ref": "<S4 output reference>",
  "devices": { "M1": { "w_um": 4.0, "l_um": 0.5, "m": 2 }, "...": "..." },
  "passives": { "R1": { "value_ohm": 5000 }, "...": "..." },
  "last_sim_result": { "netlist": "...", "status": "pass", "...": "see klt sim's own response shape" }
}
```

## Entry / exit criteria

- **Entry:** a topology has been selected (S4 exit criteria met).
- **Exit:** Loop A's convergence criterion (design doc §1) — **every
  declared measurement's `status` is `"pass"` across the full declared corner
  matrix** (`klt sim`'s aggregate `status: "pass"`), for a *stable* candidate:
  the same sizing produces a clean sweep, not a single lucky corner set. A
  schematic-level pass converging (S6 -> S10 direct path) is a legitimate
  fast-loop exit for iterating sizing, but does **not** retire the pipeline
  stage — the vision sentence's "simulation-verified" is only satisfied by a
  post-extraction (S9 -> S10) pass, per the design doc's S10 row. Don't let a
  clean pre-layout run get reported as final. See
  `.claude/skills/design-extraction/SKILL.md`'s "Post-layout re-verification
  workflow" for how to re-run this same testbench against the extracted
  netlist (`klt sim`'s `netlist_source` field).

## Applicable `klt` verbs

**`klt sim <request.json>`** — shipped, full contract in
[`docs/cli/sim.md`](../../../docs/cli/sim.md). This skill does not restate
that contract (request/response shape, corner-axis semantics, `margin` sign
convention, exit codes) — read that document for the mechanics of building a
request and interpreting a response. What this skill adds is how to *use*
the response to drive the next sizing iteration:

- Read `measurements[].status` and `measurements[].worst_case` to find which
  declared measurement(s) are failing and by how much (`margin`, signed:
  positive is headroom, negative is violation).
- Read the per-corner `corners[].measurements[]` (not just the top-level
  rollup) when a measurement's worst case isn't enough context — e.g. to
  check whether a failure is isolated to one corner or systemic across the
  matrix.
- `status: "error"` corners (nonconvergence, timeout, netlist error — see
  `docs/cli/sim.md` → "Failure classification") are **not** a sizing signal;
  fix the netlist/request before treating the run as feedback. `error` always
  outranks `fail` in the aggregate — don't read an errored run's `passed`
  count as partial progress.

No dedicated sizing/optimization verb exists — the next-candidate proposal
after reading a `klt sim` result is this skill's own reasoning, not a tool
call. That is a **recorded decision, not an unfilled gap**: design doc §2's
"S5 — sizing" section ("Recorded scope decision (#310)") weighed a generic
optimizer verb, an S5-specific helper, and remain-manual, and chose
remain-manual — the one real Loop A run on record converged in two passes
without changing a single device size, so an optimizer would have had
nothing to search. Read that decision before proposing to build one; it
also names the re-trigger (a real block hitting escalation N with **every**
pass genuinely changing device parameters and diagnosis-side causes ruled
out) and asks for that `loop_a_history` to be attached to #310. If your
loop meets it, that is the evidence — file it rather than freehanding
around a missing tool.

## Loop A convergence / stuck-loop checklist

Per design doc §1. Track margins and worst-case-offender measurements across
consecutive `klt sim` passes (N = the number you fixed before starting, per
the escalation rule above) and check these every pass:

- [ ] **Converged?** Every declared measurement's `status` is `"pass"` across
  the full corner matrix on this pass — not just the previously-worst
  measurement. Confirm the *whole* `measurements[]` array, not only the one
  you were chasing.
- [ ] **Stable, not lucky?** The passing sizing is not a knife-edge result —
  re-check `margin` values are not near-zero across the board for a corner
  matrix that barely covers the spec's real PVT range (an incompleteness in
  S3 surfacing here, per the design doc's failure-modes row).
- [ ] **Monotonic margins?** Across the last N passes, is the worst-case
  margin for each failing measurement trending toward zero/positive, or is
  it oscillating / getting worse? Non-monotonic margins over N passes =
  stuck, escalate.
- [ ] **Same offender, no remedy change?** Is the same measurement the
  worst-case offender across N passes with no change in the sizing strategy
  being tried? Repeating the same fix and expecting a different corner
  result = stuck, escalate.
- [ ] **Genuine tradeoff?** Did a sizing change fix one measurement while
  regressing another by a *larger* margin? This is a real tradeoff, not a
  bug — it needs a strategy decision (e.g. renegotiate the S3 budget, per
  the design doc's "Non-loop backtracking" note) rather than another blind
  iteration. Escalate rather than iterate further on the same axis.

Any one of the last three boxes checked = stuck, not iterating. Escalate to
frontier-reasoning per the rule above rather than continuing to spend passes.

## Failure modes

(Design doc §2, S5 row.)

- **Loop A's stuck condition** (the checklist above) — mistaken for ordinary
  slow convergence and iterated past the point of being useful.
- **Fragile local optimum.** A sizing candidate that clears every declared
  measurement in the *declared* corner matrix but is fragile to a corner the
  matrix doesn't sweep — this is an incompleteness in S3 (the block spec's
  corner coverage), surfacing here as an apparently-converged result that
  isn't actually robust. If you suspect this, flag it in the output rather
  than reporting a clean convergence.
- **Corner-dependent `gain_db` collapse mistaken for a sizing failure.** If
  `gain_db` fails only at a handful of skewed PVT corners while other
  measurements pass broadly, suspect the S6 netlist's open-loop testbench
  construction before spending another Loop A pass on sizing. A block whose
  closed-loop intent is declared upstream (S1's
  `target_specs.closed_loop_config`, corroborated by S4's
  `matched_spec_fields`) needs its open-loop gain characterized with a DC
  feedback network holding the output at the intended closed-loop operating
  point at DC while opening the loop at AC — letting the output float
  instead lets its DC bias drift to a corner-dependent balance point that
  can push a load device into triode at skewed corners, collapsing measured
  gain independent of the actual sizing. See
  `.claude/skills/design-netlist-authoring/SKILL.md`'s Failure modes section
  for the technique and the worked `Lfb`/`Cfb` example in
  `examples/design-pipeline/ota_5t.spice`. Rule this out before treating a
  skewed-corner-only `gain_db` failure as a sizing deficiency.

## Matching specs (Pelgrom's law)

If the block spec includes a matching/offset requirement (e.g. a
spec-review line like "σ(Vos) ≤ 2 mV"), the device parameter set this stage
outputs (`W`/`L`/multiplier for a matched pair or array) has a **minimum
area floor** set by Pelgrom's law, independent of the transconductance/
overdrive sizing this stage otherwise drives. See
[`docs/design/matching-and-floorplanning.md`](../../../docs/design/matching-and-floorplanning.md)
for the law itself, where to get the process's real `A_VT`/`A_β` constants
(never invent one), and the worked conversion from a stated spec through
this stage's `W`/`L` output to S7's generator parameters. Check that floor
against whatever `W`/`L` the transconductance/overdrive sizing already
produced — most of the time it is already satisfied and changes nothing;
only widen a device if the Pelgrom-derived floor exceeds what sizing alone
would have chosen.

## Next stage

Hand the sized device parameter set to **S6 schematic/netlist**
(`.claude/skills/design-netlist-authoring/SKILL.md`) to produce the netlist
`klt sim` (and later S10 passes) run against; the sizing/simulation loop
continues from there via S10.
