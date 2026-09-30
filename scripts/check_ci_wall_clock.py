#!/usr/bin/env python3
"""Assert a CI run stayed inside its documented wall-clock budget (issue #1971).

Issue #1971 surveyed CI wall clock across the 2AM Logic repos and found that
for most of them wall clock is dominated by *self-hosted runner queue time*,
not by compute -- `sky130-pll` did 24 s of work in 6.5 hours. `klayout-tools`
was the one exception: ~1000 s of real summed job seconds, already parallelised
down to ~400-780 s of run wall clock. That makes it the one repo where the
work itself has to shrink, and the one repo where a compute regression is worth
gating on.

This script is that gate. It reads the GitHub Actions `/jobs` payload for a
run (plus, optionally, the run object) and compares three things against
budgets committed in `.github/ci-wall-clock-budget.json`:

  1. **each job's own duration** -- catches one leg growing;
  2. **the summed job seconds** (total compute) -- catches death by a thousand
     new jobs, which no single per-job budget would ever notice;
  3. **the run's wall clock** -- which, unlike (1) and (2), includes the
     pre-run queue wait.

Crucially it *classifies* a breach as `compute` or `queue`. The whole survey
in #1971 turned on that distinction: a wall-clock breach while compute is
comfortably within budget is a scheduling problem (runner contention), and
telling the reader to go optimise jobs that are fine is how six hours of queue
time gets misread as slow tests. Only a genuine compute breach points at the
code under test.

## The comparison model (issue #2615)

Comparing one run's absolute durations against *hand-committed* ceilings
stopped working once CI moved onto a shared autoscaled pool: contention
inflates every leg of every concurrent run together, which is the same shape a
genuine regression has, so no within-run statistic separates them. Re-deriving
the constants after every pool change (#2406, #2612) only ever produced a
ceiling loose enough to swallow the contention envelope -- and therefore too
loose to catch the 2x constant-factor slowdowns the gate exists for.

So the comparison is now *cross-run*, and takes three inputs, in this order:

  1. **A rolling baseline.** Each job is compared against the median (and MAD)
     of that same job's duration over the last N completed runs on the
     baseline branch, supplied via `--history-json`. Nobody hand-derives a
     number; the baseline follows the pool.
  2. **A pool-contention factor.** Runs that overlapped this run's own window
     on a *different* tree are direct evidence of what the pool was doing to
     everybody at that moment. If they were inflated k-fold against the same
     rolling baseline, this run's thresholds are scaled by k (capped).
  3. **Same-tree corroboration.** A breach on a tree that *also* has a clean
     run of the same commit is reported as UNCONFIRMED and does not fail the
     build -- "re-run it" is then a real answer, not a shrug. This is the
     "require the breach to repeat" half of #2615, used to filter the residual
     false positives the pool factor cannot see (contention from workloads
     outside this repo's own Actions history).

Without `--history-json` -- and for any job with too few samples in it -- the
check falls back to the committed per-job ceilings in the budget file, which
are retained exactly for that purpose. The check therefore still works offline
and on day one of a newly added job.

**Cache-miss rebuilds are not compute** (issue #2617). `ci.yml`'s `test` legs
restore pinned Yosys/SymbiYosys/Icarus/Verilator builds from `actions/cache`
and, *only when that restore misses*, rebuild them from source -- up to ~570 s
per leg. That time is a property of the cache service, not of the code under
test, so on 2026-09-29/30 an Actions cache degradation turned every leg red and
blocked eleven otherwise-approved PRs. This script therefore reads each job's
`steps[]` (already present in the `/jobs` payload), subtracts the time spent in
the `" (cache miss only)"` rebuild steps that actually *ran* -- plus that job's
own `actions/cache` restore/save steps, whose retry time the same outage
inflates -- from the job's compute figure, and reports the miss explicitly.
Nothing else is subtracted: a slower pytest step still breaches, exactly as
before. `scripts/fetch_ci_wall_clock_history.py` applies the same subtraction
(this module's `measure_cache_miss`) to every history run, so the rolling
baseline and the judged run are the same kind of number.

Exit codes (mirroring `scripts/check-release-lag.sh`'s tiering):

  0  within budget, a queue-only breach, an UNCONFIRMED compute breach, or a
     `--report-only` run
  1  a confirmed compute budget breach
  2  the check itself could not run (unreadable/incoherent budget or history
     file, or a payload with nothing measurable in it) -- a check that
     silently stops checking is worse than no check at all, so this is never a
     green.

Only a *compute* breach exits 1. A queue-only breach is printed, annotated and
summarised but does not fail the build: the classification above says it is not
the author's to fix, and reddening a build for runner-pool contention is how a
check earns the reflex of being ignored. A hard queue gate, if ever wanted,
belongs behind an opt-in flag.

Usage:

    gh api "repos/$REPO/actions/runs/$RUN_ID/jobs?per_page=100" > jobs.json
    gh api "repos/$REPO/actions/runs/$RUN_ID"                   > run.json
    python3 scripts/fetch_ci_wall_clock_history.py \
        --repo "$REPO" --run-id "$RUN_ID" --out history.json
    python3 scripts/check_ci_wall_clock.py \
        --jobs-json jobs.json --run-json run.json --history-json history.json

Everything is standard library on purpose: this job must be fast and must not
need `uv sync`, or the budget check becomes part of the problem it measures.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

# The budget file's own schema (`.github/ci-wall-clock-budget.json`). Still 1:
# #2615 only *added* an optional `rolling_window` block, so a budget file
# written before it stays readable and simply runs with the rolling model off.
BUDGET_SCHEMA_VERSION = 1
# The history payload written by scripts/fetch_ci_wall_clock_history.py.
HISTORY_SCHEMA_VERSION = 1
# This script's own `--format json` payload. Bumped to 2 by #2615: every field
# the old shape carried is still emitted, plus the `model` block and the
# per-job/per-breach model fields.
OUTPUT_SCHEMA_VERSION = 2
DEFAULT_BUDGET_FILE = (
    Path(__file__).resolve().parent.parent / ".github" / "ci-wall-clock-budget.json"
)

EXIT_OK = 0
EXIT_BREACH = 1
EXIT_CANNOT_RUN = 2

# Conclusions that carry no meaningful duration.
_UNMEASURABLE_CONCLUSIONS = frozenset({"skipped", "cancelled", "neutral"})

# Defaults for the rolling-window model (issue #2615). Every one of these is
# overridable from the budget file's `rolling_window` block; the values here
# are what the model runs with when that block is absent or partial, and are
# the ones docs/guides/ci-wall-clock-budget.md derives.
DEFAULT_ROLLING = {
    "enabled": True,
    # Fewer than this many samples for a job name => that job keeps its
    # committed fixed ceiling. A newly added job is covered from day one.
    "min_samples": 5,
    # Per-job threshold: max(job_ratio x median, median + mad_multiplier x MAD,
    # floor). The ratio is the "2x the typical run" regression band; the MAD
    # term widens it only for jobs that genuinely are that noisy; the floor
    # keeps a 20-second job from being gated on runner cold-start variance.
    "job_ratio": 1.5,
    "job_mad_multiplier": 8.0,
    "job_floor_seconds": 180,
    # Total compute gets a tighter ratio: it is a sum over ~15 jobs, so it is
    # far less noisy than any single one of them.
    "total_ratio": 1.35,
    "total_mad_multiplier": 6.0,
    "total_floor_seconds": 600,
    # A job needs a baseline at least this long before its ratio is allowed to
    # vote on the pool-contention factor -- short jobs have a 4x spread from
    # cold-start variance alone and would dominate the estimate with noise.
    "min_baseline_seconds": 30,
    # A peer run needs at least this many baselined jobs before it is credible
    # evidence about the pool.
    "min_peer_jobs": 3,
    # How hard a busy pool is allowed to widen the thresholds. Past this the
    # right answer is "the pool is broken", not "raise the gate".
    "max_pool_factor": 6.0,
    # Which quantile of a peer's own job ratios represents what the pool did to
    # it. Contention is heavy-tailed -- it hits the longest jobs hardest -- so
    # the median of a peer's jobs understates it badly.
    "peer_quantile": 0.9,
}

# `ci.yml`'s naming convention for the two halves of a pinned-tool install
# (issue #2617). Both are load-bearing here, so a rename in the workflow must
# be mirrored in this file -- see docs/guides/ci-wall-clock-budget.md.
#
#   "Cache pinned Yosys build"                        <- actions/cache restore
#   "Build + install pinned Yosys (cache miss only)"  <- `if: cache-hit != 'true'`
#
# On a cache hit the rebuild step is reported as `skipped`, so a rebuild step
# with any *other* conclusion is a positive, unambiguous cache-miss signal --
# no workflow change and no extra API call needed to detect one.
CACHE_MISS_STEP_SUFFIX = " (cache miss only)"
CACHE_STEP_PREFIXES = ("Cache ", "Post Cache ")


class CannotRun(Exception):
    """The check could not be performed (exit 2, never a silent pass)."""


@dataclass(frozen=True)
class Baseline:
    """A job's (or the total's) rolling statistics over the history window."""

    median_seconds: float
    mad_seconds: float
    samples: int


@dataclass(frozen=True)
class HistoryRun:
    """One prior/concurrent run, reduced to job-name -> seconds."""

    run_id: int | None
    head_sha: str | None
    roles: frozenset[str]
    jobs: dict[str, int]
    # Jobs that were still running when the history was captured: their
    # seconds are a *lower bound*, usable as evidence that the pool was slow
    # but never as evidence that it was fast.
    partial_jobs: frozenset[str] = frozenset()


@dataclass(frozen=True)
class JobTiming:
    """One job's timing, with cache-miss recovery time held separately.

    `seconds` is the **compute** figure -- the number compared against the
    budget -- and deliberately excludes `cache_miss_seconds`. `total_seconds`
    is what the GitHub UI shows for the job.
    """

    name: str
    seconds: int
    budget_seconds: int
    # "fixed" -> the committed ceiling; "rolling" -> derived from history.
    basis: str = "fixed"
    baseline: Baseline | None = None
    cache_miss_seconds: int = 0
    cache_miss_steps: tuple[str, ...] = ()

    @property
    def over_budget(self) -> bool:
        return self.seconds > self.budget_seconds

    @property
    def total_seconds(self) -> int:
        return self.seconds + self.cache_miss_seconds

    @property
    def cache_missed(self) -> bool:
        # Duration is not the signal -- a rebuild step that *ran at all* is.
        return bool(self.cache_miss_steps)


@dataclass(frozen=True)
class Breach:
    kind: str  # "compute" | "queue"
    scope: str  # "job" | "total" | "run"
    subject: str
    actual_seconds: int
    budget_seconds: int
    # False when another run of this same commit measured the same subject
    # *within* threshold: one noisy run does not fail the build (#2615).
    confirmed: bool = True
    basis: str = "fixed"
    note: str = ""

    @property
    def message(self) -> str:
        over = self.actual_seconds - self.budget_seconds
        qualifier = "" if self.confirmed else "UNCONFIRMED "
        tail = f" -- {self.note}" if self.note else ""
        return (
            f"{qualifier}{self.subject}: {self.actual_seconds}s exceeds the "
            f"{self.budget_seconds}s budget by {over}s ({self.kind} breach)"
            f"{tail}"
        )


@dataclass(frozen=True)
class Model:
    """The cross-run comparison model applied to this run (issue #2615)."""

    cfg: dict
    baselines: dict[str, Baseline] = field(default_factory=dict)
    total_baseline: Baseline | None = None
    pool_factor: float = 1.0
    peer_runs: int = 0
    baseline_runs: int = 0
    same_tree_runs: tuple[HistoryRun, ...] = ()

    @property
    def active(self) -> bool:
        """True when at least one subject is compared against history."""
        return bool(self.baselines) or self.total_baseline is not None

    def threshold(self, baseline: Baseline, *, total: bool = False) -> int:
        """The pool-scaled allowance for a subject with this baseline."""
        prefix = "total_" if total else "job_"
        ratio = float(self.cfg[f"{prefix}ratio"])
        mad_k = float(self.cfg[f"{prefix}mad_multiplier"])
        floor = float(self.cfg[f"{prefix}floor_seconds"])
        sensitivity = max(
            ratio * baseline.median_seconds,
            baseline.median_seconds + mad_k * baseline.mad_seconds,
            floor,
        )
        return int(math.ceil(self.pool_factor * sensitivity))

    def unscaled_threshold(self, baseline: Baseline, *, total: bool = False) -> int:
        """The allowance a *quiet* pool would give -- used to judge whether a
        same-commit sibling run was clean for the same subject."""
        quiet = Model(cfg=self.cfg)
        return quiet.threshold(baseline, total=total)


def _parse_iso(stamp: str) -> datetime:
    # GitHub emits `2026-09-17T07:00:00Z`; fromisoformat only learned `Z` in
    # 3.11 and this repo targets 3.10.
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def load_json(path: str) -> object:
    try:
        if path == "-":
            return json.load(sys.stdin)
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CannotRun(f"could not read JSON from {path}: {exc}") from exc


def load_budget(path: str) -> dict:
    budget = load_json(path)
    if not isinstance(budget, dict):
        raise CannotRun(f"budget file {path} is not a JSON object")
    version = budget.get("schema_version")
    if version != BUDGET_SCHEMA_VERSION:
        raise CannotRun(
            f"budget file {path} has schema_version {version!r}, "
            f"expected {BUDGET_SCHEMA_VERSION}"
        )
    for key in (
        "default_job_budget_seconds",
        "total_job_budget_seconds",
        "run_wall_clock_budget_seconds",
    ):
        value = budget.get(key)
        if not isinstance(value, int) or value <= 0:
            raise CannotRun(f"budget file {path} is missing a positive {key!r}")
    if not isinstance(budget.get("jobs", {}), dict):
        raise CannotRun(f"budget file {path} has a non-object 'jobs' map")
    rolling = budget.get("rolling_window", {})
    if not isinstance(rolling, dict):
        raise CannotRun(f"budget file {path} has a non-object 'rolling_window' block")
    for key in rolling:
        if key not in DEFAULT_ROLLING and key not in _FETCHER_ONLY_ROLLING_KEYS:
            raise CannotRun(
                f"budget file {path} has an unknown 'rolling_window' key {key!r}"
            )
    return budget


# Keys the *fetcher* reads out of `rolling_window` to decide what history to
# collect. They are meaningless to this script but must not be rejected as
# typos when it validates the block.
_FETCHER_ONLY_ROLLING_KEYS = frozenset(
    {
        "baseline_branch",
        "baseline_runs",
        "peer_window_runs",
        "min_peer_overlap_seconds",
        "max_age_days",
        "history_not_before",
    }
)


def rolling_config(budget: dict) -> dict:
    cfg = dict(DEFAULT_ROLLING)
    cfg.update(
        {
            k: v
            for k, v in budget.get("rolling_window", {}).items()
            if k in DEFAULT_ROLLING
        }
    )
    for key, default in DEFAULT_ROLLING.items():
        if isinstance(default, bool):
            if not isinstance(cfg[key], bool):
                raise CannotRun(f"rolling_window.{key} must be a boolean")
        elif not isinstance(cfg[key], (int, float)) or isinstance(cfg[key], bool):
            raise CannotRun(f"rolling_window.{key} must be a number")
        elif cfg[key] <= 0:
            raise CannotRun(f"rolling_window.{key} must be positive")
    return cfg


# --------------------------------------------------------------------------
# Rolling-window history (issue #2615)
# --------------------------------------------------------------------------


def load_history(path: str | None) -> list[HistoryRun]:
    """Prior/concurrent runs, or `[]` when no history was supplied.

    An *absent* history is a supported mode: the check falls back to the
    committed ceilings and behaves exactly as it did before #2615, which is
    what makes it runnable locally and off the Actions API. A *present but
    malformed* history is exit 2 -- that is a wiring mistake, and silently
    degrading to the loose fallback is how a gate stops gating without anyone
    noticing.
    """
    if not path:
        return []
    payload = load_json(path)
    if not isinstance(payload, dict):
        raise CannotRun(f"history file {path} is not a JSON object")
    version = payload.get("schema_version")
    if version != HISTORY_SCHEMA_VERSION:
        raise CannotRun(
            f"history file {path} has schema_version {version!r}, "
            f"expected {HISTORY_SCHEMA_VERSION}"
        )
    raw = payload.get("runs", [])
    if not isinstance(raw, list):
        raise CannotRun(f"history file {path} has a non-list 'runs'")

    runs: list[HistoryRun] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise CannotRun(f"history file {path} has a non-object entry in 'runs'")
        jobs = entry.get("jobs", {})
        if not isinstance(jobs, dict):
            raise CannotRun(f"history file {path} has a non-object 'jobs' in an entry")
        cleaned = {
            name: int(seconds)
            for name, seconds in jobs.items()
            if isinstance(name, str)
            and isinstance(seconds, (int, float))
            and not isinstance(seconds, bool)
            and seconds >= 0
        }
        roles = entry.get("roles", [])
        if not isinstance(roles, list):
            raise CannotRun(f"history file {path} has a non-list 'roles' in an entry")
        partial = entry.get("partial_jobs", [])
        if not isinstance(partial, list):
            raise CannotRun(
                f"history file {path} has a non-list 'partial_jobs' in an entry"
            )
        runs.append(
            HistoryRun(
                run_id=entry.get("run_id"),
                head_sha=entry.get("head_sha"),
                roles=frozenset(str(r) for r in roles),
                jobs=cleaned,
                partial_jobs=frozenset(str(p) for p in partial),
            )
        )
    return runs


def _mad(values: list[float], median: float) -> float:
    return statistics.median([abs(v - median) for v in values])


def _quantile(values: list[float], q: float) -> float:
    """Nearest-rank upper quantile -- no interpolation, no numpy."""
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def build_baselines(
    runs: list[HistoryRun], cfg: dict, *, exclude: frozenset[str]
) -> tuple[dict[str, Baseline], Baseline | None, int]:
    """Per-job and total-compute rolling statistics from the baseline runs.

    The median (not the mean) and the MAD (not the standard deviation) are
    deliberate: a contention episode inside the window must not be able to
    drag the baseline up with it. Both survive a minority of arbitrarily bad
    samples, which is exactly the shape of the data (#2615 measured a 253 s
    median across 274 runs whose max was 1247 s).
    """
    baseline_runs = [r for r in runs if "baseline" in r.roles]
    per_job: dict[str, list[float]] = {}
    totals: list[float] = []
    for run in baseline_runs:
        measured = {
            name: seconds
            for name, seconds in run.jobs.items()
            if name not in exclude and name not in run.partial_jobs
        }
        if not measured:
            continue
        for name, seconds in measured.items():
            per_job.setdefault(name, []).append(float(seconds))
        totals.append(float(sum(measured.values())))

    min_samples = int(cfg["min_samples"])
    baselines: dict[str, Baseline] = {}
    for name, values in per_job.items():
        if len(values) < min_samples:
            continue
        median = statistics.median(values)
        baselines[name] = Baseline(median, _mad(values, median), len(values))

    total_baseline = None
    if len(totals) >= min_samples:
        median = statistics.median(totals)
        total_baseline = Baseline(median, _mad(totals, median), len(totals))
    return baselines, total_baseline, len(baseline_runs)


def pool_contention_factor(
    runs: list[HistoryRun], baselines: dict[str, Baseline], cfg: dict
) -> tuple[float, int]:
    """How much slower the pool was running for *everybody else* right then.

    This is the one measurement that separates "this code got slower" from
    "our own dispatch fleet saturated the pool while this run was on it"
    (#2615). Peers are runs of a *different* commit whose window overlapped
    this one's; if they were k-fold slow against the same rolling baselines,
    this run's thresholds are widened k-fold.

    A peer's own contribution is a high quantile of its job ratios, not their
    median: contention is heavy-tailed and hits the longest jobs hardest, so
    the median of a peer's fifteen jobs badly understates what the pool did to
    its four test legs. Across peers the maximum is taken -- one peer
    demonstrably suffering is sufficient evidence that the pool was bad, and
    the failure mode this whole issue is about is the gate being too eager,
    not too forgiving.
    """
    min_baseline = float(cfg["min_baseline_seconds"])
    min_jobs = int(cfg["min_peer_jobs"])
    quantile = float(cfg["peer_quantile"])

    peer_factors: list[float] = []
    for run in runs:
        if "peer" not in run.roles:
            continue
        ratios: list[float] = []
        for name, seconds in run.jobs.items():
            base = baselines.get(name)
            if base is None or base.median_seconds < min_baseline:
                continue
            ratio = seconds / base.median_seconds
            # A still-running job's elapsed time is a lower bound: it can
            # prove the pool was slow, never that it was fast.
            if name in run.partial_jobs and ratio <= 1.0:
                continue
            ratios.append(ratio)
        if len(ratios) >= min_jobs:
            peer_factors.append(_quantile(ratios, quantile))

    if not peer_factors:
        return 1.0, 0
    factor = max(1.0, min(float(cfg["max_pool_factor"]), max(peer_factors)))
    return factor, len(peer_factors)


def build_model(
    runs: list[HistoryRun], budget: dict, *, exclude: frozenset[str]
) -> Model:
    cfg = rolling_config(budget)
    if not cfg["enabled"] or not runs:
        return Model(cfg=cfg)
    baselines, total_baseline, baseline_runs = build_baselines(
        runs, cfg, exclude=exclude
    )
    factor, peers = pool_contention_factor(runs, baselines, cfg)
    return Model(
        cfg=cfg,
        baselines=baselines,
        total_baseline=total_baseline,
        pool_factor=factor,
        peer_runs=peers,
        baseline_runs=baseline_runs,
        same_tree_runs=tuple(r for r in runs if "same-tree" in r.roles),
    )


def apply_model(timings: list[JobTiming], model: Model) -> list[JobTiming]:
    """Replace each job's committed ceiling with its rolling threshold.

    A job with enough history is judged entirely against that history -- the
    committed number is not also applied as an AND, because keeping it would
    reintroduce exactly the hand-derived constant #2615 exists to delete (and
    would redden every contended run again, since no committed ceiling
    survives a 4x pool). Jobs without enough history keep the committed
    ceiling, which is what it is now *for*.
    """
    if not model.active:
        return timings
    updated: list[JobTiming] = []
    for timing in timings:
        base = model.baselines.get(timing.name)
        if base is None:
            updated.append(timing)
            continue
        # `replace`, not a fresh JobTiming: the cache-miss split (#2617)
        # measured on this job must survive the switch to a rolling basis.
        updated.append(
            replace(
                timing,
                budget_seconds=model.threshold(base),
                basis="rolling",
                baseline=base,
            )
        )
    return updated


def _same_tree_clean(
    model: Model, name: str, threshold: int
) -> tuple[HistoryRun, int] | None:
    """A run of this same commit that measured `name` inside `threshold`."""
    for run in model.same_tree_runs:
        seconds = run.jobs.get(name)
        if seconds is None or name in run.partial_jobs:
            continue
        if seconds <= threshold:
            return run, seconds
    return None


def _same_tree_total_clean(
    model: Model, threshold: int, *, exclude: frozenset[str]
) -> tuple[HistoryRun, int] | None:
    """A run of this same commit whose total compute was inside `threshold`."""
    for run in model.same_tree_runs:
        measured = {
            name: seconds
            for name, seconds in run.jobs.items()
            if name not in exclude and name not in run.partial_jobs
        }
        if not measured:
            continue
        total = sum(measured.values())
        if total <= threshold:
            return run, total
    return None


def _jobs_from_payload(payload: object) -> list[dict]:
    if isinstance(payload, dict):
        jobs = payload.get("jobs", [])
    elif isinstance(payload, list):
        jobs = payload
    else:
        raise CannotRun("jobs payload is neither an object nor a list")
    if not isinstance(jobs, list):
        raise CannotRun("jobs payload's 'jobs' field is not a list")
    return jobs


def _step_seconds(step: dict) -> int:
    """A step's duration, or 0 when it is missing/unparseable.

    Step timings are *secondary* data: they only ever subtract from the
    compute figure, so a malformed one must degrade to "subtract nothing"
    rather than take the whole check down (which a `CannotRun` would).
    """
    started, completed = step.get("started_at"), step.get("completed_at")
    if not isinstance(started, str) or not isinstance(completed, str):
        return 0
    if not started or not completed:
        return 0
    try:
        elapsed = _parse_iso(completed) - _parse_iso(started)
    except ValueError:
        return 0
    return max(0, int(elapsed.total_seconds()))


def measure_cache_miss(job: dict) -> tuple[int, tuple[str, ...]]:
    """Seconds this job spent recovering from an `actions/cache` miss.

    Returns `(seconds, rebuilt_step_names)`; `(0, ())` for a job that hit its
    caches, carries no `steps[]`, or does not use the convention at all.

    Only a job with at least one rebuild step that actually ran gets *any*
    time excluded, and only these two kinds of step are ever counted:

      * the `" (cache miss only)"` source builds themselves;
      * that job's own `actions/cache` restore/save steps -- ~1 s each on a
        healthy run, but the retry path of a degraded cache service (the
        `(500) Internal Server Error` loop behind #2617) is precisely where
        they stop being ~1 s.

    Every other step -- pytest above all -- stays in the compute figure, so
    this can never launder a real slowdown.
    """
    steps = job.get("steps")
    if not isinstance(steps, list):
        return 0, ()

    rebuilds: list[tuple[str, int]] = []
    cache_io_seconds = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        name = step.get("name")
        if not isinstance(name, str):
            continue
        if name.endswith(CACHE_MISS_STEP_SUFFIX):
            # `skipped` == the cache hit and the rebuild never ran.
            if step.get("conclusion") in _UNMEASURABLE_CONCLUSIONS:
                continue
            rebuilds.append((name, _step_seconds(step)))
        elif name.startswith(CACHE_STEP_PREFIXES):
            cache_io_seconds += _step_seconds(step)

    if not rebuilds:
        return 0, ()
    seconds = sum(step_seconds for _, step_seconds in rebuilds) + cache_io_seconds
    return seconds, tuple(name for name, _ in rebuilds)


def measure_jobs(
    payload: object, budget: dict, *, exclude: frozenset[str]
) -> tuple[list[JobTiming], datetime, datetime]:
    """Per-job durations plus the earliest start / latest finish across them.

    Jobs still running (null `completed_at` -- which always includes the budget
    job itself, since it measures the run it belongs to) and jobs whose
    conclusion carries no duration are dropped.

    Each job's `steps[]`, when the payload carries them, are used to split its
    duration into compute and `actions/cache`-miss recovery time (#2617); only
    the compute half is compared against the budget.
    """
    per_job_budgets = budget.get("jobs", {})
    default_budget = budget["default_job_budget_seconds"]

    timings: list[JobTiming] = []
    starts: list[datetime] = []
    ends: list[datetime] = []

    for job in _jobs_from_payload(payload):
        if not isinstance(job, dict):
            continue
        name = job.get("name")
        started, completed = job.get("started_at"), job.get("completed_at")
        if not isinstance(name, str) or name in exclude:
            continue
        if not started or not completed:
            continue
        if job.get("conclusion") in _UNMEASURABLE_CONCLUSIONS:
            continue
        try:
            start, end = _parse_iso(started), _parse_iso(completed)
        except ValueError as exc:
            raise CannotRun(
                f"job {name!r} has an unparseable timestamp: {exc}"
            ) from exc
        seconds = max(0, int((end - start).total_seconds()))
        job_budget = per_job_budgets.get(name, default_budget)
        if not isinstance(job_budget, int) or job_budget <= 0:
            raise CannotRun(f"budget for job {name!r} is not a positive integer")
        cache_seconds, cache_steps = measure_cache_miss(job)
        # Steps can overlap the job's own bounds by a second of rounding;
        # never let the excluded slice exceed the job itself.
        cache_seconds = min(cache_seconds, seconds)
        timings.append(
            JobTiming(
                name,
                seconds - cache_seconds,
                job_budget,
                cache_miss_seconds=cache_seconds,
                cache_miss_steps=cache_steps,
            )
        )
        starts.append(start)
        ends.append(end)

    if not timings:
        raise CannotRun(
            "no measurable jobs in the payload -- nothing to compare against "
            "the budget (refusing to report a vacuous pass)"
        )
    return timings, min(starts), max(ends)


def run_start(run_payload: object | None) -> datetime | None:
    """When the run's queue wait is knowable, the moment the run was created."""
    if not isinstance(run_payload, dict):
        return None
    for key in ("run_started_at", "created_at"):
        stamp = run_payload.get(key)
        if isinstance(stamp, str) and stamp:
            try:
                return _parse_iso(stamp)
            except ValueError as exc:
                raise CannotRun(f"run payload's {key!r} is unparseable: {exc}") from exc
    return None


def total_budget_seconds(budget: dict, model: Model) -> tuple[int, str]:
    if model.total_baseline is not None:
        return model.threshold(model.total_baseline, total=True), "rolling"
    return budget["total_job_budget_seconds"], "fixed"


def evaluate(
    timings: list[JobTiming],
    *,
    total_seconds: int,
    wall_clock_seconds: int,
    budget: dict,
    model: Model | None = None,
    exclude: frozenset[str] = frozenset(),
) -> list[Breach]:
    """Breaches, each classified `compute` or `queue`, and each either
    confirmed or UNCONFIRMED.

    A per-job or total-compute overrun is a `compute` breach -- the code under
    test got slower. A wall-clock overrun is only reported when compute itself
    is *within* budget, in which case the extra time is queue/scheduling and is
    classified `queue`. Reporting the same slowdown twice (once as compute,
    once as wall clock) would just bury the actionable line.

    A compute breach is marked UNCONFIRMED -- reported, annotated, but not
    build-failing -- when another run of this *same commit* measured the same
    subject inside a quiet-pool threshold. Same code, two verdicts, means the
    slow one measured the pool (#2615).
    """
    model = model or Model(cfg=dict(DEFAULT_ROLLING))
    breaches: list[Breach] = []
    for timing in timings:
        if not timing.over_budget:
            continue
        confirmed, note = True, ""
        if timing.baseline is not None:
            clean = _same_tree_clean(
                model, timing.name, model.unscaled_threshold(timing.baseline)
            )
            if clean is not None:
                sibling, seconds = clean
                confirmed = False
                note = (
                    f"run {sibling.run_id} of this same commit measured it at "
                    f"{seconds}s, so this is runner noise until it reproduces"
                )
        breaches.append(
            Breach(
                "compute",
                "job",
                timing.name,
                timing.seconds,
                timing.budget_seconds,
                confirmed=confirmed,
                basis=timing.basis,
                note=note,
            )
        )

    total_budget, total_basis = total_budget_seconds(budget, model)
    compute_over = total_seconds > total_budget
    if compute_over:
        confirmed, note = True, ""
        if model.total_baseline is not None:
            clean = _same_tree_total_clean(
                model,
                model.unscaled_threshold(model.total_baseline, total=True),
                exclude=exclude,
            )
            if clean is not None:
                sibling, sibling_total = clean
                confirmed = False
                note = (
                    f"run {sibling.run_id} of this same commit totalled "
                    f"{sibling_total}s, so this is runner noise until it "
                    f"reproduces"
                )
        breaches.append(
            Breach(
                "compute",
                "total",
                "total job-seconds",
                total_seconds,
                total_budget,
                confirmed=confirmed,
                basis=total_basis,
                note=note,
            )
        )

    wall_budget = budget["run_wall_clock_budget_seconds"]
    if wall_clock_seconds > wall_budget and not compute_over:
        breaches.append(
            Breach("queue", "run", "run wall clock", wall_clock_seconds, wall_budget)
        )
    return breaches


def _model_summary(model: Model, total_basis: str) -> list[str]:
    if not model.active:
        return [
            "model: committed fixed ceilings (no usable run history supplied --"
            " see docs/guides/ci-wall-clock-budget.md)",
        ]
    lines = [
        f"model: rolling window over {model.baseline_runs} baseline run(s); "
        f"{len(model.baselines)} job(s) compared against history, "
        f"total compute against {total_basis} budget",
    ]
    if model.peer_runs:
        lines.append(
            f"pool: {model.pool_factor:.2f}x contention factor, measured from "
            f"{model.peer_runs} concurrent run(s) on other commits"
        )
    else:
        lines.append(
            "pool: no concurrent run overlapped this one, so no contention "
            "allowance was applied (factor 1.00x)"
        )
    if model.same_tree_runs:
        lines.append(
            f"tree: {len(model.same_tree_runs)} other run(s) of this same commit "
            f"available to corroborate a breach"
        )
    return lines


def cache_missed_jobs(timings: list[JobTiming]) -> list[JobTiming]:
    """Jobs that rebuilt a pinned tool after an `actions/cache` miss, costliest
    first."""
    return sorted(
        (t for t in timings if t.cache_missed), key=lambda t: -t.cache_miss_seconds
    )


def _render_cache_misses(cache_missed: list[JobTiming]) -> list[str]:
    """The `CACHE MISS:` block -- empty when every cache restored."""
    if not cache_missed:
        return []
    excluded = sum(t.cache_miss_seconds for t in cache_missed)
    return [
        f"CACHE MISS: {len(cache_missed)} job(s) could not restore a pinned-tool "
        "build from actions/cache and",
        f"rebuilt it from source. That {excluded}s is cache-service time, not "
        "compute, so it is",
        "EXCLUDED from the figures above (issue #2617):",
        *(
            f"  - {t.name}: {t.cache_miss_seconds}s ({', '.join(t.cache_miss_steps)})"
            for t in cache_missed
        ),
        "",
    ]


def _breach_guidance(breaches: list[Breach], *, cache_missed: bool) -> list[str]:
    """What the reader should do about `breaches` (a non-empty list)."""
    kinds = {b.kind for b in breaches}
    failing = [b for b in breaches if b.kind == "compute" and b.confirmed]
    guidance: list[str] = []
    if kinds == {"queue"}:
        guidance += [
            "",
            "Compute is WITHIN budget -- this is a scheduling/queue breach, not a",
            "code regression. Optimising these jobs would achieve nothing (issue",
            "#1971): look at runner-pool contention instead. Reported, not gated:",
            "a queue-only breach exits 0 and does not fail this build.",
        ]
        if cache_missed:
            guidance.append(
                "The cache miss above also inflates wall clock, which -- unlike "
                "compute -- is measured whole."
            )
    elif "compute" in kinds and not failing:
        guidance += [
            "",
            "Every compute breach above is UNCONFIRMED: another run of this same",
            "commit measured the same job inside a quiet-pool threshold, so this",
            "run measured the pool, not the code (issue #2615). Reported, not",
            "gated: this exits 0.",
        ]
    elif "compute" in kinds:
        guidance += [
            "",
            "This is a COMPUTE breach: the work took longer than the rolling",
            "baseline of recent runs allows, and the pool was not measurably busy",
            "at the time. What to do, in order:",
            "  1. Read the `basis` column. `rolling` compares against the median",
            "     of recent baseline-branch runs; `fixed ceiling` means that job",
            "     has too little history yet and is on the committed fallback in",
            "     .github/ci-wall-clock-budget.json.",
            "  2. If the slowdown is real, shrink the job -- or, if the new cost",
            "     is justified, say so in the PR. A rolling baseline absorbs a",
            "     justified increase on its own within a few baseline-branch runs;",
            "     no number has to be hand-edited for that.",
            "  3. If you believe the pool was busy and this check could not see it",
            "     (contention from workloads outside this repo's own Actions",
            "     history is invisible here), RE-RUN this job. A clean re-run of",
            "     the same commit both passes and, if it breaches again, marks the",
            "     breach confirmed rather than noise.",
            "  4. Only edit the committed ceilings when a job is on the `fixed",
            "     ceiling` basis; the rolling rows are derived, not declared.",
        ]
        if cache_missed:
            guidance.append(
                "Cache-miss rebuild time is ALREADY excluded above, so this "
                "breach is not the cache service."
            )
    return guidance


def render_text(
    timings: list[JobTiming],
    breaches: list[Breach],
    *,
    total_seconds: int,
    total_budget: int,
    wall_clock_seconds: int,
    wall_budget: int,
    includes_queue: bool,
    report_only: bool,
    model: Model | None = None,
    total_basis: str = "fixed",
) -> str:
    model = model or Model(cfg=dict(DEFAULT_ROLLING))
    width = max([len(t.name) for t in timings] + [len("total job-seconds (compute)")])
    lines = [f"CI wall-clock budget -- {len(timings)} job(s) measured"]
    lines += [f"  {line}" for line in _model_summary(model, total_basis)]
    lines += [
        "",
        f"  {'job'.ljust(width)}  {'actual':>8}  {'budget':>8}  basis",
        f"  {'-' * width}  {'-' * 8}  {'-' * 8}  {'-' * 24}",
    ]
    for t in sorted(timings, key=lambda t: -t.seconds):
        flag = "  OVER" if t.over_budget else ""
        if t.baseline is None:
            basis = "fixed ceiling"
        else:
            basis = (
                f"rolling (median {t.baseline.median_seconds:.0f}s, "
                f"n={t.baseline.samples})"
            )
        if t.cache_missed:
            flag += f"  (+{t.cache_miss_seconds}s cache miss, excluded)"
        lines.append(
            f"  {t.name.ljust(width)}  {t.seconds:>8}  "
            f"{t.budget_seconds:>8}  {basis}{flag}"
        )
    lines.append(f"  {'-' * width}  {'-' * 8}  {'-' * 8}  {'-' * 24}")
    lines.append(
        f"  {'total job-seconds (compute)'.ljust(width)}  "
        f"{total_seconds:>8}  {total_budget:>8}  {total_basis}"
    )
    queue_note = "incl. queue" if includes_queue else "job span only, queue unknown"
    lines.append(
        f"  {f'run wall clock ({queue_note})'.ljust(width)}  "
        f"{wall_clock_seconds:>8}  {wall_budget:>8}  fixed ceiling"
    )
    lines.append("")

    cache_missed = cache_missed_jobs(timings)
    lines += _render_cache_misses(cache_missed)

    if not breaches:
        lines.append("OK: every job, total compute, and run wall clock within budget.")
        return "\n".join(lines)

    lines.append(
        ("REPORT-ONLY: " if report_only else "BREACH: ")
        + f"{len(breaches)} budget breach(es)"
    )
    for b in breaches:
        lines.append(f"  - {b.message}")
    lines += _breach_guidance(breaches, cache_missed=bool(cache_missed))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assert a CI run stayed inside its documented wall-clock budget."
    )
    parser.add_argument(
        "--jobs-json",
        required=True,
        help="Path to the Actions `/runs/<id>/jobs` payload, or '-' for stdin.",
    )
    parser.add_argument(
        "--run-json",
        help=(
            "Path to the Actions `/runs/<id>` payload. Without it the pre-run "
            "queue wait is unknowable and wall clock falls back to the job span."
        ),
    )
    parser.add_argument(
        "--history-json",
        help=(
            "Path to a run-history payload from "
            "scripts/fetch_ci_wall_clock_history.py. Supplies the rolling "
            "baseline and the concurrent-run contention factor (issue #2615). "
            "Without it every job falls back to its committed fixed ceiling."
        ),
    )
    parser.add_argument(
        "--budget",
        default=str(DEFAULT_BUDGET_FILE),
        help="Path to the budget file (default: .github/ci-wall-clock-budget.json).",
    )
    parser.add_argument(
        "--exclude-job",
        action="append",
        default=[],
        help="Job name to ignore, repeatable; added to the budget's exclude_jobs.",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help=(
            "Report breaches as warnings and exit 0. Used for fork PRs, which "
            "ci.yml routes to ubuntu-latest and whose timings the budgets -- "
            "measured on the self-hosted pool -- do not describe."
        ),
    )
    parser.add_argument(
        "--annotate",
        action="store_true",
        help="Emit GitHub Actions ::error::/::warning:: annotations.",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    try:
        budget = load_budget(args.budget)
        exclude = frozenset(budget.get("exclude_jobs", [])) | frozenset(
            args.exclude_job
        )
        timings, first_start, last_end = measure_jobs(
            load_json(args.jobs_json), budget, exclude=exclude
        )
        started_at = run_start(load_json(args.run_json) if args.run_json else None)
        model = build_model(load_history(args.history_json), budget, exclude=exclude)
        timings = apply_model(timings, model)
    except CannotRun as exc:
        message = f"CI wall-clock budget check could not run: {exc}"
        if args.annotate:
            print(
                "::error file=scripts/check_ci_wall_clock.py,"
                f"title=Wall-clock budget check failed::{message}"
            )
        print(message, file=sys.stderr)
        return EXIT_CANNOT_RUN

    includes_queue = started_at is not None
    # Without --run-json (or a run payload missing both timestamp fields) the
    # pre-run queue wait is unknowable, so this falls back to the earliest job
    # start: wall_clock_seconds below is the job span only and silently
    # excludes whatever time the run spent queued before any job started.
    # `includes_queue` records that so callers (render_text, --format json)
    # can say so rather than presenting a job-span number as the real thing.
    wall_start = started_at if includes_queue else first_start
    wall_clock_seconds = max(0, int((last_end - wall_start).total_seconds()))
    # Compute only: per JobTiming, `seconds` already has each job's
    # cache-miss recovery time (#2617) split off into `cache_miss_seconds`.
    # Wall clock is deliberately NOT adjusted -- it measures the run as it
    # really elapsed, and its breach is report-only anyway.
    total_seconds = sum(t.seconds for t in timings)
    total_cache_miss_seconds = sum(t.cache_miss_seconds for t in timings)

    total_budget, total_basis = total_budget_seconds(budget, model)
    breaches = evaluate(
        timings,
        total_seconds=total_seconds,
        wall_clock_seconds=wall_clock_seconds,
        budget=budget,
        model=model,
        exclude=exclude,
    )

    if args.format == "json":
        payload = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "status": "ok" if not breaches else "breach",
            "report_only": args.report_only,
            "model": {
                "kind": "rolling" if model.active else "fixed",
                "baseline_runs": model.baseline_runs,
                "baselined_jobs": len(model.baselines),
                "pool_factor": round(model.pool_factor, 3),
                "peer_runs": model.peer_runs,
                "same_tree_runs": len(model.same_tree_runs),
            },
            "total_job_seconds": total_seconds,
            "total_job_budget_seconds": total_budget,
            "total_job_budget_basis": total_basis,
            "total_cache_miss_seconds": total_cache_miss_seconds,
            "run_wall_clock_seconds": wall_clock_seconds,
            "run_wall_clock_budget_seconds": budget["run_wall_clock_budget_seconds"],
            "run_wall_clock_includes_queue": includes_queue,
            "jobs": [
                {
                    "name": t.name,
                    "seconds": t.seconds,
                    "budget_seconds": t.budget_seconds,
                    "over_budget": t.over_budget,
                    "basis": t.basis,
                    "baseline_seconds": (
                        None
                        if t.baseline is None
                        else round(t.baseline.median_seconds, 1)
                    ),
                    "baseline_samples": (
                        None if t.baseline is None else t.baseline.samples
                    ),
                    "total_seconds": t.total_seconds,
                    "cache_miss_seconds": t.cache_miss_seconds,
                    "cache_miss_steps": list(t.cache_miss_steps),
                }
                for t in sorted(timings, key=lambda t: -t.seconds)
            ],
            "breaches": [
                {
                    "kind": b.kind,
                    "scope": b.scope,
                    "subject": b.subject,
                    "actual_seconds": b.actual_seconds,
                    "budget_seconds": b.budget_seconds,
                    "basis": b.basis,
                    "confirmed": b.confirmed,
                }
                for b in breaches
            ],
        }
        print(json.dumps(payload, indent=2))
    else:
        report = render_text(
            timings,
            breaches,
            total_seconds=total_seconds,
            total_budget=total_budget,
            wall_clock_seconds=wall_clock_seconds,
            wall_budget=budget["run_wall_clock_budget_seconds"],
            includes_queue=includes_queue,
            report_only=args.report_only,
            model=model,
            total_basis=total_basis,
        )
        print(report)

    if args.annotate:
        # Mirror the exit-code rule per breach, not per run: a queue breach
        # never fails the build (see the `EXIT_BREACH` gate below), so it must
        # never read `::error::` either -- a red annotation on a green job
        # just trains people to stop reading annotations. Same for an
        # UNCONFIRMED compute breach (#2615), which also exits 0. Only a
        # confirmed compute breach on a non-report-only run reddens the build,
        # and only that case gets `::error::`.
        for b in breaches:
            gating = b.kind == "compute" and b.confirmed and not args.report_only
            level = "error" if gating else "warning"
            print(
                f"::{level} file=.github/ci-wall-clock-budget.json,"
                f"title=CI wall-clock budget ({b.kind})::{b.message}"
            )
        # Cache health is a `::notice::`, never an error: a missed cache is
        # nothing the PR author did and nothing they can fix, but it is the
        # first thing a reader of a slow run needs to know (#2617).
        for t in cache_missed_jobs(timings):
            print(
                "::notice file=.github/workflows/ci.yml,"
                f"title=CI cache miss::{t.name}: actions/cache missed, so "
                f"{', '.join(t.cache_miss_steps)} rebuilt from source. That "
                f"{t.cache_miss_seconds}s is excluded from the "
                f"{t.seconds}s compute figure."
            )

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        table = render_text(
            timings,
            breaches,
            total_seconds=total_seconds,
            total_budget=total_budget,
            wall_clock_seconds=wall_clock_seconds,
            wall_budget=budget["run_wall_clock_budget_seconds"],
            includes_queue=includes_queue,
            report_only=args.report_only,
            model=model,
            total_basis=total_basis,
        )
        try:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write("## CI wall-clock budget\n\n```\n" + table + "\n```\n")
        except OSError as exc:  # pragma: no cover - summary is best-effort
            print(f"warning: could not write step summary: {exc}", file=sys.stderr)

    # Only a *confirmed compute* breach is the author's to act on, so only that
    # reddens the build. A queue-only breach is runner-pool contention, and an
    # UNCONFIRMED compute breach is a slow run of code another run of the same
    # commit already measured as fast: both are reported (annotation + step
    # summary) but exit 0, because failing a build for something no PR author
    # can fix is how a red check gets ignored.
    gating = [b for b in breaches if b.kind == "compute" and b.confirmed]
    if gating and not args.report_only:
        return EXIT_BREACH
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
