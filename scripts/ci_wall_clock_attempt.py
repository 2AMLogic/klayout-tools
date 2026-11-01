"""Pick the `/jobs` rows that belong to a run's current attempt (#4751).

Split out of `check_ci_wall_clock.py` so that file stays under its size ratchet
ceiling (see `scripts/check_file_size_baseline.py`). Pure functions over
already-extracted job dicts; no dependency on the budget check itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime


def parse_iso(stamp: str) -> datetime:
    # `fromisoformat` only learned `Z` in 3.11 and this repo targets 3.10.
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


# A carried-over job (a `--failed` rerun keeps the earlier attempt's passing
# jobs in the payload, restamped with the new `run_attempt` but with their old
# timestamps) can only be told apart from a fresh one by having started before
# the attempt did. Allow a little clock skew between the two API fields.
_ATTEMPT_START_SKEW_SECONDS = 2


@dataclass(frozen=True)
class AttemptSelection:
    """Which `/jobs` rows belong to the run's current attempt (#4751).

    `filter=latest` is not "the current attempt": rows of earlier attempts stay
    in the payload next to the fresh ones, and measuring them makes a breach
    that a full rerun can never clear.
    """

    attempt: int | None
    current: list[dict]
    stale: list[dict]  # `run_attempt` differs from the run's own attempt
    carried: list[dict]  # tagged current, but started before this attempt did
    nothing_new: bool = False  # only carried-over rows could be measured

    def note(self) -> str | None:
        if self.attempt is None:
            return None
        parts = []
        if self.stale:
            parts.append(
                f"{len(self.stale)} row(s) from earlier attempts dropped, not measured"
            )
        if self.nothing_new:
            parts.append(
                f"NOTHING NEW WAS MEASURED: every measurable job was carried "
                f"over from before attempt {self.attempt} began (a --failed "
                f"rerun of this job alone), so these are the earlier "
                f"attempt's timings re-read, not a fresh sample"
            )
        elif self.carried:
            parts.append(
                f"{len(self.carried)} job(s) carried over unchanged from an "
                f"earlier attempt dropped, not measured"
            )
        return "; ".join(parts) or None


def current_attempt(run_payload: object | None) -> int | None:
    """The run's own attempt: the run JSON's `run_attempt`, else the env var."""
    candidates = []
    if isinstance(run_payload, dict):
        candidates.append(run_payload.get("run_attempt"))
    candidates.append(os.environ.get("GITHUB_RUN_ATTEMPT"))
    for value in candidates:
        try:
            attempt = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if attempt > 0:
            return attempt
    return None


def select_attempt(
    jobs: list[dict], attempt: int | None, attempt_started: datetime | None
) -> AttemptSelection:
    """Split the payload into current-attempt rows and the rest.

    Fails open: with no known attempt, or a job lacking `run_attempt`, the job
    is kept, which is the pre-#4751 behaviour.
    """
    if attempt is None:
        return AttemptSelection(None, jobs, [], [])
    current: list[dict] = []
    stale: list[dict] = []
    carried: list[dict] = []
    for job in jobs:
        job_attempt = job.get("run_attempt") if isinstance(job, dict) else None
        if isinstance(job_attempt, int) and job_attempt != attempt:
            stale.append(job)
            continue
        started = job.get("started_at") if isinstance(job, dict) else None
        if attempt_started is not None and isinstance(started, str) and started:
            try:
                begun = parse_iso(started)
            except ValueError:
                begun = None  # measure_jobs reports the unparseable stamp
            if begun is not None and (
                (attempt_started - begun).total_seconds() > _ATTEMPT_START_SKEW_SECONDS
            ):
                carried.append(job)
                continue
        current.append(job)
    return AttemptSelection(attempt, current, stale, carried)


def as_json(sel: AttemptSelection) -> dict:
    return {
        "run_attempt": sel.attempt,
        "stale_attempt_rows_dropped": len(sel.stale),
        "carried_over_rows_dropped": 0 if sel.nothing_new else len(sel.carried),
        "nothing_new_measured": sel.nothing_new,
    }
