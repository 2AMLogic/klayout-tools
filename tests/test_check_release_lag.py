"""Tests for `scripts/check-release-lag.sh` (issue #1020): the advisory
release-cadence check that reports how far `main` has drifted ahead of the
latest tagged release.

Most tests run against synthetic, throwaway git repos built under `tmp_path`
-- never against this repo's own history -- mirroring
`tests/test_generate_deck_history.py`'s approach, so the above/below-threshold
and no-tags cases are fully controlled. Two tests deliberately *do* point the
script at this repo: one asserts the backstop is still parseable from the
checked-in `RELEASING.md` (the anti-drift guarantee the script's threshold
sourcing depends on), and one asserts the CI wiring stays advisory.

The last-failed-run lookup (issue #6911) is exercised through a stub `gh`
on PATH: a failed newest run is named with a link, a superseded or empty
history is not, and unavailable history changes nothing about the local lag,
the JSON shape, or the exit codes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-release-lag.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

# Mirrors the phrase check-release-lag.sh parses out of RELEASING.md's
# "Release cadence" section. Written independently here on purpose: if either
# side drifts, test_threshold_is_parseable_from_this_repos_releasing_md fails.
THRESHOLD_RE = re.compile(r"more than \*\*(\d+) commits\*\*")


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    if env is None:
        # Never inherit a real GITHUB_STEP_SUMMARY (these tests can themselves
        # run inside GitHub Actions) -- only the test that asserts on it sets
        # it. Same for GH_TOKEN/GITHUB_TOKEN: with a real token the
        # last-failed-run lookup (issue #6911) would hit the live API; only
        # the tests that install a stub `gh` set one.
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("GITHUB_STEP_SUMMARY", "GH_TOKEN", "GITHUB_TOKEN")
        }
    return subprocess.run([str(SCRIPT), *args], capture_output=True, text=True, env=env)


def _commit(repo: Path, message: str) -> None:
    (repo / "file.txt").write_text(f"{message}\n")
    _git("add", "file.txt", cwd=repo)
    _git("commit", "-q", "-m", message, cwd=repo)


def _make_repo(tmp_path: Path, *, threshold: int | None = 25) -> Path:
    """A throwaway repo with one commit, optionally carrying a RELEASING.md
    whose cadence wording matches what the script parses."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    if threshold is not None:
        (repo / "RELEASING.md").write_text(
            "# Releasing\n\n## Release cadence\n\n"
            "2. **Commit-count backstop** -- `main` is more than "
            f"**{threshold} commits** ahead of the latest tag.\n"
        )
    _commit(repo, "initial")
    return repo


def _tagged_repo(tmp_path: Path, *, ahead: int, threshold: int | None = 25) -> Path:
    """A repo tagged `v1.0.0` with `ahead` commits on top of that tag."""
    repo = _make_repo(tmp_path, threshold=threshold)
    _git("tag", "v1.0.0", cwd=repo)
    for i in range(ahead):
        _commit(repo, f"commit {i}")
    return repo


def _json_report(repo: Path, *extra: str) -> tuple[dict, int]:
    proc = _run("--repo", str(repo), "--ref", "HEAD", "--format", "json", *extra)
    assert proc.stdout, f"no stdout; stderr={proc.stderr}"
    return json.loads(proc.stdout), proc.returncode


# --------------------------------------------------------------------------
# Above / below the backstop
# --------------------------------------------------------------------------


def test_over_threshold_reports_release_due_and_exits_nonzero(tmp_path: Path) -> None:
    repo = _tagged_repo(tmp_path, ahead=6, threshold=5)
    report, code = _json_report(repo)
    assert code == 1
    assert report["status"] == "release_due"
    assert report["commits_ahead"] == 6
    assert report["threshold"] == 5
    assert report["latest_tag"] == "v1.0.0"
    assert "a release is due" in report["message"]


def test_under_threshold_reports_ok_and_exits_zero(tmp_path: Path) -> None:
    repo = _tagged_repo(tmp_path, ahead=2, threshold=5)
    report, code = _json_report(repo)
    assert code == 0
    assert report["status"] == "ok"
    assert report["commits_ahead"] == 2


def test_exactly_at_threshold_is_not_over(tmp_path: Path) -> None:
    """RELEASING.md says "*more than* N commits", so N itself is still clean."""
    repo = _tagged_repo(tmp_path, ahead=5, threshold=5)
    report, code = _json_report(repo)
    assert code == 0
    assert report["status"] == "ok"
    assert report["commits_ahead"] == 5


def test_no_commits_since_tag_is_clean(tmp_path: Path) -> None:
    repo = _tagged_repo(tmp_path, ahead=0, threshold=5)
    report, code = _json_report(repo)
    assert code == 0
    assert report["status"] == "ok"
    assert report["commits_ahead"] == 0


# --------------------------------------------------------------------------
# Edge cases
# --------------------------------------------------------------------------


def test_no_tags_at_all_is_handled_gracefully(tmp_path: Path) -> None:
    """A repo that has never been tagged must not crash or claim a lag."""
    repo = _make_repo(tmp_path)
    report, code = _json_report(repo)
    assert code == 0
    assert report["status"] == "no_tags"
    assert report["latest_tag"] is None
    assert report["commits_ahead"] is None
    assert "no release has been cut yet" in report["message"]


def test_default_ref_falls_back_when_origin_main_is_absent(tmp_path: Path) -> None:
    """The synthetic repos have no remote, so origin/main does not resolve --
    the check degrades to main rather than erroring out."""
    repo = _tagged_repo(tmp_path, ahead=1, threshold=5)
    proc = _run("--repo", str(repo), "--format", "json")
    report = json.loads(proc.stdout)
    assert proc.returncode == 0
    assert report["ref"] == "main"


def test_non_git_directory_exits_two(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    proc = _run("--repo", str(plain))
    assert proc.returncode == 2
    assert "not a git repository" in proc.stderr


def test_unknown_ref_exits_two(tmp_path: Path) -> None:
    repo = _tagged_repo(tmp_path, ahead=1, threshold=5)
    proc = _run("--repo", str(repo), "--ref", "refs/heads/nope")
    assert proc.returncode == 2


def test_unknown_format_exits_two(tmp_path: Path) -> None:
    proc = _run("--format", "yaml")
    assert proc.returncode == 2
    assert "unknown --format" in proc.stderr


# --------------------------------------------------------------------------
# Threshold sourcing (the anti-drift requirement)
# --------------------------------------------------------------------------


def test_threshold_comes_from_the_repos_releasing_md(tmp_path: Path) -> None:
    repo = _tagged_repo(tmp_path, ahead=8, threshold=7)
    report, code = _json_report(repo)
    assert report["threshold"] == 7
    assert report["threshold_source"] == "RELEASING.md"
    assert code == 1


def test_missing_releasing_md_exits_two(tmp_path: Path) -> None:
    repo = _tagged_repo(tmp_path, ahead=1, threshold=None)
    proc = _run("--repo", str(repo), "--ref", "HEAD")
    assert proc.returncode == 2
    assert "--threshold" in proc.stderr


def test_unparseable_releasing_md_exits_two_rather_than_guessing(
    tmp_path: Path,
) -> None:
    """If the cadence wording drifts, the check must fail loudly instead of
    falling back to a stale hardcoded number."""
    repo = _tagged_repo(tmp_path, ahead=1, threshold=None)
    (repo / "RELEASING.md").write_text("# Releasing\n\nNo cadence rule here.\n")
    proc = _run("--repo", str(repo), "--ref", "HEAD")
    assert proc.returncode == 2
    assert "could not parse" in proc.stderr


def test_explicit_threshold_overrides_the_document(tmp_path: Path) -> None:
    repo = _tagged_repo(tmp_path, ahead=3, threshold=100)
    report, code = _json_report(repo, "--threshold", "2")
    assert report["threshold"] == 2
    assert report["threshold_source"] == "--threshold"
    assert code == 1


def test_threshold_is_parseable_from_this_repos_releasing_md() -> None:
    """The script's parser and RELEASING.md's actual wording must stay in sync
    -- this is what keeps the policy and the check from drifting apart."""
    text = (REPO_ROOT / "RELEASING.md").read_text()
    match = THRESHOLD_RE.search(text)
    assert match is not None, (
        "RELEASING.md no longer states the backstop as "
        '"more than **N commits**" -- update scripts/check-release-lag.sh\'s '
        "parser (and this test) together with the policy wording"
    )
    documented = int(match.group(1))

    proc = _run("--repo", str(REPO_ROOT), "--ref", "HEAD", "--format", "json")
    assert proc.returncode in (0, 1), f"unexpected exit; stderr={proc.stderr}"
    report = json.loads(proc.stdout)
    assert report["threshold"] == documented
    assert report["threshold_source"] == "RELEASING.md"


# --------------------------------------------------------------------------
# Reporting surfaces
# --------------------------------------------------------------------------


def test_text_report_points_at_pending_changelog_entries(
    tmp_path: Path,
) -> None:
    repo = _tagged_repo(tmp_path, ahead=9, threshold=5)
    proc = _run("--repo", str(repo), "--ref", "HEAD")
    assert proc.returncode == 1
    assert "changelog.d/" in proc.stdout
    assert "## Unreleased" in proc.stdout


def test_annotation_is_a_warning_when_a_release_is_due(tmp_path: Path) -> None:
    repo = _tagged_repo(tmp_path, ahead=9, threshold=5)
    proc = _run("--repo", str(repo), "--ref", "HEAD", "--annotate")
    assert proc.returncode == 1
    assert "::warning file=RELEASING.md" in proc.stdout


def test_annotation_is_a_notice_when_within_the_backstop(tmp_path: Path) -> None:
    repo = _tagged_repo(tmp_path, ahead=1, threshold=5)
    proc = _run("--repo", str(repo), "--ref", "HEAD", "--annotate")
    assert proc.returncode == 0
    assert "::notice file=RELEASING.md" in proc.stdout


def test_markdown_report_is_appended_to_the_github_step_summary(
    tmp_path: Path,
) -> None:
    """The signal must be visible without anyone opening the job log."""
    repo = _tagged_repo(tmp_path, ahead=9, threshold=5)
    summary = tmp_path / "summary.md"
    summary.write_text("pre-existing content\n")

    env = dict(os.environ)
    env["GITHUB_STEP_SUMMARY"] = str(summary)
    proc = _run("--repo", str(repo), "--ref", "HEAD", env=env)

    assert proc.returncode == 1
    written = summary.read_text()
    assert written.startswith("pre-existing content\n")
    assert "Release lag" in written
    assert "| 9 |" in written
    assert "RELEASING.md" in written


# --------------------------------------------------------------------------
# CI wiring: automatic, and advisory-only
# --------------------------------------------------------------------------


def test_workflow_runs_the_check_on_a_regular_trigger() -> None:
    text = WORKFLOW.read_text()
    assert "scripts/check-release-lag.sh" in text
    assert "schedule:" in text and "cron:" in text
    assert "branches: [main]" in text


#: Git invocations that would make either surface a *writer*. `git tag` is not
#: listed as a bare prefix because listing tags (`git tag --sort=...`) is the
#: read-only call the check is built on -- only tag *creation* forms are banned.
MUTATING_GIT = (
    "git push",
    "git commit",
    "git checkout",
    "git reset",
    "git tag -a",
    "git tag -s",
    "git tag -f",
    "git tag v",
)


def test_workflow_does_not_gate_pull_requests() -> None:
    """Release lag is a property of main; it must never redden a PR."""
    text = WORKFLOW.read_text()
    assert "pull_request:" not in text
    assert "release-lag.yml" not in text
    assert not (REPO_ROOT / ".github" / "workflows" / "release-lag.yml").exists()


def test_script_never_mutates_the_repository() -> None:
    """Static guard: the check is read-only by construction."""
    text = SCRIPT.read_text()
    for forbidden in MUTATING_GIT:
        assert forbidden not in text, f"{forbidden!r} has no place in a read-only check"


# --------------------------------------------------------------------------
# Last failed Release run (issue #6911): named with a link when Actions
# history is available; omitted -- without touching the lag, the JSON shape,
# or the exit codes -- when it is not.
# --------------------------------------------------------------------------


def _run_entry(run_id: int, conclusion: str, created: str) -> dict:
    return {
        "id": run_id,
        "name": "Release",
        "conclusion": conclusion,
        "created_at": created,
        "html_url": f"https://github.com/acme/widgets/actions/runs/{run_id}",
        "display_title": "Scheduled release",
    }


def _history(*entries: dict) -> str:
    return json.dumps({"total_count": len(entries), "workflow_runs": list(entries)})


def _gh_env(dir_path: Path, runs_json: str, *, fail: bool = False) -> dict[str, str]:
    """A stub `gh` first on PATH, answering the workflow-runs query.

    The stub records its arguments (so tests can assert the query targeted
    this repository's `release.yml`) and cats a fixture; ``fail`` simulates an
    API error (history unavailable).
    """
    bin_dir = dir_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    fixture = dir_path / "runs.json"
    fixture.write_text(runs_json)
    (bin_dir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$*" >> "$STUB_GH_LOG"\n'
        'if [ -n "${STUB_GH_FAIL:-}" ]; then'
        ' echo "gh: Not Found (HTTP 404)" >&2; exit 1; fi\n'
        'cat "$STUB_GH_JSON"\n'
    )
    (bin_dir / "gh").chmod(0o755)
    env = dict(os.environ)
    for var in ("GITHUB_STEP_SUMMARY", "GH_TOKEN", "GITHUB_TOKEN"):
        env.pop(var, None)
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
        GITHUB_REPOSITORY="acme/workflow-runner",
        GH_TOKEN="stub-token",
        STUB_GH_LOG=str(dir_path / "gh.log"),
        STUB_GH_JSON=str(fixture),
        STUB_GH_FAIL="1" if fail else "",
    )
    return env


def _lag_with_history(dir_path: Path, runs_json: str, *, fail: bool = False):
    dir_path.mkdir(parents=True, exist_ok=True)
    repo = _tagged_repo(dir_path, ahead=6, threshold=5)
    _git("remote", "add", "origin", "https://github.com/acme/widgets.git", cwd=repo)
    proc = _run(
        "--repo",
        str(repo),
        "--ref",
        "HEAD",
        "--format",
        "json",
        env=_gh_env(dir_path, runs_json, fail=fail),
    )
    return json.loads(proc.stdout), proc.returncode, proc.stdout, proc.stderr


def test_newest_failed_release_run_is_named_with_its_link(tmp_path: Path) -> None:
    report, _code, _out, _err = _lag_with_history(
        tmp_path,
        _history(
            _run_entry(101, "success", "2026-11-09T15:17:00Z"),
            _run_entry(102, "failure", "2026-11-16T15:17:00Z"),  # newest, failed
        ),
    )
    url = "https://github.com/acme/widgets/actions/runs/102"
    assert report["last_failed_run"] == url
    assert url in report["message"]
    # The query targeted this repository's release.yml workflow.
    assert (
        "repos/acme/widgets/actions/workflows/release.yml/runs?status=failure"
        in (tmp_path / "gh.log").read_text()
    )


def test_failed_run_is_still_named_after_a_success(tmp_path: Path) -> None:
    report, _code, _out, _err = _lag_with_history(
        tmp_path,
        _history(
            _run_entry(102, "failure", "2026-11-16T15:17:00Z"),
            _run_entry(103, "success", "2026-11-23T15:17:00Z"),  # newest, ok
        ),
    )
    assert (
        report["last_failed_run"] == "https://github.com/acme/widgets/actions/runs/102"
    )
    assert report["last_failed_run"] in report["message"]


def test_empty_actions_history_has_no_link(tmp_path: Path) -> None:
    report, _code, _out, _err = _lag_with_history(tmp_path, _history())
    assert report["last_failed_run"] is None
    assert "actions/runs" not in report["message"]


def test_unavailable_history_omits_the_link_and_keeps_it_out_of_the_report(
    tmp_path: Path,
) -> None:
    # An API error is not a release fact: no link, no error text in the
    # report, same exit code.
    report, code, out, _err = _lag_with_history(
        tmp_path,
        _history(_run_entry(102, "failure", "2026-11-16T15:17:00Z")),
        fail=True,
    )
    assert code == 1
    assert report["last_failed_run"] is None
    assert "HTTP 404" not in out

    # Without a token configured, gh is never invoked at all.
    env = _gh_env(
        tmp_path / "notoken",
        _history(_run_entry(102, "failure", "2026-11-16T15:17:00Z")),
    )
    env.pop("GH_TOKEN")
    repo = _tagged_repo(tmp_path / "notoken", ahead=6, threshold=5)
    proc = _run("--repo", str(repo), "--ref", "HEAD", "--format", "json", env=env)
    assert json.loads(proc.stdout)["last_failed_run"] is None
    assert not (tmp_path / "notoken" / "gh.log").exists()


def test_the_lookup_never_changes_the_local_lag_or_exit_status(
    tmp_path: Path,
) -> None:
    """Failed, superseded, empty, and unavailable history must all report the
    same locally computed lag and the same exit status."""
    cases = {
        "failed": _history(_run_entry(102, "failure", "2026-11-16T15:17:00Z")),
        "superseded": _history(
            _run_entry(102, "failure", "2026-11-16T15:17:00Z"),
            _run_entry(103, "success", "2026-11-23T15:17:00Z"),
        ),
        "empty": _history(),
    }
    results = set()
    for name, history in cases.items():
        report, code, _out, _err = _lag_with_history(tmp_path / name, history)
        results.add((report["status"], report["commits_ahead"], code))
    report, code, _out, _err = _lag_with_history(
        tmp_path / "unavailable", cases["failed"], fail=True
    )
    results.add((report["status"], report["commits_ahead"], code))
    assert results == {("release_due", 6, 1)}
