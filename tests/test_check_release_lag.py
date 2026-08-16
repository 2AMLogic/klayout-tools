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
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-release-lag.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-lag.yml"

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
        # run inside GitHub Actions) -- only the test that asserts on it sets it.
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
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


def test_text_report_points_at_the_changelog_unreleased_section(
    tmp_path: Path,
) -> None:
    """A consumer pinned to the latest release needs to be told where to look
    for what has landed since -- CHANGELOG.md's `## Unreleased` section."""
    repo = _tagged_repo(tmp_path, ahead=9, threshold=5)
    proc = _run("--repo", str(repo), "--ref", "HEAD")
    assert proc.returncode == 1
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


def test_workflow_is_advisory_and_never_releases() -> None:
    """The check must not gate unrelated PRs, and must never cut a release."""
    text = WORKFLOW.read_text()
    assert "pull_request:" not in text, (
        "release lag is a property of main; running on pull_request would "
        "redden unrelated PRs"
    )
    for forbidden in (*MUTATING_GIT, "workflow_call", "gh release"):
        assert forbidden not in text, (
            f"release-lag.yml must stay read-only; found {forbidden!r}"
        )
    assert "permissions:\n  contents: read" in text


def test_script_never_mutates_the_repository() -> None:
    """Static guard: the check is read-only by construction."""
    text = SCRIPT.read_text()
    for forbidden in MUTATING_GIT:
        assert forbidden not in text, f"{forbidden!r} has no place in a read-only check"
