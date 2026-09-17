# Design note: what makes a subagent run slow

**Status:** measurement + guidance. Nothing here changes `klt`; it is written
for whoever dispatches subagents against this repo.

Adjacent, and deliberately not duplicated: `.loom/docs/cpu-budget.md` bounds how
much CPU an agent's background work may consume, and
`.loom/docs/model-cost-experiment.md` measures model selection. Both are about
*resource* cost. This note is about *round-trip* cost, which is a different
term and — on the runs measured here — the dominant one.

## The measurement

21 subagent runs against this repo and its canaries, one session,
2026-09-16/17. Totals: **540 agent-minutes, 1,536 tool uses, 5.76M tokens.**

| Agent | min | tool uses | sec/tool |
|---|---:|---:|---:|
| pdn_fix | 61.4 | 143 | 25.7 |
| trial1_synth | 55.1 | 97 | 34.1 |
| tinytapeout_flow | 44.1 | 126 | 21.0 |
| false_pass_audit | 36.3 | 71 | 30.7 |
| regression_tests | 32.5 | 107 | 18.2 |
| … | | | |
| module_map | 13.3 | 76 | **10.5** |
| signoff_test | 8.5 | 29 | 17.6 |
| scoping_prs | 7.2 | 46 | **9.4** |

**Median: 21.0 s per tool use.** Wall-clock tracks tool *count* far more
closely than token count or model choice. Opus runs hold three of the four
fastest slots and the single slowest — the task shape predicts duration, the
model does not.

## The 21 seconds is two different things

**Round-trip latency.** Every tool call costs an inference pass regardless of
what the call does. The floor is visible in the fastest runs: `module_map` and
`scoping_prs` sat at 9–10 s/tool doing cheap reads. That is close to
irreducible per call — so the lever is *fewer calls*, not faster ones.

**Real compute.** `pdn_fix` spent 576 s inside one `klt place-and-route`;
`trial1_synth` spent 934 s. `numeric_contract` is the clearest case: only 35
tool calls but **41.9 s/tool**, because each one ran a test suite. Those runs
are not slow — they are correctly waiting.

Separating the two matters, because they have opposite remedies. Batching helps
the first and does nothing for the second.

## Guidance

**1. Batch reads.** At ~21 s of fixed cost per call, five `gh api` reads in one
shell invocation cost 21 s rather than 105 s. In the runs above, roughly half of
all tool calls were reads that could have been grouped. This is the largest
available saving and it costs nothing.

**2. Hand down findings, not just environments.** Reusing a provisioned
environment already shows up in the numbers — `lvs_power_check` reused
`prep/asic` and finished in 18 min where `sky130_ASIC` spent 24 min building
it. Several runs then independently re-verified the same facts (the ORFS image
digest, the Python ≤3.13 constraint, the DRC deck hash). A short
established-facts preamble removes those calls from every agent that follows.

**3. Do not hold attention on a long wait.** A 10-minute P&R should be started,
left, and collected. `trial1_synth` built an ECP5 bitstream while a Docker pull
ran, and it is the reason that run covered as much ground as it did.

**4. Narrow the brief.** The longest runs had the broadest briefs: `pdn_fix`
carried six numbered tasks including an optional cross-check; `scoping_prs`
asked one question and finished in 7 minutes. Scope is the dispatcher's dial.

**5. One worktree per agent.** Two agents sharing a checkout is a correctness
problem, not a speed one: a `git checkout -b` in one changed the other's
working tree mid-run. Nothing was lost because the affected files were
untracked, but branch switching changes another agent's inputs silently.
`git worktree add` per agent removes the class.

## Scope of this measurement

These runs were dispatched from outside the Loom fleet, by a single
coordinator, against a warm local environment. They are **not** a measurement
of the fleet's own Builder/Curator/Champion agents, whose batching behaviour
was not sampled. Treat the 21 s/tool figure as an observation about this
dispatch pattern, not a property of agents in general — and re-measure before
citing it for fleet work.
