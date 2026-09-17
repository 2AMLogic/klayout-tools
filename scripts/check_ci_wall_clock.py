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

Exit codes (mirroring `scripts/check-release-lag.sh`'s tiering):

  0  within budget, or a `--report-only` run
  1  budget breach
  2  the check itself could not run (unreadable/incoherent budget file, or a
     payload with nothing measurable in it) -- a check that silently stops
     checking is worse than no check at all, so this is never a green.

Usage:

    gh api "repos/$REPO/actions/runs/$RUN_ID/jobs?per_page=100" > jobs.json
    gh api "repos/$REPO/actions/runs/$RUN_ID"                   > run.json
    python3 scripts/check_ci_wall_clock.py --jobs-json jobs.json --run-json run.json

Everything is standard library on purpose: this job must be fast and must not
need `uv sync`, or the budget check becomes part of the problem it measures.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_BUDGET_FILE = (
    Path(__file__).resolve().parent.parent / ".github" / "ci-wall-clock-budget.json"
)

EXIT_OK = 0
EXIT_BREACH = 1
EXIT_CANNOT_RUN = 2

# Conclusions that carry no meaningful duration.
_UNMEASURABLE_CONCLUSIONS = frozenset({"skipped", "cancelled", "neutral"})


class CannotRun(Exception):
    """The check could not be performed (exit 2, never a silent pass)."""


@dataclass(frozen=True)
class JobTiming:
    name: str
    seconds: int
    budget_seconds: int

    @property
    def over_budget(self) -> bool:
        return self.seconds > self.budget_seconds


@dataclass(frozen=True)
class Breach:
    kind: str  # "compute" | "queue"
    scope: str  # "job" | "total" | "run"
    subject: str
    actual_seconds: int
    budget_seconds: int

    @property
    def message(self) -> str:
        over = self.actual_seconds - self.budget_seconds
        return (
            f"{self.subject}: {self.actual_seconds}s exceeds the "
            f"{self.budget_seconds}s budget by {over}s ({self.kind} breach)"
        )


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
    if version != SCHEMA_VERSION:
        raise CannotRun(
            f"budget file {path} has schema_version {version!r}, "
            f"expected {SCHEMA_VERSION}"
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
    return budget


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


def measure_jobs(
    payload: object, budget: dict, *, exclude: frozenset[str]
) -> tuple[list[JobTiming], datetime, datetime]:
    """Per-job durations plus the earliest start / latest finish across them.

    Jobs still running (null `completed_at` -- which always includes the budget
    job itself, since it measures the run it belongs to) and jobs whose
    conclusion carries no duration are dropped.
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
        timings.append(JobTiming(name, seconds, job_budget))
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


def evaluate(
    timings: list[JobTiming],
    *,
    total_seconds: int,
    wall_clock_seconds: int,
    budget: dict,
) -> list[Breach]:
    """Breaches, each classified `compute` or `queue`.

    A per-job or total-compute overrun is a `compute` breach -- the code under
    test got slower. A wall-clock overrun is only reported when compute itself
    is *within* budget, in which case the extra time is queue/scheduling and is
    classified `queue`. Reporting the same slowdown twice (once as compute,
    once as wall clock) would just bury the actionable line.
    """
    breaches = [
        Breach("compute", "job", t.name, t.seconds, t.budget_seconds)
        for t in timings
        if t.over_budget
    ]
    total_budget = budget["total_job_budget_seconds"]
    compute_over = total_seconds > total_budget
    if compute_over:
        breaches.append(
            Breach("compute", "total", "total job-seconds", total_seconds, total_budget)
        )

    wall_budget = budget["run_wall_clock_budget_seconds"]
    if wall_clock_seconds > wall_budget and not compute_over:
        breaches.append(
            Breach("queue", "run", "run wall clock", wall_clock_seconds, wall_budget)
        )
    return breaches


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
) -> str:
    width = max([len(t.name) for t in timings] + [len("total job-seconds (compute)")])
    lines = [
        f"CI wall-clock budget -- {len(timings)} job(s) measured",
        "",
        f"  {'job'.ljust(width)}  {'actual':>8}  {'budget':>8}",
        f"  {'-' * width}  {'-' * 8}  {'-' * 8}",
    ]
    for t in sorted(timings, key=lambda t: -t.seconds):
        flag = "  OVER" if t.over_budget else ""
        lines.append(
            f"  {t.name.ljust(width)}  {t.seconds:>8}  {t.budget_seconds:>8}{flag}"
        )
    lines.append(f"  {'-' * width}  {'-' * 8}  {'-' * 8}")
    lines.append(
        f"  {'total job-seconds (compute)'.ljust(width)}  "
        f"{total_seconds:>8}  {total_budget:>8}"
    )
    queue_note = "incl. queue" if includes_queue else "job span only, queue unknown"
    lines.append(
        f"  {f'run wall clock ({queue_note})'.ljust(width)}  "
        f"{wall_clock_seconds:>8}  {wall_budget:>8}"
    )
    lines.append("")

    if not breaches:
        lines.append("OK: every job, total compute, and run wall clock within budget.")
    else:
        kinds = {b.kind for b in breaches}
        lines.append(
            ("REPORT-ONLY: " if report_only else "BREACH: ")
            + f"{len(breaches)} budget breach(es)"
        )
        for b in breaches:
            lines.append(f"  - {b.message}")
        if kinds == {"queue"}:
            lines += [
                "",
                "Compute is WITHIN budget -- this is a scheduling/queue breach, not a",
                "code regression. Optimising these jobs would achieve nothing (issue",
                "#1971): look at runner-pool contention instead.",
            ]
        elif "compute" in kinds:
            lines += [
                "",
                "This is a COMPUTE breach: the work itself grew. Either shrink the",
                "job(s) above or, if the new cost is justified, raise the budget in",
                ".github/ci-wall-clock-budget.json in the same PR and say why.",
            ]
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
    wall_start = started_at if includes_queue else first_start
    wall_clock_seconds = max(0, int((last_end - wall_start).total_seconds()))
    total_seconds = sum(t.seconds for t in timings)

    breaches = evaluate(
        timings,
        total_seconds=total_seconds,
        wall_clock_seconds=wall_clock_seconds,
        budget=budget,
    )

    if args.format == "json":
        payload = {
            "schema_version": SCHEMA_VERSION,
            "status": "ok" if not breaches else "breach",
            "report_only": args.report_only,
            "total_job_seconds": total_seconds,
            "total_job_budget_seconds": budget["total_job_budget_seconds"],
            "run_wall_clock_seconds": wall_clock_seconds,
            "run_wall_clock_budget_seconds": budget["run_wall_clock_budget_seconds"],
            "run_wall_clock_includes_queue": includes_queue,
            "jobs": [
                {
                    "name": t.name,
                    "seconds": t.seconds,
                    "budget_seconds": t.budget_seconds,
                    "over_budget": t.over_budget,
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
            total_budget=budget["total_job_budget_seconds"],
            wall_clock_seconds=wall_clock_seconds,
            wall_budget=budget["run_wall_clock_budget_seconds"],
            includes_queue=includes_queue,
            report_only=args.report_only,
        )
        print(report)

    if args.annotate:
        level = "warning" if args.report_only else "error"
        for b in breaches:
            print(
                f"::{level} file=.github/ci-wall-clock-budget.json,"
                f"title=CI wall-clock budget ({b.kind})::{b.message}"
            )

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        table = render_text(
            timings,
            breaches,
            total_seconds=total_seconds,
            total_budget=budget["total_job_budget_seconds"],
            wall_clock_seconds=wall_clock_seconds,
            wall_budget=budget["run_wall_clock_budget_seconds"],
            includes_queue=includes_queue,
            report_only=args.report_only,
        )
        try:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write("## CI wall-clock budget\n\n```\n" + table + "\n```\n")
        except OSError as exc:  # pragma: no cover - summary is best-effort
            print(f"warning: could not write step summary: {exc}", file=sys.stderr)

    if breaches and not args.report_only:
        return EXIT_BREACH
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
