#!/usr/bin/env python3
"""Collect the run history `scripts/check_ci_wall_clock.py` compares against.

Issue #2615 replaced the wall-clock gate's hand-committed per-job ceilings
with a cross-run model: a rolling baseline from recent baseline-branch runs,
plus a contention factor measured from runs that were on the pool at the same
time. That model needs history, and the decision recorded here -- the one the
issue flagged as its main open design question -- is **where the history comes
from**:

    Live `gh api` reads at check time, not a committed or cached artifact.

Why:

* **It is always current, and never a merge conflict.** A committed rolling
  artifact would be rewritten by (and conflict between) every PR that touched
  it, and would need a bot push on every baseline-branch run to stay fresh --
  re-introducing, in a new shape, exactly the "somebody has to maintain the
  number" failure mode #2406/#2612 documented.
* **The concurrency evidence only exists live.** The pool-contention factor is
  measured from runs that overlap *this* run's window. Those runs are minutes
  old and frequently still in flight; no cache written by a previous run can
  contain them.
* **The permission already exists.** The `CI wall-clock budget` job in
  `ci.yml` already grants `actions: read` and already calls `gh api` twice for
  its own run. This adds ~20 more reads of the same kind, bounded by
  `--max-api-calls`.
* **The cost is bounded and small.** Two list calls plus one `/jobs` call per
  selected run, on a job that deliberately runs on `ubuntu-latest` precisely
  so it is not itself queued behind the pool it measures.

The trade-off accepted: the check now depends on the Actions API being
reachable. That is why **this script never fails**. Any error -- missing `gh`,
a 403, a rate limit, a timeout -- produces a valid, empty history payload and
exit 0, and the check then falls back to the committed ceilings, which is the
pre-#2615 behaviour rather than a broken build.

Output payload (`schema_version` 1):

    {
      "schema_version": 1,
      "generated_at": "2026-09-29T21:00:00+00:00",
      "run_id": 36620846383,
      "head_sha": "e3270d98...",
      "error": null,
      "runs": [
        {
          "run_id": 36611724105,
          "head_sha": "ee8fc812...",
          "head_branch": "main",
          "roles": ["baseline"],
          "jobs": {"Tests (Python 3.10)": 422, ...},
          "partial_jobs": []
        }
      ]
    }

`roles` is how the check reads an entry:

* `baseline` -- a completed run on the baseline branch, on a different commit;
  feeds the rolling median/MAD.
* `peer`     -- a run on a different commit whose window overlapped this one's;
  feeds the pool-contention factor. May still be in flight, in which case its
  running jobs appear in `partial_jobs` with their elapsed-so-far seconds,
  which are a lower bound on the real duration.
* `same-tree` -- another run of *this* commit (a re-run, or a push and a PR
  run of the same SHA); lets the check mark a breach UNCONFIRMED when the same
  job was fast on identical code.

Standard library only, like the check it feeds.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HISTORY_SCHEMA_VERSION = 1
DEFAULT_BUDGET_FILE = (
    Path(__file__).resolve().parent.parent / ".github" / "ci-wall-clock-budget.json"
)
DEFAULT_WORKFLOW = "ci.yml"
_UNMEASURABLE_CONCLUSIONS = frozenset({"skipped", "cancelled", "neutral"})
# Conclusions whose timings still describe the pool honestly. `cancelled` is
# excluded: a cancelled run's jobs were cut short, so they read as fast.
_USABLE_RUN_CONCLUSIONS = frozenset({"success", "failure"})

# Defaults for what to collect; overridable from the budget file's
# `rolling_window` block and then from the command line.
DEFAULTS = {
    "baseline_branch": "main",
    "baseline_runs": 15,
    "peer_window_runs": 40,
    "min_peer_overlap_seconds": 120,
}


class FetchError(Exception):
    """Any reason the history could not be collected (never fatal)."""


def _parse_iso(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


class GhClient:
    """A counted, timeout-bounded `gh api` caller."""

    def __init__(self, gh: str, timeout: int, max_calls: int) -> None:
        self.gh = gh
        self.timeout = timeout
        self.max_calls = max_calls
        self.calls = 0

    def get(self, path: str) -> object:
        if self.calls >= self.max_calls:
            raise FetchError(f"API call budget of {self.max_calls} exhausted")
        self.calls += 1
        try:
            proc = subprocess.run(
                [self.gh, "api", "-H", "Accept: application/vnd.github+json", path],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FetchError(f"`{self.gh} api {path}` failed: {exc}") from exc
        if proc.returncode != 0:
            raise FetchError(
                f"`{self.gh} api {path}` exited {proc.returncode}: "
                f"{proc.stderr.strip()[:200]}"
            )
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise FetchError(
                f"`{self.gh} api {path}` returned non-JSON: {exc}"
            ) from exc


def job_durations(
    payload: object, *, now: datetime
) -> tuple[dict[str, int], list[str]]:
    """Job name -> seconds, plus the names whose seconds are a lower bound."""
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    durations: dict[str, int] = {}
    partial: list[str] = []
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict):
            continue
        name, started = job.get("name"), job.get("started_at")
        if not isinstance(name, str) or not isinstance(started, str) or not started:
            continue
        if job.get("conclusion") in _UNMEASURABLE_CONCLUSIONS:
            continue
        completed = job.get("completed_at")
        try:
            start = _parse_iso(started)
            if isinstance(completed, str) and completed:
                seconds = int((_parse_iso(completed) - start).total_seconds())
                is_partial = False
            else:
                # Still running: elapsed-so-far is a lower bound on how long
                # the pool is taking to get through it.
                seconds = int((now - start).total_seconds())
                is_partial = True
        except (ValueError, TypeError):
            continue
        if seconds < 0:
            continue
        # A matrix leg can appear twice across re-run attempts; keep the last.
        durations[name] = seconds
        if is_partial:
            partial.append(name)
        elif name in partial:
            partial.remove(name)
    return durations, partial


def _overlap_seconds(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> float:
    return (min(a_end, b_end) - max(a_start, b_start)).total_seconds()


def _run_window(run: dict, *, now: datetime) -> tuple[datetime, datetime] | None:
    started = run.get("run_started_at") or run.get("created_at")
    if not isinstance(started, str) or not started:
        return None
    try:
        start = _parse_iso(started)
    except ValueError:
        return None
    if run.get("status") != "completed":
        return start, now
    updated = run.get("updated_at")
    try:
        end = _parse_iso(updated) if isinstance(updated, str) and updated else now
    except ValueError:
        end = now
    return start, max(end, start)


class _Selection:
    """Accumulates run -> roles, ignoring the current run and anything the
    payload did not give an integer id."""

    def __init__(self, current_id: object) -> None:
        self.current_id = current_id
        self.runs: dict[int, dict] = {}
        self.roles: dict[int, set[str]] = {}

    def mark(self, run: dict, role: str) -> None:
        run_id = run.get("id")
        if not isinstance(run_id, int) or run_id == self.current_id:
            return
        self.runs.setdefault(run_id, run)
        self.roles.setdefault(run_id, set()).add(role)

    def count(self, role: str) -> int:
        return sum(1 for roles in self.roles.values() if role in roles)

    def items(self) -> list[tuple[dict, set[str]]]:
        return [(self.runs[rid], self.roles[rid]) for rid in self.runs]


def _add_baselines(
    selection: _Selection, runs: list, current_sha: object, limit: int
) -> None:
    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("head_sha") == current_sha:
            selection.mark(run, "same-tree")
        elif (
            run.get("conclusion") in _USABLE_RUN_CONCLUSIONS
            and selection.count("baseline") < limit
        ):
            selection.mark(run, "baseline")


def _add_peers(
    selection: _Selection,
    runs: list,
    current_sha: object,
    window: tuple[datetime, datetime] | None,
    min_overlap: float,
    *,
    now: datetime,
) -> None:
    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("head_sha") == current_sha:
            selection.mark(run, "same-tree")
            continue
        peer_window = _run_window(run, now=now) if window else None
        if peer_window is None:
            continue
        if _overlap_seconds(*window, *peer_window) >= min_overlap:
            selection.mark(run, "peer")


def select_runs(
    current: dict, baseline_list: list, peer_list: list, opts: dict, *, now: datetime
) -> list[tuple[dict, set[str]]]:
    """Which runs to fetch job timings for, and why (their roles)."""
    selection = _Selection(current.get("id"))
    current_sha = current.get("head_sha")
    _add_baselines(selection, baseline_list, current_sha, opts["baseline_runs"])
    _add_peers(
        selection,
        peer_list,
        current_sha,
        _run_window(current, now=now),
        opts["min_peer_overlap_seconds"],
        now=now,
    )
    return selection.items()


def collect(client: GhClient, repo: str, run_id: str, opts: dict) -> dict:
    now = datetime.now(timezone.utc)
    current = client.get(f"repos/{repo}/actions/runs/{run_id}")
    if not isinstance(current, dict):
        raise FetchError("current run payload is not an object")

    workflow = opts["workflow"]
    branch = opts["baseline_branch"]
    baseline_payload = client.get(
        f"repos/{repo}/actions/workflows/{workflow}/runs"
        f"?branch={branch}&status=completed&per_page={opts['baseline_runs'] + 5}"
    )
    peer_payload = client.get(
        f"repos/{repo}/actions/workflows/{workflow}/runs"
        f"?per_page={opts['peer_window_runs']}"
    )

    def runs_of(payload: object) -> list:
        if isinstance(payload, dict) and isinstance(payload.get("workflow_runs"), list):
            return payload["workflow_runs"]
        return []

    selected = select_runs(
        current, runs_of(baseline_payload), runs_of(peer_payload), opts, now=now
    )

    entries = []
    for run, roles in selected:
        try:
            payload = client.get(
                f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100"
            )
        except FetchError:
            # One unreadable run is not a reason to abandon the whole window;
            # the model is robust to a smaller sample.
            continue
        durations, partial = job_durations(payload, now=now)
        if not durations:
            continue
        entries.append(
            {
                "run_id": run["id"],
                "head_sha": run.get("head_sha"),
                "head_branch": run.get("head_branch"),
                "roles": sorted(roles),
                "jobs": durations,
                "partial_jobs": sorted(partial),
            }
        )

    return {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "run_id": current.get("id"),
        "head_sha": current.get("head_sha"),
        "error": None,
        "runs": entries,
    }


def budget_options(path: str) -> dict:
    opts = dict(DEFAULTS)
    try:
        budget = json.loads(Path(path).read_text())
        rolling = budget.get("rolling_window", {})
        if isinstance(rolling, dict):
            opts.update({k: v for k, v in rolling.items() if k in DEFAULTS})
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return opts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect the run history the CI wall-clock budget check "
        "compares against."
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--run-id", required=True, help="the run being checked")
    parser.add_argument("--out", required=True, help="where to write the payload")
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    parser.add_argument("--budget", default=str(DEFAULT_BUDGET_FILE))
    parser.add_argument("--baseline-branch")
    parser.add_argument("--baseline-runs", type=int)
    parser.add_argument("--peer-window-runs", type=int)
    parser.add_argument("--min-peer-overlap-seconds", type=int)
    parser.add_argument("--gh", default="gh", help="the gh executable to call")
    parser.add_argument("--timeout", type=int, default=30, help="seconds per API call")
    parser.add_argument("--max-api-calls", type=int, default=40)
    args = parser.parse_args(argv)

    opts = budget_options(args.budget)
    opts["workflow"] = args.workflow
    for key in ("baseline_branch", "baseline_runs", "peer_window_runs"):
        value = getattr(args, key)
        if value is not None:
            opts[key] = value
    if args.min_peer_overlap_seconds is not None:
        opts["min_peer_overlap_seconds"] = args.min_peer_overlap_seconds

    client = GhClient(args.gh, args.timeout, args.max_api_calls)
    try:
        payload = collect(client, args.repo, args.run_id, opts)
    except FetchError as exc:
        # Never fatal: an empty history is a supported mode of the check, and
        # reddening CI because the Actions API hiccuped would be the same
        # "gate nobody trusts" failure #2615 is about.
        payload = {
            "schema_version": HISTORY_SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "run_id": None,
            "head_sha": None,
            "error": str(exc),
            "runs": [],
        }
        print(f"warning: history unavailable, falling back to fixed ceilings: {exc}")

    try:
        Path(args.out).write_text(json.dumps(payload, indent=2))
    except OSError as exc:
        print(f"warning: could not write {args.out}: {exc}", file=sys.stderr)
        return 0
    print(
        f"history: {len(payload['runs'])} run(s) in {client.calls} API call(s) "
        f"-> {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
