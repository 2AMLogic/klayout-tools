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
allows 45 minutes for a job whose median is 4 minutes — so a leg can triple in
cost and stay green. The budget check fires in the band where a real regression
actually lives.

## Measured baseline

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
asymmetry is the basis of the design note below.

**The run is not queue-starved, but it queues deeper under burst.** 1,684 s of
compute finishing in 477 s of wall clock is ~3.5x overlap. The tail is worse
than before: one sampled PR run waited 33 minutes for two Blacksmith legs to
get a machine (2,255 s wall on 1,633 s of compute). That is a queue breach, not
a compute breach — report-only by design, and exactly the signal the wall-clock
threshold exists to surface.

**Short jobs are noisy in relative terms.** `Golden artifacts` has a 16 s
median and a 54 s max; `site/ (tsc + vitest)` runs 9 s and spikes to 34 s — a
3.4x spread from runner cold-start variance alone. That is why every job under
~60 s gets a flat 120 s floor rather than a proportional budget: a tight
threshold on a 9-second job measures the runner, not the code.

## Budget derivation

- **Per job**: roughly `2x p90`, rounded to a clean number, with a **120 s
  floor** for the reason above. The floor means short jobs only trip the gate
  on a large, unambiguous regression.
- **The four `Tests` legs**: pooled over 755 leg observations they measure
  median 253 s, p90 361 s, p95 412 s, max 996 s. `2x p90` is 722 s, rounded to
  **700** (the 2026-09-17 baseline rounded `2x194 = 388` down to 360 the same
  way), shared by all four because they are one suite differing only by
  interpreter. 700 is also the knee of the in-sample false-breach curve: every
  ceiling from 700 to 800 leaves the same 4 of 755 legs over, so another 100 s
  of laxity buys nothing. It drops the sampled main-push false-breach rate from
  8 of 72 runs to 2 — one of which is the excluded bad-node incident, leaving a
  single non-incident residual (run `36611724105`, one leg at 809 s). That
  residual is what the design note below is about, not an argument for another
  150 s of laxity. PR #2571's interim 700 s unblock is therefore
  confirmed by derivation rather than left standing as a placeholder (its
  companion total of 2,600 s is superseded by the 3,000 s below).
- **Two rows moved with the pools, not with the code**: `— static timing` goes
  120 → **150** because `6da0c9a4` moved it to GitHub-hosted (p90 43 s → 64 s,
  `2x p90` = 128 s), and `Native engines (Rust)` **holds 240** rather than
  dropping to the 200 its `2x p90` of 182 s suggests — its sibling legs on the
  same Blacksmith pool show max/p90 ratios up to 3.4x, so a 200 s ceiling would
  measure the pool. 240 s is still 1.8x its observed 136 s max.
- **Unbudgeted job**: falls back to `default_job_budget_seconds` (600 s), so a
  newly added job is covered on the day it lands rather than whenever someone
  remembers to add a row. Budget coverage cannot silently drift as jobs are
  added.
- **Total job-seconds**: 3,000 s — 6% above the observed 2,833 s max and 78%
  above the median. Deliberately *not* the ~3,460 s that four 700 s legs plus
  the cheap jobs could reach with every per-job budget still passing: this is
  the only threshold that catches death by a thousand cheap jobs, so it has to
  bind somewhere below the sum of the per-job ceilings.
- **Run wall clock**: **1,800 s, unchanged** — 1.8x the observed 1,006 s max
  for a comparable run, and still clear of the 2,255 s burst-queue excursion
  described above only in the sense that such an excursion *should* annotate.
  It is the one number that includes pre-run queue time, which is not a
  property of the code under test, and the only threshold whose breach does not
  fail the build.

### Is a fixed per-job ceiling still the right model?

**Judgment call, recorded per issue #2612: no, but the fix is a better
statistic rather than a bigger number — and it is out of scope here.** On the
retired fixed pool a `Tests` leg's max was 1.16x its median, so a `2x p90`
ceiling sat comfortably between "noise" and "regression". On the autoscaled
pool the max is 3.9x the median, which puts the noise tail *above* the
regression signal: 700 s is the loosest useful ceiling, and by construction it
can no longer notice a 2x slowdown of the 253 s median — the exact class of
regression #2359/#2392 caught.

The concrete recommendation, if this is ever revisited in
`scripts/check_ci_wall_clock.py`, is to gate the matrix on the **median of the
four legs** rather than on each leg independently. The four run the same suite
on the same pool in the same run, so pool noise moves one leg
(439/442/459/996) while code moves all four (724/741/810/816). Over the same
sample the median-of-four statistic runs median 252 s, p90 350 s, p99 510 s,
**max 564 s** — against a 996 s worst single leg. A ceiling in the 550-600 s
band would therefore have breached **0 of 188** sampled runs while still
tripping on the excluded incident (median-of-four 775 s) *and*, unlike the
700 s per-leg ceiling, on a uniform 2x slowdown of the 253 s median. That is a
gate that is simultaneously quieter and ~1.2x tighter. A two-consecutive-breach
rule would also suppress single-run noise, but it costs a run of latency and
needs cross-run state; the median-of-legs statistic needs neither.

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

## Cache-miss rebuilds are not compute

Each `Tests` leg restores pinned **Yosys**, **SymbiYosys + Bitwuzla**,
**Icarus Verilog** and **Verilator** builds from `actions/cache`, and rebuilds
them **from source only when that restore misses** — up to ~570 s per leg, and
in the worst sampled case ~1,780 s. That time is a property of the cache
service, not of the code under test.

Issue #2617: on 2026-09-29/30 the Actions cache service degraded (restores
returning `(500) Internal Server Error`, then reporting "Cache not found" for
entries that existed), every leg rebuilt from source, the check counted that as
compute, and `main` plus every open PR went red. Eleven Judge-approved PRs were
each analysed by hand and all eleven were environmental. A cache outage looked
exactly like a code regression — so the check now tells them apart.

**How the split is made.** The `/jobs` payload the check already reads carries
each job's `steps[]`, so no extra API call and no workflow change is needed.
Per job, the check excludes:

- every step whose name ends with **`" (cache miss only)"`** *and that actually
  ran* — on a cache hit these steps are reported `skipped`, so a rebuild step
  with any other conclusion is an unambiguous miss signal;
- that job's own **`Cache …` / `Post Cache …`** restore and save steps, but
  **only in a job where a rebuild ran**. These are ~1 s each on a healthy run;
  the retry loop of a degraded cache service is exactly when they stop being
  ~1 s, and a freshly built cache also has to be uploaded.

Nothing else is ever excluded. `pytest`, the techmap build, the apt installs
and every other step stay in the compute figure, so a real slowdown breaches
just as it did before — including in the same run as a cache miss.
`tests/test_ci_wall_clock.py` pins both halves of that:
`test_cache_miss_rebuild_time_does_not_breach_the_budget` and
`test_a_real_slowdown_alongside_a_cache_miss_still_fails`.

**What you see.** The per-job row shows the compute figure with the excluded
time called out (`(+202s cache miss, excluded)`), a `CACHE MISS:` block names
the affected jobs and the exact rebuild steps, and `--annotate` emits one
`::notice::` per affected job — a notice, not an error, because a missed cache
is neither the author's doing nor the author's to fix. `--format json` keeps it
auditable: per job `cache_miss_seconds`, `cache_miss_steps` and `total_seconds`
(what the GitHub UI shows) alongside the budget-compared `seconds`, plus a
run-level `total_cache_miss_seconds`.

**Run wall clock is *not* adjusted.** It measures the run as it really elapsed,
and its breach is report-only anyway; when a cache miss is present the queue
guidance says so rather than pointing only at runner contention.

**This is a step-name convention, and it is load-bearing.** Renaming those
steps in `ci.yml` without updating `CACHE_MISS_STEP_SUFFIX` /
`CACHE_STEP_PREFIXES` in `scripts/check_ci_wall_clock.py` would silently fold
rebuild time back into compute and re-create the false-red.
`test_cache_miss_step_naming_convention_still_holds_in_ci_yml` guards the link.

**Raising the budgets is not the alternative.** #2582's 420 → 480 s bump did
not turn its run green, and a budget loose enough to absorb a 1,780 s rebuild
would no longer catch anything real.

## Fork PRs are report-only

`ci.yml`'s runner conditional routes fork PRs from `blacksmith-4vcpu-ubuntu-2404`
back to `ubuntu-latest`, whose per-job timings the budgets above — measured on
whichever pool serves each job for a same-repo run — do not describe. A fixed
threshold tuned against Blacksmith numbers would redden every fork PR, so the
workflow passes
`--report-only` for them: breaches print as `::warning::` annotations and the
job still exits 0. Same-repo pushes and PRs gate for real.

## Re-measuring

After any deliberate change in CI cost — adding a job, splitting the test
matrix, moving work between jobs — re-derive the baseline and update both the
`baseline` block and the `jobs` budgets in
`.github/ci-wall-clock-budget.json`:

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
4. **Sample the *compute* figure, not the raw job duration.** The one-liner
   above measures `completed_at - started_at`, which still includes any
   cache-miss rebuild the check now excludes (see
   [Cache-miss rebuilds are not compute](#cache-miss-rebuilds-are-not-compute)).
   A cache-outage window therefore inflates the sample exactly where the gate
   no longer looks. The simplest way to get the number the check compares is to
   run the check itself over each sampled run and read `--format json`:

   ```bash
   gh api "repos/$REPO/actions/runs/$id/jobs?per_page=100" > /tmp/jobs.json
   python3 scripts/check_ci_wall_clock.py --jobs-json /tmp/jobs.json \
     --format json --report-only \
     | jq -r '.jobs[] | [.name, .seconds, .cache_miss_seconds] | @tsv'
   ```

   The budgets recorded above predate this split and were derived from raw
   durations, which makes them mildly *conservative* (a touch looser than a
   compute-only derivation would give) — noted rather than corrected here,
   since only the next full re-measure should move those numbers.

Exclude a run only when it is a *distinguishable incident* rather than the
noise the budget exists to tolerate — e.g. `36003284901`, whose four `Tests`
legs spiked together to 724-816 s. Say so in the `baseline.sample` string, and
sanity-check that the excluded run would still breach the budget you derived.

`tests/test_ci_wall_clock.py::test_repo_budgets_leave_headroom_over_the_measured_baseline`
asserts the recorded baseline sits below the budgets, so a transcription slip
cannot ship a budget that is already breached on a green run.

**Re-measurement is also mandatory after any runner-pool change** — moving a
job between pools, resizing runners, or otherwise changing what a `runs-on`
label resolves to — even when nothing else in `ci.yml` changes. Every number on
this page (the per-job budgets, the total-compute budget, and especially the
wall-clock budget, which is queue time plus compute) is calibrated against
*these* pools' contention and hardware. A pool change invalidates that
calibration exactly like a job-cost change does, and a stale budget is either a
spurious compute breach (pool got slower) or a gate that no longer catches a
real regression (pool got faster).

This is not hypothetical: the 2026-09-24 Blacksmith migration and the
2026-09-28 partial move back to GitHub-hosted both shipped without it, and the
resulting stale budget failed `main` on 8 of 72 sampled runs whose diffs did
not touch test runtime (issue #2612). **A PR that changes a `runs-on:` value
should re-derive the affected rows in the same PR**, exactly as a PR that adds
compute is expected to.

That applies to the next one already in the plan: `6da0c9a4` was explicitly
"the first half" of retiring Blacksmith across 2AM Logic. The four `Tests`
legs and the three `native` matrix legs are the half still on
`blacksmith-4vcpu-ubuntu-2404`, and they are the rows whose numbers this page
is least confident about — moving them is the moment to redo the table above,
not a moment to reuse it.

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
