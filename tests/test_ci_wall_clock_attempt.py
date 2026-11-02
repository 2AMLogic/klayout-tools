"""Tests for the current-attempt row selection in `scripts/check_ci_wall_clock.py`
(issue #4751). Split from `test_ci_wall_clock.py` to respect the file-size ratchet.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from test_ci_wall_clock import (
    EXIT_BREACH,
    EXIT_OK,
    _budget,
    _job,
    _jobs,
    _run,
    _write,
)

# --------------------------------------------------------------------------
# Stale-attempt rows (issue #4751)
# --------------------------------------------------------------------------


def _attempt_job(name: str, attempt: int, started: str, completed: str) -> dict:
    job = _job(name, started=started, completed=completed)
    job["run_attempt"] = attempt
    return job


def _attempt_run(tmp_path: Path, attempt: int, run_started_at: str) -> Path:
    return _write(
        tmp_path / "run.json",
        {"run_attempt": attempt, "run_started_at": run_started_at},
    )


def test_a_full_rerun_ignores_earlier_attempts_rows(tmp_path: Path) -> None:
    jobs = _jobs(
        tmp_path,
        [
            # attempt 1: way over its 360 s budget (pool contention)
            _attempt_job("Slow job", 1, "2026-09-17T06:00:00Z", "2026-09-17T07:00:00Z"),
            # attempt 3: inside budget
            _attempt_job("Slow job", 3, "2026-09-17T09:00:05Z", "2026-09-17T09:03:05Z"),
        ],
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        "--run-json",
        str(_attempt_run(tmp_path, 3, "2026-09-17T09:00:00Z")),
        "--budget",
        str(_budget(tmp_path)),
        "--format",
        "json",
    )
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert [j["seconds"] for j in payload["jobs"]] == [180]
    assert payload["total_job_seconds"] == 180
    assert payload["attempt"]["stale_attempt_rows_dropped"] == 1
    assert payload["attempt"]["nothing_new_measured"] is False


def test_the_text_report_counts_dropped_stale_rows(tmp_path: Path) -> None:
    jobs = _jobs(
        tmp_path,
        [
            _attempt_job("Slow job", 1, "2026-09-17T06:00:00Z", "2026-09-17T07:00:00Z"),
            _attempt_job("Slow job", 2, "2026-09-17T09:00:05Z", "2026-09-17T09:03:05Z"),
        ],
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        "--run-json",
        str(_attempt_run(tmp_path, 2, "2026-09-17T09:00:00Z")),
        "--budget",
        str(_budget(tmp_path)),
    )
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert "1 row(s) from earlier attempts dropped" in result.stdout


def test_a_failed_only_rerun_of_the_budget_job_says_nothing_new_was_measured(
    tmp_path: Path,
) -> None:
    # Attempt 2 re-ran only the budget job: the other rows carry
    # run_attempt=2 but their attempt-1 timestamps.
    jobs = _jobs(
        tmp_path,
        [
            _attempt_job("Slow job", 2, "2026-09-17T06:00:00Z", "2026-09-17T07:00:00Z"),
            _attempt_job(
                "CI wall-clock budget",
                2,
                "2026-09-17T09:00:05Z",
                "2026-09-17T09:00:50Z",
            ),
        ],
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        "--run-json",
        str(_attempt_run(tmp_path, 2, "2026-09-17T09:00:00Z")),
        "--budget",
        str(_budget(tmp_path)),
    )
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr
    assert "NOTHING NEW WAS MEASURED" in result.stdout


def test_without_an_attempt_nothing_is_filtered(tmp_path: Path) -> None:
    jobs = _jobs(
        tmp_path,
        [
            _attempt_job("Slow job", 1, "2026-09-17T06:00:00Z", "2026-09-17T07:00:00Z"),
        ],
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("GITHUB_STEP_SUMMARY", "GITHUB_RUN_ATTEMPT")
    }
    result = _run("--jobs-json", str(jobs), "--budget", str(_budget(tmp_path)), env=env)
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr
    assert "earlier attempts" not in result.stdout


def test_github_run_attempt_env_is_the_fallback(tmp_path: Path) -> None:
    jobs = _jobs(
        tmp_path,
        [
            _attempt_job("Slow job", 1, "2026-09-17T06:00:00Z", "2026-09-17T07:00:00Z"),
            _attempt_job("Slow job", 2, "2026-09-17T09:00:05Z", "2026-09-17T09:03:05Z"),
        ],
    )
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"} | {
        "GITHUB_RUN_ATTEMPT": "2"
    }
    result = _run("--jobs-json", str(jobs), "--budget", str(_budget(tmp_path)), env=env)
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
