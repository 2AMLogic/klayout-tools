# CI wall-clock budget

`ci.yml` carries a **wall-clock budget**: a final `CI wall-clock budget` job
that measures the run it belongs to and fails if a job or the total compute has
regressed past what recent runs say is normal. Since issue #2615 the comparison
is **cross-run** — a rolling baseline plus a measured pool-contention factor —
rather than a hand-committed constant. This page records the model, the
measurements behind it, and what to do when it fires.

- Budgets / tuning: [`.github/ci-wall-clock-budget.json`](../../.github/ci-wall-clock-budget.json)
- Check: [`scripts/check_ci_wall_clock.py`](../../scripts/check_ci_wall_clock.py)
- History fetcher: [`scripts/fetch_ci_wall_clock_history.py`](../../scripts/fetch_ci_wall_clock_history.py)
- Tests: `tests/test_ci_wall_clock.py`
- Measured replay data: `tests/fixtures/ci_wall_clock/actions_timings.json`

## If this check just failed your build

Read the `basis` column in the job's step summary first, then:

1. **`rolling`** — the job took longer than the median of recent `main` runs
   allows, *and* no concurrent run was measurably slow at the same time. If the
   slowdown is real, shrink it. If the new cost is justified, say so in the PR
   and merge: the rolling baseline absorbs it by itself within a few `main`
   runs. **Do not hand-edit `.github/ci-wall-clock-budget.json` — for a job on
   the rolling basis, nothing in its `jobs` map is consulted.**
2. **Believe the pool was busy?** — **re-run the job.** This is a real answer,
   not a shrug: the check reads other runs of the *same commit*, and a clean
   one downgrades the breach to `UNCONFIRMED`, which exits 0. If it breaches
   again on identical code, the slowness is in the code and the build stays
   red. (Pool contention caused by workloads *outside* this repo's Actions
   history is invisible to the pool factor — this is the case that path
   exists for.)
3. **`fixed ceiling`** — that job has fewer than `min_samples` runs of history
   (it is new, or was renamed) and is on the committed fallback. Those numbers
   *are* editable, and a first estimate for a new job belongs there.
4. **`queue` breach** — never fails the build. It is runner-pool contention,
   not the code (issue #1971); see "Compute breaches vs. queue breaches" below.

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
allows 45 minutes for a job whose median is 4 minutes — so a leg can triple in
cost and stay green. The budget check fires in the band where a real regression
actually lives.

## Measured baseline (2026-09-17, historical)

This is the profiling that produced the last hand-derived ceilings. It is kept
because the *shape* it describes still holds and because the `baseline` block
in the budget file is still asserted against the fallback ceilings — but the
`budget` columns below are the **fallback** values, not what CI evaluates
today. The live model is described under "The model" above. (The `Tests` rows
were later raised 360 → 420 by PR #2392 and the total 1,500 → 2,100; the table
records the numbers as first derived.)

Measured **2026-09-29** (issue #2612), replacing the 2026-09-17 baseline and
the interim bumps stacked on it. Two runner-pool changes had landed without the
re-measure this page makes mandatory: the 2026-09-24 migration of the heavy
jobs to **Blacksmith autoscaled runners** (`c66f18fd`, #2214) and the
2026-09-28 move of the short and scheduled jobs back to **GitHub-hosted**
(`6da0c9a4`, #2598).

Sample: **273 `ci.yml` runs from 2026-09-24 to 2026-09-29** — main pushes *and*
same-repo PR runs, and deliberately **not** filtered to `--status=success`
runs (see [Re-measuring](#re-measuring) for why, and for the recipe). Each
observation is still a job that itself concluded `success`; each job's numbers
come only from runs where it executed on the runner label `ci.yml` assigns it
*today*, so the two pool migrations cannot blend into one distribution. Run
`36003284901` (2026-09-24) is excluded as a bad-node incident: all four `Tests`
legs spiked together to 724-816 s on a 3,401 s total.

Per job, in seconds (`n` = observations on the job's current runner):

| Job | runner | n | median | p90 | max | budget |
|---|---|---:|---:|---:|---:|---:|
| Tests (Python 3.10) | blacksmith-4vcpu | 188 | 265 | 368 | 996 | 700 |
| Tests (Python 3.11) | blacksmith-4vcpu | 188 | 252 | 366 | 923 | 700 |
| Tests (Python 3.12) | blacksmith-4vcpu | 189 | 256 | 365 | 627 | 700 |
| Tests (Python 3.13) | blacksmith-4vcpu | 190 | 244 | 349 | 824 | 700 |
| Native engines (Rust) | blacksmith-4vcpu | 206 | 62 | 91 | 136 | 240 |
| Native engines (Rust) — static timing | ubuntu-24.04 | 45 | 52 | 64 | 89 | 150 |
| Native engines (Rust) — yield statistics | blacksmith-4vcpu | 205 | 24 | 36 | 84 | 120 |
| Native engines (Rust) — waveform build/query | ubuntu-24.04 | 45 | 22 | 35 | 62 | 120 |
| Native engines (Rust) — congestion pre-check | blacksmith-4vcpu | 205 | 20 | 30 | 102 | 120 |
| Native engines (Rust) — technology mapping | ubuntu-24.04 | 45 | 19 | 30 | 36 | 120 |
| klt verify Action smoke test | ubuntu-24.04 | 45 | 25 | 27 | 31 | 120 |
| Tests (numpy cross-check) | ubuntu-latest | 272 | 17 | 20 | 32 | 120 |
| Golden artifacts (hash seed + path varied) | ubuntu-latest | 272 | 16 | 19 | 54 | 120 |
| examples/signoff round-trip | ubuntu-24.04 | 45 | 12 | 16 | 21 | 120 |
| Lint (ruff) | ubuntu-24.04 | 44 | 12 | 16 | 26 | 120 |
| site/ (tsc + vitest) | ubuntu-24.04 | 45 | 9 | 10 | 34 | 120 |

Every row above is now derived from real timings. The two jobs the previous
baseline could only estimate at 300 s — `Golden artifacts (hash seed + path
varied)` (#2225) and `Tests (numpy cross-check)` (#2276) — measure p90 19 s and
20 s, so both drop to the 120 s floor. `examples/signoff round-trip` (#2163)
gains its first row; it had been silently inheriting the 600 s
`default_job_budget_seconds`.

Aggregates, in seconds, over the **35 runs carrying exactly today's job set on
today's runner labels** (the composition changed on 2026-09-28, so older runs
are not comparable totals):

| Aggregate | min | median | p90 | max | budget |
|---|---:|---:|---:|---:|---:|
| total job-seconds (compute) | 1,276 | 1,684 | 2,316 | 2,833 | 3,000 |
| run wall clock (incl. queue) | 260 | 477 | 820 | 1,006 | 1,800 |

### What the profile says

**The four `test` matrix legs are still the whole story.** At a ~253 s median
each they are ~1,010 s of the 1,684 s median total — 60% of all compute — and
each is individually the longest job in the run. Everything else put together
is under 700 s. Any future attempt to make this repo's CI faster starts and
very nearly ends with the Python test matrix: either it gets faster, or it gets
split so the four Python versions stop each paying the full suite's cost.

**The autoscaled pool is far noisier than the fixed pool it replaced.** On the
2026-09-17 baseline a `Tests` leg ran 172-200 s — a 16% spread from median to
max. On Blacksmith the same suite runs a 253 s median with a 361 s p90, a 412 s
p95, and a 996 s max: the max is ~3.9x the median. The median itself grew by
~40%, but that is dwarfed by the tail, and the tail is what the gate kept
firing on — 8 of 72 sampled main-push runs had at least one leg over the old
420 s budget on diffs that touch nothing about test runtime.

**Spikes are single-leg; regressions are not.** The worst sampled runs read
439/442/459/**996** and 314/317/394/**923** — one leg stalls while its three
siblings, running the identical suite on the same pool at the same moment, stay
normal. The excluded 2026-09-24 incident is the opposite shape
(724/741/810/816: all four together), and so is a genuine code regression. This
asymmetry is the basis of the pool-contention factor described under "The
model" below.

**The run is not queue-starved, but it queues deeper under burst.** 1,684 s of
compute finishing in 477 s of wall clock is ~3.5x overlap. The tail is worse
than before: one sampled PR run waited 33 minutes for two Blacksmith legs to
get a machine (2,255 s wall on 1,633 s of compute). That is a queue breach, not
a compute breach — report-only by design, and exactly the signal the wall-clock
threshold exists to surface.

**Short jobs are noisy in relative terms.** `Lint (ruff)` has a 12 s median
and a 55 s max — a 4.5x spread from runner cold-start variance alone. That is
why short jobs get a flat floor rather than a proportional budget: a tight
threshold on a 9-second job measures the runner, not the code. (The floor was
120 s in the fixed-ceiling generation; the rolling model's
`job_floor_seconds` is 180 s.)

## Why the fixed ceilings had to go (issue #2615)

The original model compared **one run's** absolute durations against constants
committed in the budget file. On the shared autoscaled pool that model can no
longer tell *"this code got slower"* apart from *"our own dispatch fleet
saturated the pool while this run was on it"*. Measured over 274 `ci.yml` runs,
2026-09-24 → 2026-09-29:

| Situation | The four `Tests (Python 3.1x)` legs |
|---|---|
| Quiet pool | median 253 s, p90 361 s |
| 2026-09-29 **19:39-20:03**, four concurrent runs on four unrelated branches | **every leg of every run** at 849-1247 s |
| 2026-09-29 **17:09-17:12**, four concurrent runs | worst legs 996 / 824 / 664 / 537 s |
| 2026-09-24 run `36003284901` (a genuine whole-matrix slowdown, nothing else on the pool) | 724-816 s |

Two things follow, and they are structural rather than a tuning problem:

1. **Contention inflates every leg of every concurrent run together** — the
   same shape a genuine regression has. No *within-run* statistic separates
   the second row from the fourth, including "take the median of the four
   legs" (which does handle an isolated single-leg spike such as
   `439/442/459/996`, but not this).
2. To stay quiet through row two, a fixed per-leg ceiling would have to sit
   around **1400 s — ~5.5x the median**. At that setting the gate is a
   gross-failure backstop, not a regression detector: the class of regression
   it was built for (#2359/#2392, a 2x constant-factor slowdown) passes
   silently. Re-deriving the constant after every pool change was itself the
   other recurring failure mode (#2406, #2612).

The only thing that distinguishes a slow *run* from slow *code* is **state
from other runs**.

## The model

Three inputs, applied in this order, per job and for total compute.

### 1. A rolling baseline

Each job is compared against the **median** of that same job's duration over
the last `baseline_runs` completed runs on `baseline_branch` (`main`). The
allowance is

```
sensitivity = max(job_ratio x median,  median + job_mad_multiplier x MAD,  job_floor_seconds)
```

- **Median and MAD, not mean and standard deviation.** Both survive a minority
  of arbitrarily bad samples, which is exactly the shape of the data — the
  253 s median above comes from a sample whose max is 1247 s. A single
  contended run inside the window cannot drag the gate open.
- **`job_ratio` (1.5)** is the regression band. **`job_mad_multiplier` (8)**
  widens it only for jobs that genuinely are that noisy, instead of loosening
  every job to accommodate the worst one. **`job_floor_seconds` (180)** is the
  successor to the old 120 s floor: a tight threshold on a 12-second job
  measures the runner, not the code.
- **Fewer than `min_samples` (5) observations ⇒ the committed fixed ceiling.**
  That is what the `jobs` map in the budget file is now *for*: covering a job
  on the day it lands, and covering every job when no history could be
  fetched at all.

### 2. A pool-contention factor

Runs whose window **overlapped this one's** by at least
`min_peer_overlap_seconds` (120 s), on a **different commit**, are direct
evidence of what the pool was doing to everybody at that moment. For each such
peer, its ratios against the same rolling baselines are reduced to their
`peer_quantile` (p90); across peers the **maximum** is taken and clamped into
`[1.0, max_pool_factor]`. Every threshold is then multiplied by it.

- **p90 within a peer, not its median**: contention is heavy-tailed and hits
  the longest jobs hardest, so the median across a peer's ~15 jobs badly
  understates what the pool did to its four test legs.
- **Maximum across peers**: one peer demonstrably suffering is sufficient
  evidence that the pool was bad. This is deliberately the conservative
  direction — the failure this whole issue is about is a gate that cried wolf,
  not one that was too forgiving.
- Only jobs with a baseline of at least `min_baseline_seconds` (30 s) vote,
  and a peer needs `min_peer_jobs` (3) of them to count at all.
- Peers are usually still *in flight* when this check runs. Their unfinished
  jobs contribute elapsed-so-far, which is a **lower bound** — such a job can
  prove the pool was slow, never that it was fast, so one that has not yet
  passed its own baseline is ignored.

On the 19:39-20:03 episode this measures a **4.1x** factor; on the 17:09
episode, **2.6-3.2x**; on run `36003284901`, which had nothing else on the
pool, **1.0x**.

### 3. Same-commit corroboration

A compute breach is marked **`UNCONFIRMED`** — reported, annotated as a
warning, exit 0 — when another run of the *same commit* measured the same
subject inside a **quiet-pool** threshold (the same allowance with the pool
factor forced to 1.0). Same code, two verdicts, means the slow one measured
the pool.

This is the "require the breach to repeat" half of #2615, kept as a filter
rather than as the whole model: it costs a run of latency, so it is not used
to gate the *first* observation (run `36003284901` is a single `main` push and
must still fail), only to absorb the residual false positives the pool factor
cannot see. It is also what makes "re-run the job" a real instruction.

### What stays fixed

**`run_wall_clock_budget_seconds` is untouched by the rolling model.** It is
the one number that includes pre-run queue time, it is classified `queue`
rather than `compute`, and a queue breach never fails the build — so there is
nothing for a rolling baseline to protect against there.

### Where the history comes from

**Live `gh api` reads at check time** — this was #2615's main open design
question, and the alternative (a committed or cached rolling artifact) was
rejected:

- The **concurrency evidence only exists live**. Peers are minutes old and
  frequently still running; no artifact written by an earlier run can contain
  them.
- A committed artifact would be **rewritten by, and conflict between, every
  PR**, and would need a bot push on every `main` run to stay fresh —
  re-introducing "somebody has to maintain the number" in a new shape.
- The **permission already exists**: the `CI wall-clock budget` job grants
  `actions: read` and already calls `gh api` for its own run.
- The cost is bounded: two list calls plus one `/jobs` call per selected run,
  capped by `--max-api-calls`, on a job that deliberately runs on
  `ubuntu-latest` so it is never queued behind the pool it measures.

The trade-off accepted is a dependency on the Actions API, which is why
[`scripts/fetch_ci_wall_clock_history.py`](../../scripts/fetch_ci_wall_clock_history.py)
**never fails**: any error writes a valid, empty history and exits 0, and the
check falls back to the committed ceilings — its pre-#2615 behaviour, not a
broken build.

### What this model does *not* catch

- **A regression smaller than ~1.5x of the job's median.** On a quiet pool the
  four test legs already spread 255-495 s across ordinary green `main` runs; a
  20% slowdown is below that noise floor and no threshold tuned against it can
  see one without reddening green runs. The 2026-09-29 measurements are the
  evidence, and a dedicated benchmark — not a CI wall-clock gate — is the
  right instrument for that band.
- **A genuine regression that happens to land during a contention episode.**
  The pool factor widens the gate for everyone on the pool, including the one
  PR that really did get slower. It is caught on the next quiet run of the
  same code, typically the `main` run after merge.
- **Contention from workloads outside this repo's Actions history** (other
  repos sharing the pool). No peer run is visible, so the factor stays 1.0.
  Run `36611724105` — a Loom-surfaces resync commit with literally no compute
  change, whose legs ran 422-809 s with no overlapping `ci.yml` run — is the
  measured example, and re-running it is the documented answer.

### Tuning

Everything above is in the budget file's `rolling_window` block; an unknown key
there is a hard error (exit 2) rather than a silent fallback to defaults, so a
typo cannot quietly disable the tuning it claims to apply. Set
`"enabled": false` to fall back entirely to the committed ceilings.

## Historical: the last hand-derived generation

The `jobs` numbers still in the budget file are the final fixed-ceiling
generation, kept as the fallback described above. They were derived as:

- **Per job**: roughly `2x p90` of the 2026-09-17 baseline, rounded, with a
  **120 s floor**.
- **Unbudgeted job**: `default_job_budget_seconds` (600 s).
- **Total job-seconds**: 1,500 s, ~37% above the observed 1,094 s max, later
  raised to 1,560 (#2276's numpy cross-check job), 1,800 (PR #1869's multi-box
  PEEC tests) and 2,100 (PR #2392), with the four test legs going 360 → 420 at
  the same time. That compounding of headroom-on-headroom, each step
  individually justified, is what the rolling window replaces.
- **Run wall clock**: 1,800 s, ~2.3x the observed 782 s max — and still the
  live value, since wall clock is out of the rolling model's scope.

## Compute breaches vs. queue breaches

The check **classifies** every breach, because the distinction is the entire
finding of issue #1971 — telling a reader to optimise jobs that are fine is how
six hours of queue time gets misread as slow tests.

- A per-job or total-compute overrun is a **compute** breach: the work grew.
  Shrink it, or let the rolling baseline absorb a justified increase.
- A wall-clock overrun *while compute is within budget* is a **queue** breach:
  runner-pool contention. Optimising the jobs would achieve nothing.

**Only a *confirmed* compute breach fails the build.** A queue-only breach — and
since #2615, an `UNCONFIRMED` compute breach, one another run of the same commit
contradicts — prints its report, annotates as a `::warning::` and lands in the
step summary, then exits 0. Failing a build for contention no PR author can fix
is how a red check earns the reflex of being ignored (a hard queue gate, if ever
wanted, belongs behind an opt-in flag).

A slowdown is never reported as both — when compute is over, the wall-clock
line is suppressed so the actionable breach is not buried.

## Fork PRs are report-only

`ci.yml`'s runner conditional routes fork PRs from `blacksmith-4vcpu-ubuntu-2404`
back to `ubuntu-latest`, whose per-job timings the budgets above — measured on
whichever pool serves each job for a same-repo run — do not describe. A fixed
threshold tuned against Blacksmith numbers would redden every fork PR, so the
workflow passes
`--report-only` for them: breaches print as `::warning::` annotations and the
job still exits 0. Same-repo pushes and PRs gate for real.

## Re-measuring

**A pool change no longer requires a re-measure.** That was the second failure
mode #2615 closed: every per-job and total-compute threshold is now derived
from the last `baseline_runs` runs on `main`, so adding, removing or resizing
runners, or changing what `[self-hosted, heavy]` resolves to, is absorbed
within a few `main` runs with nothing to edit. The same goes for a deliberate,
justified increase in a job's cost.

Three things still need a human:

1. **A new or renamed job** — it has no history, so it runs on
   `default_job_budget_seconds` (600 s) or on a row you add to `jobs`. Give it
   a deliberately conservative first estimate and delete nothing; once it has
   `min_samples` runs the rolling window takes over and the row becomes inert.
2. **The `baseline` block** in the budget file, which records the 2026-09-29
   profiling and is asserted against the fallback ceilings by
   `tests/test_ci_wall_clock.py::test_repo_budgets_leave_headroom_over_the_measured_baseline`
   so a transcription slip cannot ship a fallback that is already breached.
3. **Re-tuning `rolling_window`** if the model itself starts misfiring. To
   re-derive the raw numbers the way #2615 did:

```bash
REPO=2AMLogic/klayout-tools
# NOTE: no --status=success. Sample runs of BOTH conclusions and filter at the
# JOB level instead -- see "sample failures too" below.
for id in $(gh run list --workflow=ci.yml --limit=50 \
              --json databaseId,conclusion \
              --jq '.[] | select(.conclusion=="success" or .conclusion=="failure")
                    | .databaseId'); do
  gh api "repos/$REPO/actions/runs/$id/jobs?per_page=100" \
    --jq '.jobs[] | select(.conclusion=="success")
          | [.name, ((.labels // []) | join(",")),
             ((.completed_at|fromdateiso8601) - (.started_at|fromdateiso8601))]
          | @tsv'
done
```

Three rules make the resulting numbers trustworthy (all three learned the hard
way in issue #2612):

1. **Sample failures too.** Filtering runs to `--status=success` drops every
   run the budget check itself reddened — i.e. precisely the tail that decides
   whether the budget holds. Filter at the *job* level (`select(.conclusion ==
   "success")`, as above) so a job's own timing is still a clean measurement,
   but let the run's overall conclusion be anything.
2. **Group by runner label, not just by job name.** The `.labels` field above
   records which pool actually served the job. A job that moved pools has two
   distributions in the same name; only the observations on the label `ci.yml`
   assigns it *today* describe the budget you are about to write.
3. **Take aggregates only from runs with today's job set.** `total
   job-seconds` and run wall clock are properties of the whole run, so a run
   from before a job was added (or from a branch that adds one) is not a
   comparable total. Per-job rows have no such constraint and can use the
   wider sample.

Exclude a run only when it is a *distinguishable incident* rather than the
noise the budget exists to tolerate — e.g. `36003284901`, whose four `Tests`
legs spiked together to 724-816 s. Say so in the `baseline.sample` string, and
sanity-check that the excluded run would still breach the budget you derived.

`tests/test_ci_wall_clock.py::test_repo_budgets_leave_headroom_over_the_measured_baseline`
asserts the recorded baseline sits below the budgets, so a transcription slip
cannot ship a budget that is already breached on a green run.

Any re-tuning should be validated by replaying the captured episodes in
`tests/fixtures/ci_wall_clock/actions_timings.json` — the contention runs must
stay green, run `36003284901` must stay red, and the leave-one-out replay over
the fifteen measured `main` runs must stay green.

## Running the check locally

```bash
RUN_ID=<a ci.yml run id>
REPO=2AMLogic/klayout-tools
gh api "repos/$REPO/actions/runs/$RUN_ID/jobs?per_page=100" > /tmp/ci-jobs.json
gh api "repos/$REPO/actions/runs/$RUN_ID"                   > /tmp/ci-run.json

# Optional but recommended -- without it every job falls back to its committed
# fixed ceiling, which is NOT what CI evaluates.
python3 scripts/fetch_ci_wall_clock_history.py \
  --repo "$REPO" --run-id "$RUN_ID" --out /tmp/ci-history.json

python3 scripts/check_ci_wall_clock.py \
  --jobs-json /tmp/ci-jobs.json --run-json /tmp/ci-run.json \
  --history-json /tmp/ci-history.json
```

Exit codes: `0` within budget, a queue-only breach, an `UNCONFIRMED` compute
breach, or `--report-only`; `1` a confirmed compute budget breach; `2` the
check could not run — a malformed budget or history file, or a payload with
nothing measurable in it, never reports a vacuous pass.
