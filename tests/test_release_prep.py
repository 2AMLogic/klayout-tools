"""Tests for scripts/release_prep.py and the release workflows (issue #4920)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "release_prep", REPO_ROOT / "scripts" / "release_prep.py"
)
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)

PYPROJECT = '[tool.hatch.version]\nsource = "x"\nbase-version = "0.8.0"\n'
CHANGELOG = (
    "# Changelog\n\n## Unreleased\n\n- a fix\n\n## 0.8.0 (2026-10-24)\n\n- old\n"
)
SIGNOFF = "first ships in the first release after `v0.8.0`. Other 0.7.0 text.\n"


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "docs" / "cli").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    (tmp_path / "docs" / "cli" / "signoff.md").write_text(SIGNOFF)
    return tmp_path


def test_next_minor_resets_patch() -> None:
    assert rp.next_minor("0.8.3") == "0.9.0"


def test_prepare_bumps_all_three_files(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    info = rp.prepare(repo, None, "2026-11-09")
    assert info == {"previous": "0.8.0", "version": "0.9.0", "date": "2026-11-09"}
    assert 'base-version = "0.9.0"' in (repo / "pyproject.toml").read_text()
    log = (repo / "CHANGELOG.md").read_text()
    assert "## Unreleased\n\n## 0.9.0 (2026-11-09)\n\n- a fix" in log
    assert "`klt 0.9.0`" in (repo / "docs/cli/signoff.md").read_text()
    assert "Other 0.7.0 text" in (repo / "docs/cli/signoff.md").read_text()


def test_prepare_is_idempotent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    rp.prepare(repo, None, "2026-11-09")
    snapshot = {p.name: p.read_text() for p in repo.rglob("*") if p.is_file()}
    rp.prepare(repo, "0.9.0", "2026-11-10")
    assert snapshot == {p.name: p.read_text() for p in repo.rglob("*") if p.is_file()}


def test_patch_override_and_downgrade_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert rp.prepare(repo, "0.8.1", "2026-11-09")["version"] == "0.8.1"
    with pytest.raises(rp.PrepError):
        rp.prepare(repo, "0.7.0", "2026-11-09")


def test_missing_unreleased_heading_is_an_error() -> None:
    with pytest.raises(rp.PrepError):
        rp.add_changelog_heading("# Changelog\n", "0.9.0", "2026-11-09")


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("chore(release): bump version to 0.9.0", "0.9.0"),
        ("chore(release): bump version to 0.9.0 (#4999)", "0.9.0"),
        ("Merge pull request #4922 from 2AMLogic/release/v0.9.0", "0.9.0"),
        ("Merge pull request #4922 from release/v0.9.0", None),
        ("Merge pull request #4922 from 2AMLogic/feature/issue-1", None),
        ("Merge pull request #4922 from 2AMLogic/release/v0.9.0.dev1", None),
        ("fix: chore(release): bump version to 0.9.0", None),
        ("chore(release): bump version to 0.9.0.dev1", None),
        ("feat: something", None),
    ],
)
def test_detect(subject: str, expected: str | None) -> None:
    assert rp.release_version_from_subject(subject) == expected


def test_cli_next_and_detect(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path)
    assert rp.main(["--repo", str(repo), "next"]) == 0
    assert capsys.readouterr().out.strip() == "0.9.0"
    assert rp.main(["detect", "--subject", "chore: nope"]) == 0
    assert capsys.readouterr().out == ""
    assert rp.main(["--repo", str(repo), "prepare", "--date", "2026-11-09"]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == "0.9.0"


# --- workflow wiring -------------------------------------------------------

RELEASE = (REPO_ROOT / ".github/workflows/release.yml").read_text()
PUBLISH = (REPO_ROOT / ".github/workflows/publish.yml").read_text()


def test_release_workflow_triggers_and_gates() -> None:
    assert "schedule:" in RELEASE and "workflow_dispatch:" in RELEASE
    assert "scripts/check-release-lag.sh" in RELEASE
    assert "scripts/check_native_extras.py" in RELEASE
    assert "pull_request:" not in RELEASE


def test_release_workflow_never_pushes_to_main_or_merges() -> None:
    for forbidden in ("git push origin main", "gh pr merge", "--auto", "HEAD:main"):
        assert forbidden not in RELEASE


def test_tag_job_dispatches_publish_on_the_tag() -> None:
    assert (
        'gh workflow run publish.yml --repo "$GITHUB_REPOSITORY" --ref "$v"' in RELEASE
    )
    assert 'git tag -a "$v" -m "Release ${v#v}" "$GITHUB_SHA"' in RELEASE
    assert "workflow_dispatch:" in PUBLISH


def test_publish_refuses_non_tag_refs() -> None:
    assert "refs/tags/v*) ;;" in PUBLISH
    assert PUBLISH.index("Require a release tag ref") < PUBLISH.index("uv build")


def test_release_lag_workflow_is_retired() -> None:
    assert not (REPO_ROOT / ".github/workflows/release-lag.yml").exists()
