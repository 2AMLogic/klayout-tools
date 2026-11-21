#!/usr/bin/env python3
"""Fetch this run's own timings for the CI wall-clock budget check.

The budget job used to read its own run's `/jobs` and `/runs/<id>` payloads
with two bare `gh api` calls redirected into `/tmp/ci-jobs.json` and
`/tmp/ci-run.json` -- one attempt per endpoint, no retry. A single
transient API failure, the exact #6214 failure the *history* fetch already
learned to survive, exited the step before the budget check could run.
Current-run timings are the one input that check has no fallback for
(history degrades to committed ceilings; the run's own timings ARE the
measurement), so #6439 gives these two reads the bounded retry #6311's
`GhClient` already provides: transient failures (5xx, timeout, dropped
connection) are retried up to `RETRY_ATTEMPTS` times each; permanent ones
(4xx, non-JSON, a missing `gh`) raise on the first attempt.

The API-call budget is `2 * RETRY_ATTEMPTS`: every retry consumes a call,
so the step stays bounded however often the API hiccups, and two endpoints
that each survive to their final allowed attempt exactly spend it.

Unlike the history fetcher, this script IS allowed to fail the step. Both
payloads are fetched before either file is written, so a failed second
read leaves no half-written pair for the checker; then any `FetchError` is
reported on stderr naming the endpoint, and the exit is nonzero -- the
workflow's check step does not run. That is the honest outcome: a budget
check that cannot see the run it is judging should not judge it.

Standard library only, like the scripts it serves.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ci_wall_clock_gh import RETRY_ATTEMPTS, FetchError, GhClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch this run's job and run timings for the CI "
        "wall-clock budget check."
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--run-id", required=True, help="the run being checked")
    parser.add_argument(
        "--jobs-out", required=True, help="where to write the /jobs payload"
    )
    parser.add_argument(
        "--run-out", required=True, help="where to write the run-metadata payload"
    )
    parser.add_argument("--gh", default="gh", help="the gh executable to call")
    parser.add_argument("--timeout", type=int, default=30, help="seconds per API call")
    parser.add_argument(
        "--retry-sleep",
        type=float,
        default=2.0,
        help="seconds before the first transient-failure retry, growing "
        "geometrically (2 s then 5 s by default; 0 waits no time between "
        "retries but still makes them)",
    )
    args = parser.parse_args(argv)

    # (label, endpoint, output): the jobs list keeps its `?per_page=100`
    # page and the run metadata its bare path, exactly as the two `gh api`
    # calls this replaces spelled them.
    endpoints = (
        (
            "jobs",
            f"repos/{args.repo}/actions/runs/{args.run_id}/jobs?per_page=100",
            args.jobs_out,
        ),
        (
            "run",
            f"repos/{args.repo}/actions/runs/{args.run_id}",
            args.run_out,
        ),
    )

    # Two required reads x RETRY_ATTEMPTS worst-case attempts each, every
    # retry charged to the same budget (#6439).
    client = GhClient(
        args.gh, args.timeout, 2 * RETRY_ATTEMPTS, retry_sleep=args.retry_sleep
    )

    fetched: dict[str, object] = {}
    for label, path, _out in endpoints:
        try:
            fetched[label] = client.get(path)
        except FetchError as exc:
            print(f"error: {label} endpoint ({path}) failed: {exc}", file=sys.stderr)
            return 1

    # Both payloads in hand before either file exists: a failed second read
    # must not leave a half-written pair for the checker.
    for label, _path, out in endpoints:
        try:
            Path(out).write_text(json.dumps(fetched[label], indent=2) + "\n")
        except OSError as exc:
            print(f"error: could not write {out}: {exc}", file=sys.stderr)
            return 1
    print(
        f"timings: {len(endpoints)} payload(s) in {client.calls} API call(s) "
        f"-> {args.jobs_out}, {args.run_out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
