"""Tests for `scripts/check_ci_wall_clock.py` (issue #1971): the CI
wall-clock budget check.

Issue #1971 measured this repo's CI as the only one in a multi-repo survey
whose wall clock is dominated by *compute* rather than by self-hosted runner
queue time (~1000 s of summed job seconds against ~400-780 s of run wall
clock, i.e. already parallelised). The budget check exists so a future
regression past that measured baseline fails visibly instead of drifting.

Everything here runs against synthetic `/jobs` API payloads built in-test --
never against a live GitHub Actions API -- so the within-budget, per-job
breach, total-compute breach and queue-breach cases are fully controlled.
Two tests deliberately *do* read this repo's checked-in budget file and
`ci.yml`, asserting the wiring stays in place and that every budgeted job
name still names a real job.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_ci_wall_clock.py"
BUDGET_FILE = REPO_ROOT / ".github" / "ci-wall-clock-budget.json"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Exit-code contract, mirroring scripts/check-release-lag.sh's tiering:
#   0 = within budget, a queue-only breach, or a report-only run,
#   1 = compute budget breach, 2 = the check itself could not run.
EXIT_OK = 0
EXIT_BREACH = 1
EXIT_CANNOT_RUN = 2


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    if env is None:
        # Never inherit a real GITHUB_STEP_SUMMARY (these tests can themselves
        # run inside GitHub Actions) -- only the test that asserts on it sets it.
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
    return subprocess.run(
        ["python3", str(SCRIPT), *args], capture_output=True, text=True, env=env
    )


def _job(
    name: str,
    *,
    started: str,
    completed: str | None,
    conclusion: str | None = "success",
) -> dict:
    return {
        "name": name,
        "started_at": started,
        "completed_at": completed,
        "conclusion": conclusion,
    }


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload))
    return path


def _budget(tmp_path: Path, **overrides) -> Path:
    budget = {
        "schema_version": 1,
        "exclude_jobs": ["CI wall-clock budget"],
        "default_job_budget_seconds": 600,
        "total_job_budget_seconds": 1500,
        "run_wall_clock_budget_seconds": 1800,
        "jobs": {"Fast job": 120, "Slow job": 360},
    }
    budget.update(overrides)
    return _write(tmp_path / "budget.json", budget)


def _jobs(tmp_path: Path, jobs: list[dict]) -> Path:
    return _write(tmp_path / "jobs.json", {"total_count": len(jobs), "jobs": jobs})


def _run_meta(tmp_path: Path, run_started_at: str) -> Path:
    return _write(tmp_path / "run.json", {"run_started_at": run_started_at})


# --------------------------------------------------------------------------
# Within budget
# --------------------------------------------------------------------------


def test_within_budget_exits_zero(tmp_path: Path) -> None:
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Fast job",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:00:22Z",
            ),
            _job(
                "Slow job",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:03:10Z",
            ),
        ],
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        "--run-json",
        str(_run_meta(tmp_path, "2026-09-17T07:00:00Z")),
        "--budget",
        str(_budget(tmp_path)),
    )
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert "Slow job" in result.stdout
    # The compute-vs-queue split is the whole point of the check -- both
    # aggregates must be reported even on a green run.
    assert "total job-seconds" in result.stdout
    assert "run wall clock" in result.stdout


def test_reports_both_aggregates_in_json_format(tmp_path: Path) -> None:
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Fast job",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:00:22Z",
            ),
            _job(
                "Slow job",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:03:10Z",
            ),
        ],
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        "--run-json",
        str(_run_meta(tmp_path, "2026-09-17T07:00:00Z")),
        "--budget",
        str(_budget(tmp_path)),
        "--format",
        "json",
    )
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["total_job_seconds"] == 12 + 180
    assert payload["run_wall_clock_seconds"] == 190
    assert payload["breaches"] == []
    by_name = {j["name"]: j for j in payload["jobs"]}
    assert by_name["Slow job"]["seconds"] == 180
    assert by_name["Slow job"]["budget_seconds"] == 360


# --------------------------------------------------------------------------
# Breaches
# --------------------------------------------------------------------------


def test_per_job_breach_fails_and_names_the_job(tmp_path: Path) -> None:
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Fast job",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:05:10Z",
            ),
        ],
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        "--run-json",
        str(_run_meta(tmp_path, "2026-09-17T07:00:00Z")),
        "--budget",
        str(_budget(tmp_path)),
    )
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr
    assert "Fast job" in result.stdout
    assert "300" in result.stdout and "120" in result.stdout


def test_unbudgeted_job_falls_back_to_the_default_budget(tmp_path: Path) -> None:
    """A newly added job must still be covered -- otherwise budget coverage
    silently drifts as jobs are added, which is the failure mode #1971 is
    about."""
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Brand new job",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:12:10Z",  # 720s > 600s default
            ),
        ],
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        "--run-json",
        str(_run_meta(tmp_path, "2026-09-17T07:00:00Z")),
        "--budget",
        str(_budget(tmp_path)),
    )
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr
    assert "Brand new job" in result.stdout


def test_total_compute_breach_is_classified_as_compute(tmp_path: Path) -> None:
    """Every job individually under budget, but the summed job-seconds over
    the total -- the "work has to shrink" case."""
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Slow job",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:05:10Z",
            )
        ]
        * 6,  # 6 x 300s = 1800s > 1500s total, each under its 360s budget
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        "--run-json",
        str(_run_meta(tmp_path, "2026-09-17T07:00:00Z")),
        "--budget",
        str(_budget(tmp_path)),
        "--format",
        "json",
    )
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    kinds = {b["kind"] for b in payload["breaches"]}
    assert kinds == {"compute"}


def test_queue_only_breach_is_reported_but_does_not_fail(tmp_path: Path) -> None:
    """Wall clock blown while compute is well within budget is a *scheduling*
    breach -- the distinction #1971's whole survey turned on. Misreporting it
    as compute would send the next reader off optimising jobs that are fine,
    and *failing* on it would redden a build for runner-pool contention no PR
    author can fix. So it is classified `queue`, reported, and exits 0."""
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Fast job",
                started="2026-09-17T08:00:00Z",
                completed="2026-09-17T08:00:24Z",
            ),
        ],
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        # Run created an hour before the job ever started: pure queue time.
        "--run-json",
        str(_run_meta(tmp_path, "2026-09-17T07:00:00Z")),
        "--budget",
        str(_budget(tmp_path)),
        "--format",
        "json",
    )
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert [b["kind"] for b in payload["breaches"]] == ["queue"]
    assert payload["status"] == "breach"
    assert payload["total_job_seconds"] == 24


def test_queue_only_breach_still_says_so_in_the_text_report(tmp_path: Path) -> None:
    """Exiting 0 must not make the breach invisible: it still prints, and still
    says which kind it is, so queue starvation stays attributable."""
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Fast job",
                started="2026-09-17T08:00:00Z",
                completed="2026-09-17T08:00:24Z",
            ),
        ],
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        "--run-json",
        str(_run_meta(tmp_path, "2026-09-17T07:00:00Z")),
        "--budget",
        str(_budget(tmp_path)),
    )
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert "budget breach(es)" in result.stdout
    assert "run wall clock" in result.stdout
    assert "scheduling/queue breach" in result.stdout


def test_compute_breach_alongside_a_queue_breach_still_fails(tmp_path: Path) -> None:
    """A queue breach does not launder a compute breach sharing the same run:
    one job blows its own budget (compute) while total compute stays inside
    budget, so the wall-clock queue breach is reported too. Mixed => exit 1."""
    jobs = _jobs(
        tmp_path,
        [
            # 700 s against a 120 s job budget: a compute breach. Total compute
            # (700 s) is still under the 1500 s total budget, so the wall-clock
            # line is not suppressed and a queue breach is reported alongside.
            _job(
                "Fast job",
                started="2026-09-17T09:00:00Z",
                completed="2026-09-17T09:11:40Z",
            ),
        ],
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        "--run-json",
        str(_run_meta(tmp_path, "2026-09-17T07:00:00Z")),
        "--budget",
        str(_budget(tmp_path)),
        "--format",
        "json",
    )
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert {b["kind"] for b in payload["breaches"]} == {"compute", "queue"}


# --------------------------------------------------------------------------
# Report-only mode (fork PRs)
# --------------------------------------------------------------------------


def test_report_only_never_fails_but_still_warns(tmp_path: Path) -> None:
    """Fork PRs fall back to `ubuntu-latest` (ci.yml's runner conditional), so
    budgets tuned against the self-hosted pool must not redden them."""
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Fast job",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:20:10Z",
            ),
        ],
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        "--run-json",
        str(_run_meta(tmp_path, "2026-09-17T07:00:00Z")),
        "--budget",
        str(_budget(tmp_path)),
        "--report-only",
        "--annotate",
    )
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert "::warning" in result.stdout
    assert "::error" not in result.stdout


def test_annotate_emits_an_error_annotation_on_a_real_breach(tmp_path: Path) -> None:
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Fast job",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:20:10Z",
            ),
        ],
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        "--run-json",
        str(_run_meta(tmp_path, "2026-09-17T07:00:00Z")),
        "--budget",
        str(_budget(tmp_path)),
        "--annotate",
    )
    assert result.returncode == EXIT_BREACH
    assert "::error" in result.stdout


def test_annotate_emits_a_warning_not_an_error_on_a_queue_only_breach(
    tmp_path: Path,
) -> None:
    """A queue-only breach exits 0 -- the annotation must say `::warning::`,
    not `::error::`. A red annotation on a green job trains people to ignore
    annotations (follow-up from #1993, filed as #2056)."""
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Fast job",
                started="2026-09-17T08:00:00Z",
                completed="2026-09-17T08:00:24Z",
            ),
        ],
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        # Run created an hour before the job ever started: pure queue time.
        "--run-json",
        str(_run_meta(tmp_path, "2026-09-17T07:00:00Z")),
        "--budget",
        str(_budget(tmp_path)),
        "--annotate",
    )
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert "::warning" in result.stdout
    assert "::error" not in result.stdout


def test_annotate_on_mixed_breach_warns_for_queue_and_errors_for_compute(
    tmp_path: Path,
) -> None:
    """A compute breach alongside a queue breach (exit 1 overall) must still
    annotate the queue line as `::warning::` -- only the compute line, the one
    that actually reddens the build, gets `::error::`."""
    jobs = _jobs(
        tmp_path,
        [
            # 700 s against a 120 s job budget: a compute breach. Total
            # compute (700 s) stays under the 1500 s total budget, so the
            # wall-clock queue breach is reported alongside it.
            _job(
                "Fast job",
                started="2026-09-17T09:00:00Z",
                completed="2026-09-17T09:11:40Z",
            ),
        ],
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        "--run-json",
        str(_run_meta(tmp_path, "2026-09-17T07:00:00Z")),
        "--budget",
        str(_budget(tmp_path)),
        "--annotate",
        "--format",
        "json",
    )
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr
    stdout_lines = result.stdout.splitlines()
    annotation_lines = [ln for ln in stdout_lines if ln.startswith("::")]
    assert len(annotation_lines) == 2
    warning_lines = [ln for ln in annotation_lines if ln.startswith("::warning")]
    error_lines = [ln for ln in annotation_lines if ln.startswith("::error")]
    assert len(warning_lines) == 1 and "(queue)" in warning_lines[0]
    assert len(error_lines) == 1 and "(compute)" in error_lines[0]


# --------------------------------------------------------------------------
# Payload edge cases
# --------------------------------------------------------------------------


def test_still_running_and_skipped_jobs_are_excluded(tmp_path: Path) -> None:
    """The budget job measures the run it is part of, so its own row has a
    null completed_at. Skipped/cancelled jobs carry no meaningful duration."""
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Fast job",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:00:22Z",
            ),
            _job(
                "CI wall-clock budget",
                started="2026-09-17T07:03:20Z",
                completed=None,
                conclusion=None,
            ),
            _job(
                "Skipped job",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:00:11Z",
                conclusion="skipped",
            ),
        ],
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        "--run-json",
        str(_run_meta(tmp_path, "2026-09-17T07:00:00Z")),
        "--budget",
        str(_budget(tmp_path)),
        "--format",
        "json",
    )
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert [j["name"] for j in payload["jobs"]] == ["Fast job"]
    assert payload["total_job_seconds"] == 12


def test_no_measurable_jobs_cannot_run(tmp_path: Path) -> None:
    """A check that silently stops checking is worse than no check: an empty
    measurement set is exit 2, not a spurious green."""
    result = _run(
        "--jobs-json",
        str(_jobs(tmp_path, [])),
        "--budget",
        str(_budget(tmp_path)),
    )
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr


def test_malformed_budget_file_cannot_run(tmp_path: Path) -> None:
    bad = _write(tmp_path / "bad.json", {"schema_version": 1})  # no budgets
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Fast job",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:00:22Z",
            )
        ],
    )
    result = _run("--jobs-json", str(jobs), "--budget", str(bad))
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr


def test_unknown_schema_version_cannot_run(tmp_path: Path) -> None:
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Fast job",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:00:22Z",
            )
        ],
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        "--budget",
        str(_budget(tmp_path, schema_version=99)),
    )
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr


def test_missing_run_json_derives_wall_clock_from_job_timestamps(
    tmp_path: Path,
) -> None:
    """Without the run object the pre-run queue wait is unknowable, so the
    fallback measures the job span only -- and says so."""
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Fast job",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:00:22Z",
            ),
            _job(
                "Slow job",
                started="2026-09-17T07:00:30Z",
                completed="2026-09-17T07:03:30Z",
            ),
        ],
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        "--budget",
        str(_budget(tmp_path)),
        "--format",
        "json",
    )
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["run_wall_clock_seconds"] == 200
    assert payload["run_wall_clock_includes_queue"] is False


def test_step_summary_is_written_when_github_step_summary_is_set(
    tmp_path: Path,
) -> None:
    summary = tmp_path / "summary.md"
    env = dict(os.environ, GITHUB_STEP_SUMMARY=str(summary))
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Fast job",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:00:22Z",
            )
        ],
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        "--budget",
        str(_budget(tmp_path)),
        env=env,
    )
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert "Fast job" in summary.read_text()


# --------------------------------------------------------------------------
# Wiring against this repo's own checked-in files
# --------------------------------------------------------------------------


def test_repo_budget_file_is_valid_and_covers_every_ci_job() -> None:
    """Anti-drift: every budgeted job name must still name a job `ci.yml`
    actually produces, so a renamed job cannot leave a stale, unreachable
    budget row behind."""
    budget = json.loads(BUDGET_FILE.read_text())
    assert budget["schema_version"] == 1
    assert budget["total_job_budget_seconds"] > 0
    assert budget["run_wall_clock_budget_seconds"] > 0
    assert budget["default_job_budget_seconds"] > 0

    workflow = WORKFLOW.read_text()
    for job_name, seconds in budget["jobs"].items():
        assert isinstance(seconds, int) and seconds > 0, job_name
        # Matrix legs are named via `${{ matrix.job_name }}` / the
        # `python-version` interpolation, so match on the distinguishing
        # literal that appears in the matrix definition rather than the
        # fully-interpolated check name.
        needle = job_name.split(" (Python ")[0] if "(Python " in job_name else job_name
        assert needle in workflow, f"budgeted job {job_name!r} not found in ci.yml"


def test_ci_workflow_invokes_the_budget_check() -> None:
    workflow = WORKFLOW.read_text()
    assert "scripts/check_ci_wall_clock.py" in workflow
    assert "ci-wall-clock-budget.json" in workflow
    # The check must gate (fail the run), not merely report, on a
    # same-repo run -- that is the "fails visibly rather than drifting"
    # requirement. Fork PRs get --report-only instead.
    assert "--report-only" in workflow


def test_repo_budgets_leave_headroom_over_the_measured_baseline() -> None:
    """The budgets are documented as derived from a measured baseline; assert
    the recorded baseline is actually below them, so a typo cannot ship a
    budget that is already breached on a green run."""
    budget = json.loads(BUDGET_FILE.read_text())
    baseline = budget["baseline"]
    assert baseline["total_job_seconds_max"] < budget["total_job_budget_seconds"]
    assert (
        baseline["run_wall_clock_seconds_max"]
        < (budget["run_wall_clock_budget_seconds"])
    )
    for job_name, observed in baseline["job_seconds_p90"].items():
        allowed = budget["jobs"].get(job_name, budget["default_job_budget_seconds"])
        assert observed < allowed, job_name
