#!/usr/bin/env python3
"""Per-step duration measurement for the CI wall-clock gate.

Split out of `check_ci_wall_clock.py` (size ratchet, #4751) when checkout
joined cache-miss recovery as excluded time (#5964): both the checker and the
history fetcher must apply the *same* subtraction, and this module is the one
definition they import.

Two step-name conventions are load-bearing here, because GitHub reports only
step *names* in a `/jobs` payload -- there is no `uses:` field to match on:

* **Cache-miss rebuilds are not compute** (issue #2617). `ci.yml`'s test legs
  rebuild pinned Yosys/SymbiYosys/Icarus/Verilator from source when their
  `actions/cache` restore misses, into steps named `"... (cache miss only)"`;
  see the caller's docstring for the outage that motivated it.
* **Checkout is fixed setup, not compute** (issue #5964). A job's wall time
  includes its `actions/checkout` step(s), and on 2026-11-16 one stalled at
  313 s on a single runner while the five sibling jobs of the same run
  checked out in 4-6 s -- reported as a "compute breach" on a job whose real
  compute was ~29 s. That time is now excluded always, not only on a stall.

The exact step names were confirmed from a recorded `/jobs` payload (run
43967976263, 2026-11-17): a bare `- uses: actions/checkout@<sha>` is reported
as `Run actions/checkout@<sha>`, and the action's cleanup pass as
`Post Run actions/checkout@<sha>` (0-1 s normally, matched for symmetry with
the `Post Cache ` prefix). A checkout step given an explicit `name:` in the
workflow is reported under that name and is *not* matched -- as of this
writing `ci.yml` names only the budget job's own two checkouts, which are not
compute jobs.

Everything is standard library on purpose, like the gate this serves.
"""

from __future__ import annotations

from datetime import datetime

# Conclusions that carry no meaningful duration.
UNMEASURABLE_CONCLUSIONS = frozenset({"skipped", "cancelled", "neutral"})

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

# GitHub's auto-generated names for the two halves of a `uses:
# actions/checkout@<sha>` step (#5964). Load-bearing the same way: a workflow
# rename away from the bare `- uses:` form must be mirrored here.
CHECKOUT_STEP_PREFIXES = (
    "Run actions/checkout",
    "Post Run actions/checkout",
)


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


def _parse_iso(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


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
            if step.get("conclusion") in UNMEASURABLE_CONCLUSIONS:
                continue
            rebuilds.append((name, _step_seconds(step)))
        elif name.startswith(CACHE_STEP_PREFIXES):
            cache_io_seconds += _step_seconds(step)

    if not rebuilds:
        return 0, ()
    seconds = sum(step_seconds for _, step_seconds in rebuilds) + cache_io_seconds
    return seconds, tuple(name for name, _ in rebuilds)


def measure_checkout(job: dict) -> tuple[int, tuple[str, ...]]:
    """Seconds this job spent in `actions/checkout` steps (#5964).

    Returns `(seconds, checkout_step_names)`; `(0, ())` for a job with no
    running checkout step or no `steps[]` at all (degrade to "subtract
    nothing", like every step measurement here).

    Unlike the cache miss, this excludes *always*, not only when something
    went wrong: checkout is fixed git/setup time by construction -- the same
    commit, action pin and repo size on every job of a run -- so it says
    nothing about the code under test however long it took. A normal
    checkout is 3-10 s; the caller decides when an excluded figure is large
    enough to be worth naming (the checker reports a stall above 60 s).
    """
    steps = job.get("steps")
    if not isinstance(steps, list):
        return 0, ()

    checkouts: list[tuple[str, int]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        name = step.get("name")
        if not isinstance(name, str):
            continue
        if name.startswith(CHECKOUT_STEP_PREFIXES):
            if step.get("conclusion") in UNMEASURABLE_CONCLUSIONS:
                continue
            checkouts.append((name, _step_seconds(step)))

    if not checkouts:
        return 0, ()
    seconds = sum(step_seconds for _, step_seconds in checkouts)
    return seconds, tuple(name for name, _ in checkouts)
