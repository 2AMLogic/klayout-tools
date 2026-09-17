---
name: "Subagent dispatch: keeping runs short"
description: "Reduce subagent wall-clock by cutting tool-call count, not by picking a faster model. Measured at ~21s fixed cost per tool use across 21 runs; batching reads is the largest available saving."
domain: agent-operations
type: skill
user-invocable: false
---

# Dispatching subagents against this repo

This is a **thin loader**, not the source of truth. The measurement, the
per-run table and the reasoning live in
[`docs/design/subagent-dispatch-cost.md`](../../../docs/design/subagent-dispatch-cost.md).
Re-read that if anything here seems stale.

Related but different: `.loom/docs/cpu-budget.md` bounds an agent's CPU
consumption; this is about the number of round trips it makes.

## The one number

**~21 seconds per tool use, median, across 21 runs.** Wall-clock tracks tool
*count*, not token count and not model choice. A run's duration is roughly
`tool_calls × 21s` plus whatever real compute it waits on.

## Before dispatching

- **State the established facts in the brief.** Versions, digests, paths, what
  a previous agent already proved. Every fact omitted becomes two or three
  tool calls spent rediscovering it.
- **Narrow the task.** Six numbered sub-tasks produced a 61-minute run; one
  clear question produced a 7-minute one.
- **Give it its own worktree** — `git worktree add`. Two agents in one checkout
  will change each other's working tree on a branch switch, silently.
- **Name what is long-running** so the agent starts it and works elsewhere
  rather than waiting on it.

## In the brief, ask for

- **Batched reads.** Several `gh api` or file reads in one shell invocation,
  not one call each. This is the single biggest saving.
- **Real results, not claims.** "Run it and report what happened" costs the
  same and is worth more.
- **A plain statement of what could not be verified**, rather than a
  simulation of it.

## What does not help

- Choosing a "faster" model. Opus runs held three of the four fastest slots and
  the slowest; task shape decides.
- Optimising an agent that is waiting on a 10-minute place-and-route. That time
  is real work — parallelise around it instead.
