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

Issue #2615 replaced the single-run fixed ceiling with a cross-run model
(rolling baseline + pool-contention factor + same-commit corroboration). The
"Rolling-window model" section below exercises that logic, and the "Replaying
real runs" section replays *measured* timings captured from this repo's own
Actions history -- the four concurrent runs of 2026-09-29 19:39-20:03 that the
old model reddened for pool contention, and the genuine whole-matrix slowdown
of run 36003284901 that it must still catch. Those numbers are not invented:
see tests/fixtures/ci_wall_clock/actions_timings.json for their provenance.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import quote

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_ci_wall_clock.py"
FETCH_SCRIPT = REPO_ROOT / "scripts" / "fetch_ci_wall_clock_history.py"
BUDGET_FILE = REPO_ROOT / ".github" / "ci-wall-clock-budget.json"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
MEASURED = REPO_ROOT / "tests" / "fixtures" / "ci_wall_clock" / "actions_timings.json"

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
    steps: list[dict] | None = None,
) -> dict:
    job = {
        "name": name,
        "started_at": started,
        "completed_at": completed,
        "conclusion": conclusion,
    }
    if steps is not None:
        job["steps"] = steps
    return job


def _step(
    name: str,
    *,
    started: str,
    completed: str,
    conclusion: str = "success",
) -> dict:
    """One entry of a job's `steps[]`, as the `/jobs` API returns it."""
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
# Rolling-window helpers (issue #2615)
# --------------------------------------------------------------------------


def _history_run(
    run_id: int,
    roles: list[str],
    jobs: dict[str, int],
    *,
    partial: list[str] | None = None,
    head_sha: str = "sha",
) -> dict:
    return {
        "run_id": run_id,
        "head_sha": head_sha,
        "head_branch": "main",
        "roles": roles,
        "jobs": jobs,
        "partial_jobs": partial or [],
    }


def _history(tmp_path: Path, runs: list[dict], *, schema_version: int = 1) -> Path:
    return _write(
        tmp_path / "history.json",
        {
            "schema_version": schema_version,
            "generated_at": "2026-09-29T21:00:00+00:00",
            "run_id": 1,
            "head_sha": "current",
            "error": None,
            "runs": runs,
        },
    )


def _baseline_runs(jobs: dict[str, int], *, count: int = 8) -> list[dict]:
    """`count` identical baseline runs -- a zero-MAD, unambiguous window."""
    return [_history_run(1000 + i, ["baseline"], dict(jobs)) for i in range(count)]


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
# actions/cache misses are not compute (issue #2617)
# --------------------------------------------------------------------------
#
# `ci.yml`'s Tests legs restore pinned Yosys/SymbiYosys/Icarus/Verilator
# builds from `actions/cache` and rebuild them from source only when that
# restore misses -- steps named `"... (cache miss only)"`, gated on the cache
# action's `cache-hit` output, and reported as `skipped` on a hit. When the
# Actions cache service degraded on 2026-09-29/30 those rebuilds ran on every
# leg, the check counted them as compute, and eleven Judge-approved PRs went
# red for a slowdown that was not in the code. The rule below is deliberately
# narrow: a rebuild step that actually ran, plus that job's own cache
# restore/save steps, are excluded -- nothing else, ever.


def _cache_miss_job(
    *,
    rebuild_from: str,
    rebuild_to: str,
    pytest_from: str,
    pytest_to: str,
    completed: str,
    rebuild_conclusion: str = "success",
    restore_from: str = "2026-09-30T07:00:00Z",
    restore_to: str = "2026-09-30T07:00:02Z",
) -> dict:
    """A `Slow job` (360 s budget) shaped like a real `Tests (Python X)` leg."""
    return _job(
        "Slow job",
        started="2026-09-30T07:00:00Z",
        completed=completed,
        steps=[
            _step(
                "Cache pinned Verilator build",
                started=restore_from,
                completed=restore_to,
            ),
            _step(
                "Build + install pinned Verilator (cache miss only)",
                started=rebuild_from,
                completed=rebuild_to,
                conclusion=rebuild_conclusion,
            ),
            _step("pytest", started=pytest_from, completed=pytest_to),
        ],
    )


def _cache_args(tmp_path: Path, job: dict, *extra: str) -> list[str]:
    return [
        "--jobs-json",
        str(_jobs(tmp_path, [job])),
        "--run-json",
        str(_run_meta(tmp_path, "2026-09-30T07:00:00Z")),
        "--budget",
        str(_budget(tmp_path)),
        *extra,
    ]


def test_cache_miss_rebuild_time_does_not_breach_the_budget(tmp_path: Path) -> None:
    """AC1: a leg whose *only* overage is a pinned-tool rebuild after a cache
    miss passes, and the report names the miss instead of blaming the code.

    600 s wall on a 360 s budget, of which 300 s is the Verilator rebuild plus
    the cache restore: 300 s of real compute, comfortably inside budget."""
    job = _cache_miss_job(
        rebuild_from="2026-09-30T07:00:02Z",
        rebuild_to="2026-09-30T07:05:00Z",  # 298 s of source build
        pytest_from="2026-09-30T07:05:00Z",
        pytest_to="2026-09-30T07:09:58Z",  # 298 s of real work
        completed="2026-09-30T07:10:00Z",  # 600 s job
    )
    result = _run(*_cache_args(tmp_path, job))
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert "CACHE MISS" in result.stdout
    assert "Build + install pinned Verilator (cache miss only)" in result.stdout
    assert "OK: every job" in result.stdout


def test_a_real_slowdown_alongside_a_cache_miss_still_fails(tmp_path: Path) -> None:
    """AC2: the exclusion is not an allowance. Same 600 s leg, but only 120 s
    of it is the cache miss and pytest itself has grown to 478 s -- 480 s of
    compute against a 360 s budget still breaches, and the report says the
    cache miss has already been discounted."""
    job = _cache_miss_job(
        rebuild_from="2026-09-30T07:00:02Z",
        rebuild_to="2026-09-30T07:02:00Z",  # 118 s of source build
        pytest_from="2026-09-30T07:02:00Z",
        pytest_to="2026-09-30T07:09:58Z",  # 478 s: the genuine regression
        completed="2026-09-30T07:10:00Z",
    )
    result = _run(*_cache_args(tmp_path, job))
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr
    assert "480s exceeds the 360s budget" in result.stdout
    # The miss is still reported -- but as context, not as an excuse.
    assert "CACHE MISS" in result.stdout
    assert "ALREADY excluded" in result.stdout


def test_a_cache_hit_excludes_nothing(tmp_path: Path) -> None:
    """On a hit the rebuild step is reported `skipped`, so there is no miss to
    discount and a 598 s pytest breaches exactly as it did before #2617."""
    job = _cache_miss_job(
        rebuild_from="2026-09-30T07:00:02Z",
        rebuild_to="2026-09-30T07:00:02Z",
        rebuild_conclusion="skipped",
        pytest_from="2026-09-30T07:00:02Z",
        pytest_to="2026-09-30T07:10:00Z",
        completed="2026-09-30T07:10:00Z",
    )
    result = _run(*_cache_args(tmp_path, job))
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr
    assert "600s exceeds the 360s budget" in result.stdout
    assert "CACHE MISS" not in result.stdout


def test_cache_restore_time_is_excluded_only_when_a_rebuild_actually_ran(
    tmp_path: Path,
) -> None:
    """A slow `actions/cache` step on its own is not licence to discount it:
    without a rebuild step having run there was no miss, so the full 600 s
    still counts."""
    job = _cache_miss_job(
        restore_from="2026-09-30T07:00:00Z",
        restore_to="2026-09-30T07:01:40Z",  # 100 s of cache I/O
        rebuild_from="2026-09-30T07:01:40Z",
        rebuild_to="2026-09-30T07:01:40Z",
        rebuild_conclusion="skipped",
        pytest_from="2026-09-30T07:01:40Z",
        pytest_to="2026-09-30T07:10:00Z",
        completed="2026-09-30T07:10:00Z",
    )
    result = _run(*_cache_args(tmp_path, job))
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr
    assert "600s exceeds the 360s budget" in result.stdout


def test_cache_miss_seconds_are_reported_separately_in_json(tmp_path: Path) -> None:
    """The JSON stays auditable: the excluded time is published, not silently
    dropped, so a reader can always reconstruct the job's real duration."""
    job = _cache_miss_job(
        rebuild_from="2026-09-30T07:00:02Z",
        rebuild_to="2026-09-30T07:05:00Z",
        pytest_from="2026-09-30T07:05:00Z",
        pytest_to="2026-09-30T07:09:58Z",
        completed="2026-09-30T07:10:00Z",
    )
    result = _run(*_cache_args(tmp_path, job, "--format", "json"))
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["total_job_seconds"] == 300
    assert payload["total_cache_miss_seconds"] == 300
    (row,) = payload["jobs"]
    assert row["seconds"] == 300
    assert row["cache_miss_seconds"] == 300
    assert row["total_seconds"] == 600  # what the GitHub UI shows
    assert row["over_budget"] is False
    assert row["cache_miss_steps"] == [
        "Build + install pinned Verilator (cache miss only)"
    ]


def test_cache_miss_is_annotated_as_a_notice_not_an_error(tmp_path: Path) -> None:
    """A missed cache is neither the author's doing nor the author's to fix,
    so it annotates as `::notice::` -- but it does annotate, because it is the
    first thing a reader of a slow run needs to know."""
    job = _cache_miss_job(
        rebuild_from="2026-09-30T07:00:02Z",
        rebuild_to="2026-09-30T07:05:00Z",
        pytest_from="2026-09-30T07:05:00Z",
        pytest_to="2026-09-30T07:09:58Z",
        completed="2026-09-30T07:10:00Z",
    )
    result = _run(*_cache_args(tmp_path, job, "--annotate"))
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    notices = [ln for ln in result.stdout.splitlines() if ln.startswith("::notice")]
    assert len(notices) == 1
    assert "CI cache miss" in notices[0] and "Slow job" in notices[0]
    assert "::error" not in result.stdout


def test_cache_miss_is_named_in_the_step_summary(tmp_path: Path) -> None:
    """AC1's "its summary names the cache miss": the job summary is where a
    reader lands from a red (or suspiciously slow) run."""
    summary = tmp_path / "summary.md"
    env = dict(os.environ, GITHUB_STEP_SUMMARY=str(summary))
    job = _cache_miss_job(
        rebuild_from="2026-09-30T07:00:02Z",
        rebuild_to="2026-09-30T07:05:00Z",
        pytest_from="2026-09-30T07:05:00Z",
        pytest_to="2026-09-30T07:09:58Z",
        completed="2026-09-30T07:10:00Z",
    )
    result = _run(*_cache_args(tmp_path, job), env=env)
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    written = summary.read_text()
    assert "CACHE MISS" in written
    assert "Build + install pinned Verilator (cache miss only)" in written


def test_cache_miss_in_an_unbudgeted_job_uses_the_default_budget(
    tmp_path: Path,
) -> None:
    """The exclusion composes with the default-budget fallback: 720 s of wall
    on a job with no row of its own, 300 s of it a cache miss, is 420 s of
    compute against the 600 s default -- green, and still named."""
    job = _job(
        "Brand new job",
        started="2026-09-30T07:00:00Z",
        completed="2026-09-30T07:12:00Z",  # 720 s > the 600 s default
        steps=[
            _step(
                "Build + install pinned Yosys (cache miss only)",
                started="2026-09-30T07:00:00Z",
                completed="2026-09-30T07:05:00Z",
            ),
            _step(
                "pytest",
                started="2026-09-30T07:05:00Z",
                completed="2026-09-30T07:12:00Z",
            ),
        ],
    )
    result = _run(*_cache_args(tmp_path, job, "--format", "json"))
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    (row,) = json.loads(result.stdout)["jobs"]
    assert row["budget_seconds"] == 600
    assert (row["seconds"], row["cache_miss_seconds"]) == (420, 300)


def test_malformed_step_timestamps_never_take_the_check_down(tmp_path: Path) -> None:
    """Step timings only ever *subtract* from the compute figure, so a broken
    one must degrade to "subtract nothing" rather than fail the check (exit 2)
    and leave the budget unenforced."""
    job = _job(
        "Slow job",
        started="2026-09-30T07:00:00Z",
        completed="2026-09-30T07:10:00Z",
        steps=[
            {
                "name": "Build + install pinned Verilator (cache miss only)",
                "started_at": "not-a-timestamp",
                "completed_at": None,
                "conclusion": "success",
            },
            "not-a-step",
        ],
    )
    result = _run(*_cache_args(tmp_path, job))
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr
    assert "600s exceeds the 360s budget" in result.stdout


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
# Rolling-window model (issue #2615)
# --------------------------------------------------------------------------
#
# The fixture budget's committed ceiling for "Slow job" is 360 s. With eight
# identical 300 s baseline runs the rolling threshold is
# max(1.5 x 300, 300 + k x 0, 180) = 450 s, so the two bases are trivially
# distinguishable by the verdict on a 400 s job.


def _rolling_case(
    tmp_path: Path,
    seconds: int,
    history_runs: list[dict],
    *,
    extra: tuple[str, ...] = (),
) -> tuple[subprocess.CompletedProcess, dict]:
    end = 7 * 3600 + 10 + seconds
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Slow job",
                started="2026-09-17T07:00:10Z",
                completed=f"2026-09-17T{end // 3600:02d}:"
                f"{end % 3600 // 60:02d}:{end % 60:02d}Z",
            )
        ],
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        "--budget",
        str(_budget(tmp_path)),
        "--history-json",
        str(_history(tmp_path, history_runs)),
        "--format",
        "json",
        *extra,
    )
    return result, _leading_json(result.stdout)


def _leading_json(stdout: str) -> dict:
    """The `--format json` payload, ignoring any `::annotation::` lines that
    `--annotate` prints after it."""
    if not stdout.startswith("{"):
        return {}
    payload, _ = json.JSONDecoder().raw_decode(stdout)
    return payload


def test_rolling_window_replaces_the_committed_ceiling(tmp_path: Path) -> None:
    """A job with history is judged against that history, not against the
    hand-committed number -- the whole point of #2615. 400 s is over the
    committed 360 s ceiling and under the 450 s rolling threshold."""
    result, payload = _rolling_case(tmp_path, 400, _baseline_runs({"Slow job": 300}))
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert payload["model"]["kind"] == "rolling"
    job = payload["jobs"][0]
    assert job["basis"] == "rolling"
    assert job["budget_seconds"] == 450
    assert job["baseline_seconds"] == 300.0
    assert payload["breaches"] == []


def test_rolling_window_still_fails_a_job_past_its_rolling_threshold(
    tmp_path: Path,
) -> None:
    result, payload = _rolling_case(tmp_path, 600, _baseline_runs({"Slow job": 300}))
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr
    breach = next(b for b in payload["breaches"] if b["scope"] == "job")
    assert breach["basis"] == "rolling"
    assert breach["confirmed"] is True


def test_jobs_without_enough_history_keep_the_committed_ceiling(
    tmp_path: Path,
) -> None:
    """Fewer than `min_samples` observations (default 5) is not a baseline.
    A newly added job stays covered by its committed fallback from day one
    rather than being ungated while history accumulates."""
    result, payload = _rolling_case(
        tmp_path, 400, _baseline_runs({"Slow job": 300}, count=3)
    )
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr
    assert payload["jobs"][0]["basis"] == "fixed"
    assert payload["jobs"][0]["budget_seconds"] == 360


def test_without_history_the_check_is_exactly_its_pre_2615_self(
    tmp_path: Path,
) -> None:
    """No `--history-json` at all: every job on its committed ceiling. This is
    what keeps the check runnable locally and green when the Actions API is
    unreachable."""
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Slow job",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:06:50Z",  # 400s > the 360s ceiling
            )
        ],
    )
    result = _run(
        "--jobs-json", str(jobs), "--budget", str(_budget(tmp_path)), "--format", "json"
    )
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["model"]["kind"] == "fixed"
    assert payload["jobs"][0]["basis"] == "fixed"


def test_concurrent_runs_on_other_commits_widen_the_thresholds(
    tmp_path: Path,
) -> None:
    """The measurement that separates "this code got slower" from "the pool
    was saturated": peers that overlapped this run's window, inflated 3x
    against the same baseline, widen this run's thresholds 3x."""
    # Three baselined jobs, because a peer needs at least `min_peer_jobs`
    # baselined rows before it is credible evidence about the pool.
    history = _baseline_runs({"Slow job": 300, "Other job": 100, "Third job": 100}) + [
        _history_run(
            2001, ["peer"], {"Slow job": 900, "Other job": 300, "Third job": 300}
        ),
    ]
    result, payload = _rolling_case(tmp_path, 1200, history)
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert payload["model"]["peer_runs"] == 1
    assert payload["model"]["pool_factor"] == 3.0
    assert payload["jobs"][0]["budget_seconds"] == 1350
    assert payload["breaches"] == []


def test_the_pool_contention_allowance_is_capped(tmp_path: Path) -> None:
    """Past `max_pool_factor` the honest answer is "the pool is broken", not
    "widen the gate without limit"."""
    history = _baseline_runs({"Slow job": 300, "Other job": 100, "Third job": 100}) + [
        _history_run(
            2001,
            ["peer"],
            {"Slow job": 30000, "Other job": 10000, "Third job": 10000},
        ),
    ]
    result, payload = _rolling_case(tmp_path, 3000, history)
    assert payload["model"]["pool_factor"] == 6.0
    assert payload["jobs"][0]["budget_seconds"] == 2700
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr


def test_a_still_running_peer_job_can_only_raise_the_pool_factor(
    tmp_path: Path,
) -> None:
    """Concurrent runs are usually still in flight when this check runs, so
    their unfinished jobs contribute elapsed-so-far -- a lower bound. A job
    that has only been running 10 s proves nothing about the pool and must not
    drag the estimate down; one already past its baseline does count."""
    history = _baseline_runs({"Slow job": 300, "Other job": 100, "Third job": 100}) + [
        _history_run(
            2001,
            ["peer"],
            {"Slow job": 900, "Other job": 300, "Third job": 300, "Fourth": 10},
            partial=["Fourth"],
        ),
    ]
    _, payload = _rolling_case(tmp_path, 400, history)
    assert payload["model"]["pool_factor"] == 3.0


def test_one_noisy_run_in_the_history_window_does_not_move_the_baseline(
    tmp_path: Path,
) -> None:
    """The baseline is a median, not a mean, precisely so a single contended
    run inside the window cannot drag the gate open behind everyone's back.
    Seven 300 s runs and one 3000 s run still baseline at 300 s."""
    history = _baseline_runs({"Slow job": 300}, count=7) + [
        _history_run(1999, ["baseline"], {"Slow job": 3000})
    ]
    result, payload = _rolling_case(tmp_path, 600, history)
    assert payload["jobs"][0]["baseline_seconds"] == 300.0
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr


def test_a_clean_run_of_the_same_commit_downgrades_the_breach(
    tmp_path: Path,
) -> None:
    """One noisy run does not fail the build: identical code measured inside
    the quiet-pool threshold on another run means this run measured the pool.
    Reported as UNCONFIRMED, annotated as a warning, exit 0 -- which is what
    makes "re-run it" a real answer rather than a shrug."""
    history = _baseline_runs({"Slow job": 300}) + [
        _history_run(3001, ["same-tree"], {"Slow job": 310}),
    ]
    result, payload = _rolling_case(tmp_path, 600, history, extra=("--annotate",))
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    breach = next(b for b in payload["breaches"] if b["scope"] == "job")
    assert breach["confirmed"] is False
    assert "::warning" in result.stdout and "::error" not in result.stdout


def test_a_same_commit_run_that_also_breached_confirms_it(tmp_path: Path) -> None:
    """The other half of the rule: when every run of the same commit is slow,
    the slowness is a property of the code and the build goes red."""
    history = _baseline_runs({"Slow job": 300}) + [
        _history_run(3001, ["same-tree"], {"Slow job": 620}),
    ]
    result, payload = _rolling_case(tmp_path, 600, history)
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr
    breach = next(b for b in payload["breaches"] if b["scope"] == "job")
    assert breach["confirmed"] is True


def test_total_compute_gets_the_same_rolling_treatment(tmp_path: Path) -> None:
    """Death by a thousand jobs is still gated -- against the rolling median
    of recent total compute, not against a committed number."""
    history = _baseline_runs({"Slow job": 300, "Other job": 100})
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Other job",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:10:10Z",  # 600s, no per-job baseline breach
            )
        ]
        * 3,
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        "--budget",
        str(_budget(tmp_path)),
        "--history-json",
        str(_history(tmp_path, history)),
        "--format",
        "json",
    )
    payload = json.loads(result.stdout)
    assert payload["total_job_budget_basis"] == "rolling"
    # 400 s of baseline total -> max(1.35 x 400, 400, 600) = 600 s allowed.
    assert payload["total_job_budget_seconds"] == 600
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr


def test_a_malformed_history_file_cannot_run(tmp_path: Path) -> None:
    """A *missing* history is a supported fallback; a *broken* one is a wiring
    mistake. Degrading silently to the loose fallback is how a gate stops
    gating without anyone noticing, so this is exit 2."""
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Slow job",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:05:10Z",
            )
        ],
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        "--budget",
        str(_budget(tmp_path)),
        "--history-json",
        str(_history(tmp_path, [], schema_version=99)),
    )
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr


def test_an_unknown_rolling_window_key_cannot_run(tmp_path: Path) -> None:
    """A typo in the tuning block must not silently leave the model on its
    defaults while the file claims otherwise."""
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Slow job",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:00:22Z",
            )
        ],
    )
    result = _run(
        "--jobs-json",
        str(jobs),
        "--budget",
        str(_budget(tmp_path, rolling_window={"job_ration": 2.0})),
    )
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr


def test_the_rolling_model_can_be_switched_off_in_the_budget_file(
    tmp_path: Path,
) -> None:
    result, payload = _rolling_case(
        tmp_path,
        400,
        _baseline_runs({"Slow job": 300}),
        extra=(),
    )
    assert result.returncode == EXIT_OK
    disabled = _write(
        tmp_path / "disabled.json",
        {
            "schema_version": 1,
            "exclude_jobs": ["CI wall-clock budget"],
            "default_job_budget_seconds": 600,
            "total_job_budget_seconds": 1500,
            "run_wall_clock_budget_seconds": 1800,
            "jobs": {"Fast job": 120, "Slow job": 360},
            "rolling_window": {"enabled": False},
        },
    )
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Slow job",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:06:50Z",
            )
        ],
    )
    off = _run(
        "--jobs-json",
        str(jobs),
        "--budget",
        str(disabled),
        "--history-json",
        str(_history(tmp_path, _baseline_runs({"Slow job": 300}))),
        "--format",
        "json",
    )
    assert off.returncode == EXIT_BREACH, off.stdout + off.stderr
    assert json.loads(off.stdout)["model"]["kind"] == "fixed"


# --------------------------------------------------------------------------
# Replaying real runs (issue #2615's acceptance criteria)
# --------------------------------------------------------------------------


def _measured() -> dict:
    return json.loads(MEASURED.read_text())


def _replay_jobs_payload(tmp_path: Path, run: dict) -> Path:
    """Rebuild a `/jobs` payload from a measured run's job-name -> seconds."""
    start = datetime.datetime.fromisoformat(
        run["run_started_at"].replace("Z", "+00:00")
    )

    def stamp(offset: int) -> str:
        return (
            (start + datetime.timedelta(seconds=offset))
            .isoformat()
            .replace("+00:00", "Z")
        )

    return _jobs(
        tmp_path,
        [
            _job(name, started=stamp(5), completed=stamp(5 + seconds))
            for name, seconds in run["jobs"].items()
        ],
    )


def _replay(
    tmp_path: Path,
    fixture: dict,
    run_id: str,
    baseline_ids: list[str],
    peer_ids: list[str] | None = None,
    same_tree_ids: list[str] | None = None,
) -> tuple[subprocess.CompletedProcess, dict]:
    runs = fixture["runs"]
    history = [
        _history_run(
            int(rid), ["baseline"], runs[rid]["jobs"], head_sha=runs[rid]["head_sha"]
        )
        for rid in baseline_ids
    ]
    history += [
        _history_run(
            int(rid), ["peer"], runs[rid]["jobs"], head_sha=runs[rid]["head_sha"]
        )
        for rid in peer_ids or []
    ]
    history += [
        _history_run(
            int(rid), ["same-tree"], runs[rid]["jobs"], head_sha=runs[rid]["head_sha"]
        )
        for rid in same_tree_ids or []
    ]
    result = _run(
        "--jobs-json",
        str(_replay_jobs_payload(tmp_path, runs[run_id])),
        # Deliberately THIS repo's real budget file: the replay validates the
        # tuning that actually ships, not a fixture's.
        "--budget",
        str(BUDGET_FILE),
        "--history-json",
        str(_history(tmp_path, history)),
        "--format",
        "json",
    )
    return result, _leading_json(result.stdout)


def test_replay_the_2026_09_29_contention_episodes_stay_green(
    tmp_path: Path,
) -> None:
    """Acceptance criterion 1 of #2615, replayed against measured data.

    Between 19:39 and 20:03 on 2026-09-29 four runs on four unrelated
    branches were on the pool together and EVERY leg of EVERY one of them
    landed at 849-1247 s against a ~290 s median -- one of them a PR whose
    entire diff was JSON and Markdown. The old fixed ceiling reddened all of
    them. Each must now pass, because the other three are direct evidence of
    what the pool was doing at the time."""
    fixture = _measured()
    baseline = fixture["groups"]["baseline_main_2026_09_26_to_29"]
    for group in ("contention_2026_09_29_1939", "contention_2026_09_29_1709"):
        episode = fixture["groups"][group]
        for run_id in episode:
            peers = [r for r in episode if r != run_id]
            result, payload = _replay(tmp_path, fixture, run_id, baseline, peers)
            assert result.returncode == EXIT_OK, (
                f"{group}/{run_id} reddened the build: "
                f"{payload.get('breaches')}\n{result.stdout}{result.stderr}"
            )
            assert payload["model"]["pool_factor"] > 1.0
            assert payload["breaches"] == []


def test_replay_run_36003284901_still_fails_as_a_real_slowdown(
    tmp_path: Path,
) -> None:
    """Acceptance criterion 2 of #2615, replayed against measured data.

    Run 36003284901 is a `main` push whose four Tests legs ran 724-816 s
    against a 309-334 s baseline with NOTHING else on the pool. No pool
    evidence, no clean run of the same commit: a confirmed compute breach that
    must still redden the build, or the gate has been widened into
    uselessness."""
    fixture = _measured()
    result, payload = _replay(
        tmp_path,
        fixture,
        "36003284901",
        fixture["groups"]["baseline_main_before_2026_09_24"],
    )
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr
    assert payload["model"]["pool_factor"] == 1.0
    breached = {b["subject"] for b in payload["breaches"] if b["confirmed"]}
    assert breached >= {
        "Tests (Python 3.10)",
        "Tests (Python 3.11)",
        "Tests (Python 3.12)",
        "Tests (Python 3.13)",
    }


def test_replay_each_quiet_main_run_against_its_own_peers_stays_green(
    tmp_path: Path,
) -> None:
    """The false-positive floor: leave-one-out over the fifteen measured
    baseline-branch runs. Every one of them must pass when judged against the
    other fourteen -- a model that reddens ordinary green runs is no better
    than the fixed ceiling it replaces."""
    fixture = _measured()
    window = fixture["groups"]["baseline_main_2026_09_26_to_29"]
    for run_id in window:
        result, payload = _replay(
            tmp_path, fixture, run_id, [r for r in window if r != run_id]
        )
        assert result.returncode == EXIT_OK, (
            f"quiet main run {run_id} reddened the build: {payload.get('breaches')}"
        )


def test_replay_the_invisible_contention_case_is_absorbed_by_a_re_run(
    tmp_path: Path,
) -> None:
    """The documented residual limitation, and its documented answer.

    Run 36611724105 is a Loom-surfaces resync commit -- literally zero compute
    change -- whose legs ran 422-809 s. It had no overlapping `ci.yml` run, so
    the pool factor cannot see that the pool was busy (the contention came
    from workloads outside this repo's Actions history). It therefore breaches
    on its own, and the maintainer's documented answer is to re-run it: with
    one clean run of the same commit available, the breach is UNCONFIRMED and
    the build goes green."""
    fixture = _measured()
    baseline = fixture["groups"]["baseline_main_2026_09_26_to_29"]
    alone, _ = _replay(tmp_path, fixture, "36611724105", baseline)
    assert alone.returncode == EXIT_BREACH, alone.stdout + alone.stderr

    rerun, payload = _replay(
        tmp_path,
        fixture,
        "36611724105",
        baseline,
        same_tree_ids=["36584780975"],
    )
    assert rerun.returncode == EXIT_OK, rerun.stdout + rerun.stderr
    assert all(
        not b["confirmed"] for b in payload["breaches"] if b["kind"] == "compute"
    )


# --------------------------------------------------------------------------
# The history fetcher
# --------------------------------------------------------------------------


def _fake_gh(tmp_path: Path, responses: dict[str, object]) -> Path:
    """A stand-in `gh` that answers `gh api <path>` from a JSON map."""
    table = tmp_path / "responses.json"
    table.write_text(json.dumps(responses))
    script = tmp_path / "fake-gh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"table = json.loads(open({str(table)!r}).read())\n"
        "path = sys.argv[-1]\n"
        "if path not in table:\n"
        "    sys.stderr.write('no such path: ' + path)\n"
        "    sys.exit(1)\n"
        "print(json.dumps(table[path]))\n"
    )
    script.chmod(0o755)
    return script


def _wf_run(
    run_id: int,
    sha: str,
    branch: str,
    start: str,
    end: str,
    *,
    created: str | None = None,
) -> dict:
    return {
        "id": run_id,
        "head_sha": sha,
        "head_branch": branch,
        "status": "completed",
        "conclusion": "success",
        "created_at": created or start,
        "run_started_at": start,
        "updated_at": end,
    }


def _listing_paths(repo: str, cutoff: str | None) -> tuple[str, str]:
    """The fetcher's two run-list API paths for a given `created` cutoff."""
    created = "" if cutoff is None else "&created=" + quote(cutoff)
    base = f"repos/{repo}/actions/workflows/ci.yml/runs"
    return (
        f"{base}?branch=main&status=completed&per_page=20{created}",
        f"{base}?per_page=40{created}",
    )


def _fetch_budget(tmp_path: Path, **rolling) -> Path:
    """A budget file carrying only the fetcher's `rolling_window` keys --
    the repo's real one pins `history_not_before` to 2026-09-30, which would
    (correctly) reject these tests' 2026-09-29 runs."""
    return _write(
        tmp_path / "fetch-budget.json",
        {"rolling_window": {"history_not_before": "", **rolling}},
    )


def _run_fetcher(
    tmp_path: Path, responses: dict, budget: Path, *extra: str
) -> tuple[subprocess.CompletedProcess, dict]:
    out = tmp_path / "history.json"
    result = subprocess.run(
        [
            "python3",
            str(FETCH_SCRIPT),
            "--repo",
            "owner/name",
            "--run-id",
            "900",
            "--out",
            str(out),
            "--budget",
            str(budget),
            "--gh",
            str(_fake_gh(tmp_path, responses)),
            *extra,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result, json.loads(out.read_text())


def _jobs_api(jobs: dict[str, tuple[str, str | None]]) -> dict:
    return {
        "jobs": [
            {
                "name": name,
                "started_at": started,
                "completed_at": completed,
                "conclusion": "success" if completed else None,
            }
            for name, (started, completed) in jobs.items()
        ]
    }


def test_fetcher_labels_baseline_peer_and_same_tree_runs(tmp_path: Path) -> None:
    repo = "owner/name"
    current = _wf_run(
        900, "current", "feature/x", "2026-09-29T19:39:56Z", "2026-09-29T20:00:04Z"
    )
    older_main = _wf_run(
        800, "older", "main", "2026-09-29T14:00:00Z", "2026-09-29T14:10:00Z"
    )
    overlapping = _wf_run(
        700, "other", "feature/y", "2026-09-29T19:42:06Z", "2026-09-29T20:03:22Z"
    )
    untouching = _wf_run(
        600, "far", "feature/z", "2026-09-29T10:00:00Z", "2026-09-29T10:05:00Z"
    )
    same_tree = _wf_run(
        500, "current", "feature/x", "2026-09-29T18:00:00Z", "2026-09-29T18:10:00Z"
    )
    job_payload = _jobs_api({"Tests": ("2026-09-29T19:40:00Z", "2026-09-29T19:45:00Z")})
    # Default max_age_days (14) before the current run's created_at.
    baseline_path, peer_path = _listing_paths(repo, ">=2026-09-15T19:39:56Z")
    responses = {
        f"repos/{repo}/actions/runs/900": current,
        baseline_path: {"workflow_runs": [older_main, same_tree]},
        peer_path: {"workflow_runs": [overlapping, untouching, same_tree]},
    }
    for run_id in (800, 700, 500):
        responses[f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"] = job_payload

    out = tmp_path / "history.json"
    result = subprocess.run(
        [
            "python3",
            str(FETCH_SCRIPT),
            "--repo",
            repo,
            "--run-id",
            "900",
            "--out",
            str(out),
            "--budget",
            str(_fetch_budget(tmp_path)),
            "--gh",
            str(_fake_gh(tmp_path, responses)),
            "--baseline-runs",
            "15",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(out.read_text())
    assert payload["schema_version"] == 1
    roles = {entry["run_id"]: entry["roles"] for entry in payload["runs"]}
    assert roles[800] == ["baseline"]
    assert roles[700] == ["peer"]
    assert roles[500] == ["same-tree"]
    assert 600 not in roles  # no overlap with the current run's window
    assert payload["runs"][0]["jobs"]["Tests"] == 300
    assert payload["cutoff"] == "2026-09-15T19:39:56+00:00"
    assert payload["dropped_stale_runs"] == 0


def _stale_pool_case(tmp_path: Path) -> tuple[dict, dict]:
    """The #2616 re-verify repro, reduced: a `main` listing that ignores the
    `created` filter and hands back runs from a retired pool alongside the
    current pool's runs."""
    repo = "owner/name"
    current = _wf_run(
        900, "current", "main", "2026-09-30T17:10:41Z", "2026-09-30T17:30:00Z"
    )
    hosted = [
        _wf_run(
            800 + i,
            f"hosted{i}",
            "main",
            f"2026-09-30T1{5 + i // 2}:{20 + i:02d}:00Z",
            f"2026-09-30T1{5 + i // 2}:{40 + i:02d}:00Z",
        )
        for i in range(3)
    ]
    # Created before the pool change -- one of them re-run AFTER it, which
    # must not launder it: a re-run executes its original commit's ci.yml.
    retired = [
        _wf_run(
            100 + i,
            f"old{i}",
            "main",
            "2026-09-30T16:00:00Z" if i == 0 else f"2026-09-15T0{i}:00:00Z",
            "2026-09-30T16:10:00Z" if i == 0 else f"2026-09-15T0{i}:10:00Z",
            created=f"2026-09-15T0{i}:00:00Z",
        )
        for i in range(4)
    ]
    undated = _wf_run(
        99, "undated", "main", "2026-09-30T16:00:00Z", "2026-09-30T16:05:00Z"
    )
    undated.pop("created_at")
    fast = _jobs_api({"Tests": ("2026-09-15T01:00:00Z", "2026-09-15T01:03:30Z")})
    slow = _jobs_api({"Tests": ("2026-09-30T15:20:00Z", "2026-09-30T15:26:00Z")})
    baseline_path, peer_path = _listing_paths(repo, ">=2026-09-30T15:06:07Z")
    responses: dict = {
        f"repos/{repo}/actions/runs/900": current,
        # Old runs FIRST: order is not a guarantee either.
        baseline_path: {"workflow_runs": [*retired, undated, *hosted]},
        peer_path: {"workflow_runs": []},
    }
    for run in retired + [undated]:
        responses[f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100"] = fast
    for run in hosted:
        responses[f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100"] = slow
    return responses, {r["id"] for r in hosted}


def test_fetcher_never_uses_a_run_from_before_the_pool_change(
    tmp_path: Path,
) -> None:
    """#2616 re-verify finding 1: a baseline from a retired pool falsely
    reddens a green run on the current one. Every run created before
    `history_not_before` is dropped client-side, whatever the listing returns
    and in whatever order."""
    responses, hosted_ids = _stale_pool_case(tmp_path)
    _, payload = _run_fetcher(
        tmp_path,
        responses,
        _fetch_budget(tmp_path, history_not_before="2026-09-30T15:06:07Z"),
    )
    assert {entry["run_id"] for entry in payload["runs"]} == hosted_ids
    assert all(entry["jobs"]["Tests"] == 360 for entry in payload["runs"])
    assert payload["cutoff"] == "2026-09-30T15:06:07+00:00"
    assert payload["dropped_stale_runs"] == 5  # four retired + one undated


def test_the_repo_budget_file_pins_the_2624_pool_change() -> None:
    """The shipped cutoff must be at or after #2624's merge -- the moment
    ci.yml left Blacksmith for GitHub-hosted runners."""
    rolling = json.loads(BUDGET_FILE.read_text())["rolling_window"]
    cutoff = datetime.datetime.fromisoformat(
        rolling["history_not_before"].replace("Z", "+00:00")
    )
    assert cutoff >= datetime.datetime(
        2026, 9, 30, 15, 6, 7, tzinfo=datetime.timezone.utc
    )
    assert isinstance(rolling["max_age_days"], int) and rolling["max_age_days"] > 0


def test_fetcher_drops_runs_older_than_max_age_days(tmp_path: Path) -> None:
    """Without a pool bound, the age bound alone keeps a stale listing out."""
    repo = "owner/name"
    current = _wf_run(
        900, "current", "main", "2026-09-30T12:00:00Z", "2026-09-30T12:20:00Z"
    )
    recent = _wf_run(
        801, "recent", "main", "2026-09-28T12:00:00Z", "2026-09-28T12:10:00Z"
    )
    ancient = _wf_run(
        101, "ancient", "main", "2026-09-10T12:00:00Z", "2026-09-10T12:10:00Z"
    )
    job_payload = _jobs_api({"Tests": ("2026-09-28T12:00:00Z", "2026-09-28T12:05:00Z")})
    baseline_path, peer_path = _listing_paths(repo, ">=2026-09-23T12:00:00Z")
    responses = {
        f"repos/{repo}/actions/runs/900": current,
        baseline_path: {"workflow_runs": [ancient, recent]},
        peer_path: {"workflow_runs": []},
        f"repos/{repo}/actions/runs/801/jobs?per_page=100": job_payload,
        f"repos/{repo}/actions/runs/101/jobs?per_page=100": job_payload,
    }
    _, payload = _run_fetcher(
        tmp_path, responses, _fetch_budget(tmp_path, max_age_days=7)
    )
    assert [entry["run_id"] for entry in payload["runs"]] == [801]
    assert payload["dropped_stale_runs"] == 1


def test_fetcher_history_excludes_cache_miss_rebuilds(tmp_path: Path) -> None:
    """#2619's cache-miss exclusion applies to the history too, or a cache
    outage inside the window would inflate the baseline the judged run --
    which already has its own rebuild time excluded -- is compared against."""
    repo = "owner/name"
    current = _wf_run(
        900, "current", "main", "2026-09-30T17:10:41Z", "2026-09-30T17:30:00Z"
    )
    prior = _wf_run(
        801, "prior", "main", "2026-09-30T16:00:00Z", "2026-09-30T16:20:00Z"
    )
    job = {
        "name": "Tests (Python 3.12)",
        "started_at": "2026-09-30T16:00:00Z",
        "completed_at": "2026-09-30T16:15:00Z",
        "conclusion": "success",
        "steps": [
            _step(
                "Cache pinned Yosys build",
                started="2026-09-30T16:00:10Z",
                completed="2026-09-30T16:00:30Z",
            ),
            _step(
                "Build + install pinned Yosys (cache miss only)",
                started="2026-09-30T16:00:30Z",
                completed="2026-09-30T16:08:50Z",
            ),
            _step(
                "Run pytest",
                started="2026-09-30T16:09:00Z",
                completed="2026-09-30T16:14:00Z",
            ),
        ],
    }
    baseline_path, peer_path = _listing_paths(repo, ">=2026-09-16T17:10:41Z")
    responses = {
        f"repos/{repo}/actions/runs/900": current,
        baseline_path: {"workflow_runs": [prior]},
        peer_path: {"workflow_runs": []},
        f"repos/{repo}/actions/runs/801/jobs?per_page=100": {"jobs": [job]},
    }
    _, payload = _run_fetcher(tmp_path, responses, _fetch_budget(tmp_path))
    # 900 s raw, minus the 500 s rebuild and the 20 s cache restore.
    assert payload["runs"][0]["jobs"]["Tests (Python 3.12)"] == 380


def test_fetcher_never_fails_the_build_when_the_api_is_unreachable(
    tmp_path: Path,
) -> None:
    """An Actions API hiccup must degrade the check to its committed ceilings,
    never redden CI: that would be the same untrusted-gate failure #2615 is
    about, in a new place."""
    out = tmp_path / "history.json"
    result = subprocess.run(
        [
            "python3",
            str(FETCH_SCRIPT),
            "--repo",
            "owner/name",
            "--run-id",
            "900",
            "--out",
            str(out),
            "--gh",
            str(tmp_path / "does-not-exist"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(out.read_text())
    assert payload["runs"] == []
    assert payload["error"]
    # And the check accepts that payload rather than exiting 2 on it.
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Slow job",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:00:22Z",
            )
        ],
    )
    check = _run(
        "--jobs-json",
        str(jobs),
        "--budget",
        str(_budget(tmp_path)),
        "--history-json",
        str(out),
        "--format",
        "json",
    )
    assert check.returncode == EXIT_OK, check.stdout + check.stderr
    assert json.loads(check.stdout)["model"]["kind"] == "fixed"


def test_fetcher_never_fails_on_a_malformed_rolling_window_override(
    tmp_path: Path,
) -> None:
    """A hand-edit typo in the committed budget file's `rolling_window` block
    (e.g. a quoted number) must degrade to the default for that key, not
    crash with an uncaught `TypeError` -- the same "never fails the build"
    guarantee `test_fetcher_never_fails_the_build_when_the_api_is_unreachable`
    covers for an unreachable API, here for a malformed *local* config."""
    repo = "owner/name"
    current = _wf_run(
        900, "current", "feature/x", "2026-09-29T19:39:56Z", "2026-09-29T20:00:04Z"
    )
    baseline_path, peer_path = _listing_paths(repo, ">=2026-09-15T19:39:56Z")
    responses = {
        f"repos/{repo}/actions/runs/900": current,
        baseline_path: {"workflow_runs": []},
        peer_path: {"workflow_runs": []},
    }
    bad_budget = tmp_path / "bad-budget.json"
    bad_budget.write_text(json.dumps({"rolling_window": {"baseline_runs": "15"}}))

    out = tmp_path / "history.json"
    result = subprocess.run(
        [
            "python3",
            str(FETCH_SCRIPT),
            "--repo",
            repo,
            "--run-id",
            "900",
            "--out",
            str(out),
            "--budget",
            str(bad_budget),
            "--gh",
            str(_fake_gh(tmp_path, responses)),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(out.read_text())
    assert payload["error"] is None
    assert payload["runs"] == []


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


def test_ci_workflow_feeds_the_rolling_window(tmp_path: Path) -> None:
    """Without the history the check silently degrades to its committed
    ceilings -- a gate that has stopped gating, which is exactly the failure
    #2615 exists to end. So the fetch step and the flag that consumes it are
    both part of the wiring contract."""
    workflow = WORKFLOW.read_text()
    assert "scripts/fetch_ci_wall_clock_history.py" in workflow
    assert "--history-json" in workflow
    assert "actions: read" in workflow


def test_repo_budget_files_rolling_window_block_is_accepted(tmp_path: Path) -> None:
    """The shipped tuning block must survive the check's own validation --
    including its rejection of unknown keys, which is what turns a typo into a
    failure rather than a silent fallback to defaults."""
    budget = json.loads(BUDGET_FILE.read_text())
    rolling = budget["rolling_window"]
    assert rolling["enabled"] is True
    assert rolling["baseline_branch"] == "main"
    jobs = _jobs(
        tmp_path,
        [
            _job(
                "Lint (ruff)",
                started="2026-09-17T07:00:10Z",
                completed="2026-09-17T07:00:22Z",
            )
        ],
    )
    result = _run("--jobs-json", str(jobs), "--budget", str(BUDGET_FILE))
    assert result.returncode == EXIT_OK, result.stdout + result.stderr


def test_cache_miss_step_naming_convention_still_holds_in_ci_yml() -> None:
    """The cache-miss exclusion (#2617) matches on a *step-name convention*,
    so the convention is load-bearing: renaming those steps in `ci.yml`
    without updating the script would silently put rebuild time back into the
    compute figure and re-create the false-red this check was fixed for."""
    workflow = WORKFLOW.read_text()
    script = SCRIPT.read_text()
    # Mirrored from scripts/check_ci_wall_clock.py's CACHE_MISS_STEP_SUFFIX /
    # CACHE_STEP_PREFIXES.
    assert '" (cache miss only)"' in script
    assert '"Cache ", "Post Cache "' in script
    # Yosys, SymbiYosys/Bitwuzla, Icarus Verilog, Verilator.
    assert workflow.count("(cache miss only)") >= 4
    # Each rebuild is gated on its `actions/cache` step's `cache-hit` output,
    # and that step's name is what the restore/save exclusion matches on.
    assert "uses: actions/cache@" in workflow
    assert workflow.count("name: Cache pinned ") >= 4


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
