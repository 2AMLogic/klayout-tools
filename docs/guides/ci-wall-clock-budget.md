# CI wall-clock budget

`ci.yml` carries a **wall-clock budget**: a final `CI wall-clock budget` job
that measures the run it belongs to and fails if a job, the total compute, or
the run's wall clock has regressed past a committed threshold. This page records
the profiling that produced those thresholds and how to re-derive them.

- Budgets: [`.github/ci-wall-clock-budget.json`](../../.github/ci-wall-clock-budget.json)
- Check: [`scripts/check_ci_wall_clock.py`](../../scripts/check_ci_wall_clock.py)
- Tests: `tests/test_ci_wall_clock.py`

## Why this exists

A multi-repo survey of CI wall clock (issue #1971) found that for most 2AM
Logic repos wall clock is dominated by **self-hosted runner queue time, not
compute**. Measured against actual job seconds:

| Repo | worst-run wall | sum(job seconds) | gap |
|---|---:|---:|---|
| `sky130-pll` | 23,350 s | 24 s | ~6.5 h waiting |
| `sky130-ldo` | 2,134 s | 12 s | ~35 min waiting |
| `klayout-tools` | 782 s | 980 s | negative — jobs parallelise |

`klayout-tools` is the exception, and the only one of the surveyed repos whose
CI is genuinely **compute**-bound: its jobs do roughly 1,000 s of real work and
already overlap down to ~400-780 s of wall clock. Getting faster here means
shrinking the work, which is an engineering problem rather than a scheduling
one — and therefore worth a regression gate. Fixing the queue starvation in the
other repos is separate work in *those* repos; nothing on this page addresses
it.

The per-job `timeout-minutes` values `ci.yml` already had are **not** this
gate. They are hard ceilings sized for pathological hangs — the `test` matrix
allows 45 minutes for a job whose median is 3 minutes — so a leg can triple in
cost and stay green. The budget check fires in the band where a real regression
actually lives.

## Measured baseline

Sample: the **25 most recent successful `ci.yml` runs**, measured 2026-09-17,
from each run's own job timings:

```bash
gh api "repos/2AMLogic/klayout-tools/actions/runs/<run-id>/jobs?per_page=100" \
  --jq '.jobs[] | select(.conclusion=="success")
        | [.name, ((.completed_at|fromdateiso8601) - (.started_at|fromdateiso8601))]
        | @tsv'
```

Per job, in seconds:

| Job | median | p90 | max | budget |
|---|---:|---:|---:|---:|
| Tests (Python 3.10) | 180 | 194 | 200 | 360 |
| Tests (Python 3.12) | 177 | 188 | 198 | 360 |
| Tests (Python 3.11) | 176 | 188 | 194 | 360 |
| Tests (Python 3.13) | 172 | 179 | 187 | 360 |
| Native engines (Rust) | 107 | 113 | 117 | 240 |
| Native engines (Rust) — yield statistics | 41 | 44 | 45 | 120 |
| Native engines (Rust) — static timing | 39 | 43 | 46 | 120 |
| Native engines (Rust) — congestion pre-check | 37 | 38 | 41 | 120 |
| Native engines (Rust) — waveform build/query | 22 | 28 | 47 | 120 |
| klt verify Action smoke test | 21 | 26 | 28 | 120 |
| Native engines (Rust) — technology mapping | 12 | 13 | 14 | 120 |
| Lint (ruff) | 12 | 14 | 55 | 120 |
| site/ (tsc + vitest) | 7 | 9 | 10 | 120 |

Aggregates, in seconds:

| Aggregate | min | median | p90 | max | budget |
|---|---:|---:|---:|---:|---:|
| total job-seconds (compute) | 978 | 1,006 | — | 1,094 | 1,500 |
| run wall clock (incl. queue) | 363 | 408 | 488 | 782 | 1,800 |

### What the profile says

**The four `test` matrix legs are the whole story.** They are 705 s of the
~1,006 s median total — 70% of all compute — and each is individually the
longest job in the run. The Rust `native` legs are a distant second at ~236 s
combined across five jobs; everything else put together is under 60 s. Any
future attempt to make this repo's CI faster starts and very nearly ends with
the Python test matrix: either it gets faster, or it gets split so the four
Python versions stop each paying the full suite's cost.

**The run is not queue-starved and is not badly parallelised.** 1,006 s of
compute finishing in 408 s of wall clock is ~2.5x overlap, and the 782 s
worst case is still under the summed compute — the opposite of the
`sky130-pll` pattern. There is no fast-job-on-a-heavy-runner misrouting to fix
here.

**Short jobs are noisy in relative terms.** `Lint (ruff)` has a 12 s median
and a 55 s max — a 4.5x spread from runner cold-start variance alone. That is
why every job under ~60 s gets a flat 120 s floor rather than a proportional
budget: a tight threshold on a 9-second job measures the runner, not the code.

## Budget derivation

- **Per job**: roughly `2x p90`, rounded to a clean number, with a **120 s
  floor** for the reason above. The floor means short jobs only trip the gate
  on a large, unambiguous regression.
- **Unbudgeted job**: falls back to `default_job_budget_seconds` (600 s), so a
  newly added job is covered on the day it lands rather than whenever someone
  remembers to add a row. Budget coverage cannot silently drift as jobs are
  added.
- **Total job-seconds**: 1,500 s, ~37% above the observed 1,094 s max. This is
  the only threshold that catches death by a thousand cheap jobs, which no
  per-job budget would ever notice.
- **Run wall clock**: 1,800 s, ~2.3x the observed 782 s max. Deliberately
  loose: this is the one number that includes pre-run queue time, which is not
  a property of the code under test.

## Compute breaches vs. queue breaches

The check **classifies** every breach, because the distinction is the entire
finding of issue #1971 — telling a reader to optimise jobs that are fine is how
six hours of queue time gets misread as slow tests.

- A per-job or total-compute overrun is a **compute** breach: the work grew.
  Shrink it, or raise the budget in the same PR and say why.
- A wall-clock overrun *while compute is within budget* is a **queue** breach:
  runner-pool contention. Optimising the jobs would achieve nothing.

**Only a compute breach fails the build.** A queue-only breach prints its
report, annotates and lands in the step summary, then exits 0 — failing a build
for contention no PR author can fix is how a red check earns the reflex of
being ignored (a hard queue gate, if ever wanted, belongs behind an opt-in
flag).

A slowdown is never reported as both — when compute is over, the wall-clock
line is suppressed so the actionable breach is not buried.

## Fork PRs are report-only

`ci.yml`'s runner conditional routes fork PRs from `[self-hosted, heavy]` back
to `ubuntu-latest`, whose per-job timings the budgets above — measured entirely
on the self-hosted pool — do not describe. A fixed threshold tuned against
self-hosted numbers would redden every fork PR, so the workflow passes
`--report-only` for them: breaches print as `::warning::` annotations and the
job still exits 0. Same-repo pushes and PRs gate for real.

## Re-measuring

After any deliberate change in CI cost — adding a job, splitting the test
matrix, moving work between jobs — re-derive the baseline and update both the
`baseline` block and the `jobs` budgets in
`.github/ci-wall-clock-budget.json`:

```bash
REPO=2AMLogic/klayout-tools
for id in $(gh run list --workflow=ci.yml --status=success --limit=25 \
              --json databaseId --jq '.[].databaseId'); do
  gh api "repos/$REPO/actions/runs/$id/jobs?per_page=100" \
    --jq '.jobs[] | select(.conclusion=="success")
          | [.name, ((.completed_at|fromdateiso8601) - (.started_at|fromdateiso8601))]
          | @tsv'
done
```

`tests/test_ci_wall_clock.py::test_repo_budgets_leave_headroom_over_the_measured_baseline`
asserts the recorded baseline sits below the budgets, so a transcription slip
cannot ship a budget that is already breached on a green run.

**Re-measurement is also mandatory after any runner-pool change** — adding or
removing self-hosted runners, resizing them, or otherwise changing what
`[self-hosted, heavy]` resolves to — even when nothing in `ci.yml` itself
changes. Every number on this page (the per-job budgets, the total-compute
budget, and especially the wall-clock budget, which is queue time plus
compute) is calibrated against *this* pool's contention and hardware. A pool
change invalidates that calibration exactly like a job-cost change does, and a
stale budget is either a spurious compute breach (pool got slower) or a gate
that no longer catches a real regression (pool got faster).

## Running the check locally

```bash
RUN_ID=<a ci.yml run id>
REPO=2AMLogic/klayout-tools
gh api "repos/$REPO/actions/runs/$RUN_ID/jobs?per_page=100" > /tmp/ci-jobs.json
gh api "repos/$REPO/actions/runs/$RUN_ID"                   > /tmp/ci-run.json

python3 scripts/check_ci_wall_clock.py \
  --jobs-json /tmp/ci-jobs.json --run-json /tmp/ci-run.json
```

Exit codes: `0` within budget, a queue-only breach, or `--report-only`; `1` a
compute budget breach; `2` the check could not run — a malformed budget file or
a payload with nothing measurable in it never reports a vacuous pass.
