"""`actions/checkout` time is fixed setup, not compute (issue #5964).

Split out of `tests/test_ci_wall_clock.py` (size ratchet, #4751): that file
sits at its `file-size-baseline.json` ceiling, so #5964's tests live here
alongside the step-name convention guards the move pushed out of it -- both
conventions now live in `scripts/ci_wall_clock_steps.py`, shared by the
checker and the history fetcher.

The scenario these tests pin is PR #5942's attempt 1: a job whose
`actions/checkout` stalled at 313 s on one runner (siblings: 4-6 s in the
same run) while its actual cargo work was ~29 s -- reported as a "compute
breach". Checkout is excluded from every job's compute figure always; a
figure of 60 s or more is named as a stall; a slow pytest/cargo step still
breaches.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_ci_wall_clock.py"
STEPS_MODULE = REPO_ROOT / "scripts" / "ci_wall_clock_steps.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from fetch_ci_wall_clock_history import job_durations  # noqa: E402

# Exit-code contract, mirroring scripts/check-release-lag.sh's tiering.
EXIT_OK = 0
EXIT_BREACH = 1


def _env() -> dict[str, str]:
    # Never inherit a real GITHUB_STEP_SUMMARY (these tests can themselves
    # run inside GitHub Actions).
    return {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(SCRIPT), *args], capture_output=True, text=True, env=_env()
    )


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload))
    return path


def _job(
    name: str,
    *,
    started: str,
    completed: str,
    conclusion: str = "success",
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
    started: str | None = None,
    completed: str | None = None,
    conclusion: str = "success",
) -> dict:
    """One entry of a job's `steps[]`, as the `/jobs` API returns it."""
    step: dict = {"name": name, "conclusion": conclusion}
    if started is not None:
        step["started_at"] = started
    if completed is not None:
        step["completed_at"] = completed
    return step


def _checkout_job(
    *, checkout_from: str, checkout_to: str, pytest_from: str, pytest_to: str
) -> dict:
    """PR #5942's breaching job, reduced: a 313 s checkout then real work."""
    return _job(
        "Native engines (Rust) — waveform build/query",
        started=checkout_from,
        completed=pytest_to,
        steps=[
            _step(
                "Run actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                started=checkout_from,
                completed=checkout_to,
            ),
            _step("cargo test", started=pytest_from, completed=pytest_to),
        ],
    )


def _budget(tmp_path: Path, **overrides) -> Path:
    budget = {
        "schema_version": 1,
        "exclude_jobs": ["CI wall-clock budget"],
        "default_job_budget_seconds": 260,
        "total_job_budget_seconds": 1500,
        "run_wall_clock_budget_seconds": 1800,
        "jobs": {},
    }
    budget.update(overrides)
    return _write(tmp_path / "budget.json", budget)


def _args(tmp_path: Path, job: dict, *extra: str) -> list[str]:
    return [
        "--jobs-json",
        str(_write(tmp_path / "jobs.json", {"total_count": 1, "jobs": [job]})),
        "--run-json",
        str(_write(tmp_path / "run.json", {"run_started_at": job["started_at"]})),
        "--budget",
        str(_budget(tmp_path)),
        *extra,
    ]


# --------------------------------------------------------------------------
# The exclusion itself
# --------------------------------------------------------------------------


def test_checkout_stall_does_not_breach_the_budget(tmp_path: Path) -> None:
    """AC: a job whose *only* overage is a 313 s checkout plus ~40 s of work
    passes a 260 s budget, and the report names the stall instead of blaming
    the code. This is PR #5942 attempt 1's job, with the stall kept."""
    job = _checkout_job(
        checkout_from="2026-11-16T19:15:12Z",
        checkout_to="2026-11-16T19:20:25Z",  # 313 s of checkout
        pytest_from="2026-11-16T19:20:25Z",
        pytest_to="2026-11-16T19:21:05Z",  # 40 s of real work
    )
    result = _run(*_args(tmp_path, job))
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert "CHECKOUT STALL" in result.stdout
    assert "actions/checkout" in result.stdout
    assert "313s checkout, excluded" in result.stdout
    assert "OK: every job" in result.stdout


def test_a_slow_pytest_step_alongside_a_stall_still_breaches(tmp_path: Path) -> None:
    """AC: the exclusion is not an allowance. Same 313 s checkout, but pytest
    itself has grown to 300 s -- 300 s of compute against a 260 s budget
    still breaches, and the guidance says checkout has already been
    discounted."""
    job = _checkout_job(
        checkout_from="2026-11-16T19:15:12Z",
        checkout_to="2026-11-16T19:20:25Z",  # 313 s of checkout
        pytest_from="2026-11-16T19:20:25Z",
        pytest_to="2026-11-16T19:25:25Z",  # 300 s: the genuine regression
    )
    result = _run(*_args(tmp_path, job))
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr
    assert "300s exceeds the 260s budget" in result.stdout
    assert "CHECKOUT STALL" in result.stdout
    assert "ALREADY excluded" in result.stdout


def test_a_normal_checkout_is_excluded_but_names_no_stall(tmp_path: Path) -> None:
    """Checkout is excluded *always* (#5964), not only when it stalls -- but
    a 5 s checkout is not worth naming, so no stall block appears."""
    job = _checkout_job(
        checkout_from="2026-11-16T19:15:12Z",
        checkout_to="2026-11-16T19:15:17Z",  # 5 s, the sibling jobs' norm
        pytest_from="2026-11-16T19:15:17Z",
        pytest_to="2026-11-16T19:15:57Z",
    )
    result = _run(*_args(tmp_path, job, "--format", "json"))
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    by_name = {j["name"]: j for j in payload["jobs"]}[job["name"]]
    assert by_name["checkout_seconds"] == 5
    assert by_name["seconds"] == 40  # 45 s wall minus the 5 s checkout
    assert "CHECKOUT STALL" not in result.stdout


def test_a_job_without_steps_excludes_nothing(tmp_path: Path) -> None:
    """No `steps[]`, no exclusion -- a payload that cannot see steps is
    measured whole, exactly as before #5964 (degrade to "subtract nothing")."""
    job = _job(
        "Opaque job",
        started="2026-11-16T19:15:12Z",
        completed="2026-11-16T19:20:12Z",  # 300 s wall, no steps[]
    )
    result = _run(*_args(tmp_path, job))
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr
    assert "300s exceeds the 260s budget" in result.stdout


def test_skipped_and_timestampless_checkouts_exclude_nothing(
    tmp_path: Path,
) -> None:
    """A `skipped` checkout never ran; a checkout with no timestamps cannot
    be measured -- only the one running step with parseable times is
    excluded."""
    job = _job(
        "Edge job",
        started="2026-11-16T19:15:12Z",
        completed="2026-11-16T19:20:57Z",
        steps=[
            _step(
                "Run actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                conclusion="skipped",
            ),
            _step("Run actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"),
            _step(
                "Run actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                started="2026-11-16T19:15:12Z",
                completed="2026-11-16T19:15:17Z",
            ),
            _step(
                "pytest",
                started="2026-11-16T19:15:17Z",
                completed="2026-11-16T19:20:57Z",  # 340 s of real work
            ),
        ],
    )
    result = _run(*_args(tmp_path, job, "--format", "json"))
    assert result.returncode == EXIT_BREACH, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    by_name = {j["name"]: j for j in payload["jobs"]}[job["name"]]
    assert by_name["checkout_seconds"] == 5
    assert by_name["seconds"] == 340
    assert payload["breaches"][0]["actual_seconds"] == 340


def test_checkout_and_cache_miss_together_clamp_to_the_job(tmp_path: Path) -> None:
    """Both exclusions sum into a slice clamped to the job's own seconds, so
    the compute figure can never go negative: a 360 s job with a 302 s
    cache-miss slice (rebuild + restore) and a 300 s checkout reports 302 s
    cache, 58 s checkout, 0 s compute -- and passes."""
    job = _job(
        "Slow job",
        started="2026-09-30T07:00:00Z",
        completed="2026-09-30T07:06:00Z",  # 360 s job
        steps=[
            _step(
                "Run actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                started="2026-09-30T07:00:00Z",
                completed="2026-09-30T07:05:00Z",  # 300 s checkout
            ),
            _step(
                "Cache pinned Verilator build",
                started="2026-09-30T07:00:00Z",
                completed="2026-09-30T07:00:02Z",
            ),
            _step(
                "Build + install pinned Verilator (cache miss only)",
                started="2026-09-30T07:00:02Z",
                completed="2026-09-30T07:05:02Z",  # 300 s rebuild
            ),
            _step(
                "pytest",
                started="2026-09-30T07:05:02Z",
                completed="2026-09-30T07:05:22Z",  # 20 s of real work
            ),
        ],
    )
    result = _run(*_args(tmp_path, job, "--format", "json"))
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    by_name = {j["name"]: j for j in payload["jobs"]}[job["name"]]
    assert by_name["cache_miss_seconds"] == 302  # 300 s rebuild + 2 s restore
    assert by_name["checkout_seconds"] == 58  # the clamp: 360 - 302
    assert by_name["seconds"] == 0
    assert by_name["total_seconds"] == 360  # what the GitHub UI shows


def test_json_reports_the_checkout_fields_additively(tmp_path: Path) -> None:
    """`checkout_seconds` is additive per job and run-level; no existing
    field changes meaning, and `total_seconds` still reconciles with the UI."""
    job = _checkout_job(
        checkout_from="2026-11-16T19:15:12Z",
        checkout_to="2026-11-16T19:20:25Z",
        pytest_from="2026-11-16T19:20:25Z",
        pytest_to="2026-11-16T19:21:05Z",
    )
    result = _run(*_args(tmp_path, job, "--format", "json"))
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    by_name = {j["name"]: j for j in payload["jobs"]}[job["name"]]
    assert by_name["checkout_seconds"] == 313
    assert by_name["seconds"] == 40
    assert by_name["total_seconds"] == 353
    assert payload["total_checkout_seconds"] == 313
    assert payload["total_job_seconds"] == 40


def test_a_checkout_stall_is_annotated_as_a_notice(tmp_path: Path) -> None:
    """Like a cache miss (#2617), a stall is infra the author did not cause:
    `::notice::`, never an error, even though the exclusion is silent
    arithmetic in the compute figure."""
    job = _checkout_job(
        checkout_from="2026-11-16T19:15:12Z",
        checkout_to="2026-11-16T19:20:25Z",
        pytest_from="2026-11-16T19:20:25Z",
        pytest_to="2026-11-16T19:21:05Z",
    )
    result = _run(*_args(tmp_path, job, "--annotate"))
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert "::notice" in result.stdout
    assert "title=CI checkout stall" in result.stdout
    assert "313s" in result.stdout
    assert "::error" not in result.stdout


# --------------------------------------------------------------------------
# The history fetcher applies the same subtraction
# --------------------------------------------------------------------------


def test_fetcher_applies_the_same_checkout_subtraction(tmp_path: Path) -> None:
    """The rolling baseline must stay the same kind of number as the judged
    run: `job_durations` subtracts checkout (#5964) exactly like it subtracts
    cache-miss rebuilds (#2617)."""
    job = _checkout_job(
        checkout_from="2026-11-16T19:15:12Z",
        checkout_to="2026-11-16T19:20:25Z",
        pytest_from="2026-11-16T19:20:25Z",
        pytest_to="2026-11-16T19:21:05Z",
    )
    now = datetime(2026, 11, 16, 20, 0, tzinfo=timezone.utc)
    durations, partial = job_durations({"jobs": [job]}, now=now)
    assert durations == {job["name"]: 40}
    assert partial == []


# --------------------------------------------------------------------------
# Step-name conventions (load-bearing; moved from tests/test_ci_wall_clock.py)
# --------------------------------------------------------------------------


def test_cache_miss_step_naming_convention_still_holds_in_ci_yml() -> None:
    """The cache-miss exclusion (#2617) matches on a *step-name convention*,
    so the convention is load-bearing: renaming those steps in `ci.yml`
    without updating the module would silently put rebuild time back into the
    compute figure and re-create the false-red this check was fixed for."""
    workflow = WORKFLOW.read_text()
    module = STEPS_MODULE.read_text()
    # Mirrored from scripts/ci_wall_clock_steps.py's CACHE_MISS_STEP_SUFFIX /
    # CACHE_STEP_PREFIXES.
    assert '" (cache miss only)"' in module
    assert '"Cache ", "Post Cache "' in module
    # Yosys, SymbiYosys/Bitwuzla, Icarus Verilog, Verilator.
    assert workflow.count("(cache miss only)") >= 4
    # Each rebuild is gated on its `actions/cache` step's `cache-hit` output,
    # and that step's name is what the restore/save exclusion matches on.
    assert "uses: actions/cache@" in workflow
    assert workflow.count("name: Cache pinned ") >= 4


def test_checkout_step_naming_convention_still_holds_in_ci_yml() -> None:
    """Same for the checkout exclusion (#5964): the module matches GitHub's
    auto-generated names for a bare `- uses: actions/checkout@<sha>` step
    (confirmed against a recorded `/jobs` payload, run 43967976263). Renaming
    the workflow's usage away from the bare form must be mirrored there."""
    workflow = WORKFLOW.read_text()
    module = STEPS_MODULE.read_text()
    assert '"Run actions/checkout"' in module
    assert '"Post Run actions/checkout"' in module
    assert "uses: actions/checkout@" in workflow
    assert workflow.count("- uses: actions/checkout@") >= 4
