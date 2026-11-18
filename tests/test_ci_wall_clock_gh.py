"""`GhClient`'s bounded retry of transient Actions-API failures (#6214).

Split into its own file the way `tests/test_ci_wall_clock_checkout.py` was
(#4751): `tests/test_ci_wall_clock.py` sits at its `file-size-baseline.json`
ceiling, so #6214's tests live here, and the retried client itself moved to
`scripts/ci_wall_clock_gh.py` for the same reason.

The pinned scenario is run 44058611659's history fetch (PR #6006, attempt 1):
one transient HTTP 502 -- `gh: Server Error (HTTP 502)`, no retry -- made the
wall-clock gate fall back to committed ceilings ~2x below the workload the
repo runs, and every `Tests (Python 3.1x)` leg breached on an unrelated PR.
These tests pin the fix: a 502 that clears returns the payload, a 502 that
persists raises `FetchError` after the bound (and the fetcher still exits 0
with an empty history), and a 403/404 is never retried. All against fake `gh`
executables -- never a live Actions API.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

REPO_ROOT = Path(__file__).parent.parent
FETCH_SCRIPT = REPO_ROOT / "scripts" / "fetch_ci_wall_clock_history.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from ci_wall_clock_gh import (  # noqa: E402
    RETRY_ATTEMPTS,
    FetchError,
    GhClient,
)

SERVER_ERROR_502 = "gh: Server Error (HTTP 502)"


def _flaky_gh(tmp_path: Path, failures: int, stderr: str) -> Path:
    """A fake `gh` that fails its first `failures` call(s) with `stderr`
    (exit 1), then answers every call with `{"ok": true}`."""
    remaining = tmp_path / "remaining"
    remaining.write_text(str(failures))
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"remaining = {str(remaining)!r}\n"
        "left = int(open(remaining).read())\n"
        "if left > 0:\n"
        "    open(remaining, 'w').write(str(left - 1))\n"
        f"    sys.stderr.write({stderr!r})\n"
        "    sys.exit(1)\n"
        "print('{\"ok\": true}')\n"
    )
    gh.chmod(0o755)
    return gh


def _gh_that_prints(tmp_path: Path, stdout: str) -> Path:
    """A fake `gh` whose every call exits 0 printing `stdout` verbatim."""
    gh = tmp_path / "gh"
    gh.write_text(f"#!/usr/bin/env python3\nprint({stdout!r})\n")
    gh.chmod(0o755)
    return gh


def _gh_that_hangs(tmp_path: Path) -> Path:
    gh = tmp_path / "gh"
    gh.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n")
    gh.chmod(0o755)
    return gh


def test_a_transient_502_is_retried_until_it_succeeds(tmp_path: Path) -> None:
    client = GhClient(
        str(_flaky_gh(tmp_path, 1, SERVER_ERROR_502)), 30, 40, retry_sleep=0
    )
    assert client.get("repos/owner/name/actions/runs/900") == {"ok": True}
    assert client.calls == 2  # one 502, one success


def test_a_persistent_502_raises_fetcherror_after_the_bound(tmp_path: Path) -> None:
    client = GhClient(
        str(_flaky_gh(tmp_path, 99, SERVER_ERROR_502)), 30, 40, retry_sleep=0
    )
    with pytest.raises(FetchError) as raised:
        client.get("repos/owner/name/actions/runs/900")
    assert "HTTP 502" in str(raised.value)
    assert client.calls == RETRY_ATTEMPTS


@pytest.mark.parametrize(
    "stderr",
    [
        "gh: Not Found (HTTP 404)",
        "gh: API rate limit exceeded for installation (HTTP 403)",
    ],
)
def test_client_errors_are_not_retried(tmp_path: Path, stderr: str) -> None:
    client = GhClient(str(_flaky_gh(tmp_path, 99, stderr)), 30, 40, retry_sleep=0)
    with pytest.raises(FetchError) as raised:
        client.get("repos/owner/name/actions/runs/900")
    assert "exited 1" in str(raised.value)
    assert client.calls == 1


def test_retries_are_charged_to_the_api_call_budget(tmp_path: Path) -> None:
    """A retry is another API call like any other: the `--max-api-calls`
    budget must bound the step even when every call is transient-failing,
    not be multiplied by the retry bound."""
    client = GhClient(
        str(_flaky_gh(tmp_path, 99, SERVER_ERROR_502)), 30, 2, retry_sleep=0
    )
    with pytest.raises(FetchError) as raised:
        client.get("repos/owner/name/actions/runs/900")
    assert "API call budget of 2 exhausted" in str(raised.value)
    assert client.calls == 2


def test_non_json_output_is_not_retried(tmp_path: Path) -> None:
    client = GhClient(str(_gh_that_prints(tmp_path, "<html>")), 30, 40, retry_sleep=0)
    with pytest.raises(FetchError) as raised:
        client.get("repos/owner/name/actions/runs/900")
    assert "non-JSON" in str(raised.value)
    assert client.calls == 1


def test_a_hung_call_times_out_and_is_retried(tmp_path: Path) -> None:
    client = GhClient(str(_gh_that_hangs(tmp_path)), 1, 40, retry_sleep=0)
    with pytest.raises(FetchError):
        client.get("repos/owner/name/actions/runs/900")
    assert client.calls == RETRY_ATTEMPTS


def test_a_missing_gh_is_permanent_not_retried(tmp_path: Path) -> None:
    """A nonexistent executable is a local misconfiguration, not a
    transient API failure: it must stay one fast attempt, or
    `test_fetcher_never_fails_the_build_when_the_api_is_unreachable`
    would pay the backoff for nothing."""
    client = GhClient(str(tmp_path / "no-such-gh"), 30, 40, retry_sleep=2)
    with pytest.raises(FetchError):
        client.get("repos/owner/name/actions/runs/900")
    assert client.calls == 1


# --------------------------------------------------------------------------
# Through the fetcher itself: the #6214 repro, and the never-fatal contract
# --------------------------------------------------------------------------


def _wf_run(run_id: int, sha: str, branch: str, start: str, end: str) -> dict:
    return {
        "id": run_id,
        "head_sha": sha,
        "head_branch": branch,
        "status": "completed",
        "conclusion": "success",
        "created_at": start,
        "run_started_at": start,
        "updated_at": end,
    }


def _table_gh(tmp_path: Path, responses: dict[str, object], flaky: str) -> Path:
    """A fake `gh` answering `gh api <path>` from a JSON map, except that
    the one `flaky` path fails its **first** call with the 502 gh printed
    in run 44058611659's log, then answers from the map like any other."""
    table = tmp_path / "responses.json"
    table.write_text(json.dumps(responses))
    state = tmp_path / "flaky-left"
    state.write_text("1")
    gh = tmp_path / "table-gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"table = json.loads(open({str(table)!r}).read())\n"
        f"flaky, state = {flaky!r}, {str(state)!r}\n"
        "path = sys.argv[-1]\n"
        "if path == flaky and int(open(state).read()) > 0:\n"
        "    open(state, 'w').write('0')\n"
        f"    sys.stderr.write({SERVER_ERROR_502!r})\n"
        "    sys.exit(1)\n"
        "if path not in table:\n"
        "    sys.stderr.write('no such path: ' + path)\n"
        "    sys.exit(1)\n"
        "print(json.dumps(table[path]))\n"
    )
    gh.chmod(0o755)
    return gh


def _run_fetcher(
    tmp_path: Path, gh: Path, *extra: str
) -> tuple[subprocess.CompletedProcess, dict]:
    budget = tmp_path / "budget.json"
    budget.write_text(json.dumps({"rolling_window": {"history_not_before": ""}}))
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
            str(gh),
            "--baseline-runs",
            "15",
            *extra,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result, json.loads(out.read_text())


def test_one_transient_502_does_not_lose_the_rolling_model(tmp_path: Path) -> None:
    """The #6214 repro, reduced: the current-run API call 502s exactly
    once (as run 44058611659's did), and the history must still come out
    complete -- not the empty fallback payload that reddened PR #6006."""
    repo = "owner/name"
    current = _wf_run(
        900, "current", "feature/x", "2026-09-29T19:39:56Z", "2026-09-29T20:00:04Z"
    )
    older_main = _wf_run(
        800, "older", "main", "2026-09-29T14:00:00Z", "2026-09-29T14:10:00Z"
    )
    jobs = {
        "jobs": [
            {
                "name": "Tests",
                "started_at": "2026-09-29T19:40:00Z",
                "completed_at": "2026-09-29T19:45:00Z",
                "conclusion": "success",
            }
        ]
    }
    created = "&created=" + quote(">=2026-09-15T19:39:56Z")
    listing = f"repos/{repo}/actions/workflows/ci.yml/runs"
    responses = {
        f"repos/{repo}/actions/runs/900": current,
        f"{listing}?branch=main&status=completed&per_page=20{created}": {
            "workflow_runs": [older_main]
        },
        f"{listing}?per_page=40{created}": {"workflow_runs": []},
        f"repos/{repo}/actions/runs/800/jobs?per_page=100": jobs,
    }
    result, payload = _run_fetcher(
        tmp_path,
        _table_gh(tmp_path, responses, f"repos/{repo}/actions/runs/900"),
        "--retry-sleep",
        "0",
    )
    assert payload["error"] is None
    assert "history unavailable" not in result.stdout
    assert [(e["run_id"], e["roles"]) for e in payload["runs"]] == [(800, ["baseline"])]
    assert payload["runs"][0]["jobs"]["Tests"] == 300


def test_a_persistent_502_still_yields_an_empty_history_and_exit_zero(
    tmp_path: Path,
) -> None:
    """Retries change how hard the fetcher tries, never its contract: a
    failure that survives them is still an empty history, a warning, and
    exit 0 -- the check falls back to committed ceilings, it does not
    redden the build (#2615's guarantee, restated for #6214)."""
    result, payload = _run_fetcher(
        tmp_path,
        _flaky_gh(tmp_path, 99, SERVER_ERROR_502),
        "--retry-sleep",
        "0",
    )
    assert payload["runs"] == []
    assert "HTTP 502" in payload["error"]
    assert "history unavailable" in result.stdout
