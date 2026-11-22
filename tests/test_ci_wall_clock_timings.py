"""`scripts/fetch_ci_wall_clock_timings.py` (#6439): the two current-run
reads the budget check requires, given the bounded retry the history fetch
already had (#6214/#6311).

The step this replaces made two bare `gh api` calls -- one attempt per
endpoint, no retry -- so a single transient failure exited the step before
the budget check could run. Unlike the history fetch (optional; falls back
to committed ceilings), these payloads ARE the measurement, so the contract
here is the opposite on failure: report the endpoint, exit nonzero, write
neither output, and let the workflow skip the check.

All against fake `gh` executables -- never a live Actions API -- and the
final test feeds this reader's output through the real
`scripts/check_ci_wall_clock.py`, pinning that the payloads it writes are
the ones the check reads (same files the old redirects produced).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
READER = REPO_ROOT / "scripts" / "fetch_ci_wall_clock_timings.py"
CHECKER = REPO_ROOT / "scripts" / "check_ci_wall_clock.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from ci_wall_clock_gh import RETRY_ATTEMPTS  # noqa: E402

SERVER_ERROR_502 = "gh: Server Error (HTTP 502)"
NOT_FOUND_404 = "gh: Not Found (HTTP 404)"
REPO = "owner/name"
JOBS_PATH = f"repos/{REPO}/actions/runs/900/jobs?per_page=100"
RUN_PATH = f"repos/{REPO}/actions/runs/900"


def _fake_gh(
    tmp_path: Path,
    responses: dict[str, object],
    *,
    flaky: dict[str, int] | None = None,
    hard: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    """A fake `gh api` answering from a JSON map, plus the log of every
    path it was asked for. Paths in `flaky` fail their first N calls with
    the 502 gh printed in run 44058611659's log (#6214) and then answer
    from the map; paths in `hard` fail every call with their stderr."""
    flaky = flaky or {}
    hard = hard or {}
    table = tmp_path / "responses.json"
    table.write_text(json.dumps(responses))
    state = tmp_path / "flaky.json"
    state.write_text(json.dumps(flaky))
    errors = tmp_path / "hard.json"
    errors.write_text(json.dumps(hard))
    log = tmp_path / "calls.log"
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"table = json.loads(open({str(table)!r}).read())\n"
        f"state = json.loads(open({str(state)!r}).read())\n"
        f"errors = json.loads(open({str(errors)!r}).read())\n"
        f"log = {str(log)!r}\n"
        "path = sys.argv[-1]\n"
        "open(log, 'a').write(path + '\\n')\n"
        "if path in errors:\n"
        "    sys.stderr.write(errors[path])\n"
        "    sys.exit(1)\n"
        "left = state.get(path, 0)\n"
        "if left > 0:\n"
        "    state[path] = left - 1\n"
        f"    open({str(state)!r}, 'w').write(json.dumps(state))\n"
        f"    sys.stderr.write({SERVER_ERROR_502!r})\n"
        "    sys.exit(1)\n"
        "if path not in table:\n"
        "    sys.stderr.write('no such path: ' + path)\n"
        "    sys.exit(1)\n"
        "print(json.dumps(table[path]))\n"
    )
    gh.chmod(0o755)
    return gh, log


def _run_reader(
    tmp_path: Path, gh: Path, *extra: str
) -> tuple[subprocess.CompletedProcess, Path, Path]:
    jobs_out = tmp_path / "ci-jobs.json"
    run_out = tmp_path / "ci-run.json"
    result = subprocess.run(
        [
            "python3",
            str(READER),
            "--repo",
            REPO,
            "--run-id",
            "900",
            "--jobs-out",
            str(jobs_out),
            "--run-out",
            str(run_out),
            "--gh",
            str(gh),
            "--retry-sleep",
            "0",
            *extra,
        ],
        capture_output=True,
        text=True,
    )
    return result, jobs_out, run_out


def _jobs_payload() -> dict:
    return {
        "jobs": [
            {
                "name": "Tests (Python 3.10)",
                "started_at": "2026-11-17T19:40:00Z",
                "completed_at": "2026-11-17T19:45:00Z",
                "conclusion": "success",
            }
        ]
    }


def _run_payload() -> dict:
    return {
        "id": 900,
        "run_started_at": "2026-11-17T19:39:56Z",
        "updated_at": "2026-11-17T19:46:04Z",
    }


def _budget(tmp_path: Path) -> Path:
    budget = tmp_path / "budget.json"
    budget.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "exclude_jobs": [],
                "default_job_budget_seconds": 600,
                "total_job_budget_seconds": 1500,
                "run_wall_clock_budget_seconds": 1800,
            }
        )
    )
    return budget


@pytest.mark.parametrize(
    ("flaky_path", "expected_calls"),
    [
        (JOBS_PATH, [JOBS_PATH, JOBS_PATH, RUN_PATH]),
        (RUN_PATH, [JOBS_PATH, RUN_PATH, RUN_PATH]),
    ],
)
def test_a_transient_502_on_either_endpoint_still_feeds_the_checker(
    tmp_path: Path, flaky_path: str, expected_calls: list[str]
) -> None:
    """The #6439 contract end to end: one transient 502 on either current-
    run endpoint survives the retry, both payloads land, and the real
    checker accepts the files this reader wrote -- including their request
    paths, pinned via the call log (`?per_page=100` on the jobs read)."""
    gh, log = _fake_gh(
        tmp_path,
        {JOBS_PATH: _jobs_payload(), RUN_PATH: _run_payload()},
        flaky={flaky_path: 1},
    )
    result, jobs_out, run_out = _run_reader(tmp_path, gh)
    assert result.returncode == 0, result.stdout + result.stderr
    assert log.read_text().splitlines() == expected_calls
    assert json.loads(jobs_out.read_text()) == _jobs_payload()
    assert json.loads(run_out.read_text()) == _run_payload()

    check = subprocess.run(
        [
            "python3",
            str(CHECKER),
            "--jobs-json",
            str(jobs_out),
            "--run-json",
            str(run_out),
            "--budget",
            str(_budget(tmp_path)),
        ],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stdout + check.stderr


def test_both_endpoints_can_spend_the_whole_shared_call_budget(
    tmp_path: Path,
) -> None:
    """The budget is `2 * RETRY_ATTEMPTS` and is shared, not per-endpoint:
    both reads surviving only on their final allowed attempt (two 502s
    each with the current client) must still fit in it exactly."""
    gh, log = _fake_gh(
        tmp_path,
        {JOBS_PATH: _jobs_payload(), RUN_PATH: _run_payload()},
        flaky={JOBS_PATH: RETRY_ATTEMPTS - 1, RUN_PATH: RETRY_ATTEMPTS - 1},
    )
    result, jobs_out, run_out = _run_reader(tmp_path, gh)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text().splitlines()
    assert calls.count(JOBS_PATH) == RETRY_ATTEMPTS
    assert calls.count(RUN_PATH) == RETRY_ATTEMPTS
    assert len(calls) == 2 * RETRY_ATTEMPTS
    assert json.loads(jobs_out.read_text()) == _jobs_payload()
    assert json.loads(run_out.read_text()) == _run_payload()


def test_a_permanent_error_fails_the_step_with_an_endpoint_diagnostic(
    tmp_path: Path,
) -> None:
    """A 404 is never retried (#6214's rule) and must fail the fetch step
    -- these payloads have no fallback -- naming the endpoint, after exactly
    one attempt, with neither output file written."""
    gh, log = _fake_gh(
        tmp_path,
        {JOBS_PATH: _jobs_payload(), RUN_PATH: _run_payload()},
        hard={JOBS_PATH: NOT_FOUND_404},
    )
    result, jobs_out, run_out = _run_reader(tmp_path, gh)
    assert result.returncode != 0
    assert "jobs" in result.stderr
    assert JOBS_PATH in result.stderr
    assert "HTTP 404" in result.stderr
    assert log.read_text().splitlines() == [JOBS_PATH]
    assert not jobs_out.exists()
    assert not run_out.exists()


def test_exhausted_transient_retries_fail_the_step(tmp_path: Path) -> None:
    """A 502 that survives the whole retry bound is an ordinary terminal
    failure here (the history fetcher would fall back; this reader has
    nothing to fall back to): nonzero exit, the endpoint named, no files."""
    gh, log = _fake_gh(
        tmp_path,
        {JOBS_PATH: _jobs_payload(), RUN_PATH: _run_payload()},
        flaky={JOBS_PATH: 99},
    )
    result, jobs_out, run_out = _run_reader(tmp_path, gh)
    assert result.returncode != 0
    assert "jobs" in result.stderr
    assert "HTTP 502" in result.stderr
    assert log.read_text().splitlines() == [JOBS_PATH] * RETRY_ATTEMPTS
    assert not jobs_out.exists()
    assert not run_out.exists()


def test_the_second_read_failing_writes_neither_output(tmp_path: Path) -> None:
    """Both payloads are fetched before either file is written: the jobs
    read succeeding first must not leave a half-written pair behind when
    the run read fails, or the checker would judge a run whose timings it
    only half-read."""
    gh, log = _fake_gh(
        tmp_path,
        {JOBS_PATH: _jobs_payload(), RUN_PATH: _run_payload()},
        hard={RUN_PATH: NOT_FOUND_404},
    )
    result, jobs_out, run_out = _run_reader(tmp_path, gh)
    assert result.returncode != 0
    assert "run" in result.stderr
    assert RUN_PATH in result.stderr
    assert log.read_text().splitlines() == [JOBS_PATH, RUN_PATH]
    assert not jobs_out.exists()
    assert not run_out.exists()


def test_ci_workflow_invokes_the_reader_with_the_checkers_paths() -> None:
    """The wiring contract: the timings step goes through the bounded-
    retry reader, and the reader writes exactly the two files the budget
    check reads -- the same paths the bare `gh api` redirects produced."""
    workflow = WORKFLOW.read_text()
    assert "scripts/fetch_ci_wall_clock_timings.py" in workflow
    assert "--jobs-out /tmp/ci-jobs.json" in workflow
    assert "--run-out /tmp/ci-run.json" in workflow
    assert "--jobs-json /tmp/ci-jobs.json" in workflow
    assert "--run-json /tmp/ci-run.json" in workflow
    # The old one-attempt-per-endpoint form is gone (#6439's whole point).
    assert (
        'gh api "repos/${{ github.repository }}/actions/runs/${{ github.run_id }}'
        not in workflow
    )
