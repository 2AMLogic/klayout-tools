"""Tests for ``scripts/release_prep.py`` (issue #5005): the two-file bump
the fleet publisher commits to ``main``.

The CHANGELOG edit must stay byte-for-byte the shape the publisher's own
validator (2AMLogic/2am ``infra/cloudflare/github-write/src/publish/
version-sites.js`` ``changelogBump``) reconstructs from the parent file -- a
drift here means every automatic bump is refused upstream, loudly.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "release_prep", REPO_ROOT / "scripts" / "release_prep.py"
)
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)

PYPROJECT = (
    '[project]\nname = "klayout-tools"\ndynamic = ["version"]\n\n'
    '[tool.hatch.version]\nsource = "code"\npath = "hatch_build.py"\n'
    'expression = "version_from_pyproject()"\nbase-version = "0.9.2"\n'
)

CHANGELOG = (
    "# Changelog\n\nprose about the file.\n\n"
    "## Unreleased\n\n- entry A\n- entry B\n\n"
    "## 0.9.1 (2026-10-01)\n\n- older\n"
)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    return tmp_path


def _reference_changelog_bump(text: str, to: str, date: str) -> str:
    """A Python transcription of the publisher's ``changelogBump`` (the
    authority this repo's output must match). Deliberately written from the
    JavaScript, not from ``release_prep``."""
    crlf = "\r\n" in text
    nl = "\r\n" if crlf else "\n"
    cr = "\r" if crlf else ""
    return re.sub(
        r"^## Unreleased(\r?)$",
        lambda _m: f"## Unreleased{nl}{nl}## {to} ({date}){cr}",
        text,
        count=1,
        flags=re.M,
    )


# --------------------------------------------------------------------------- #
# prepare()
# --------------------------------------------------------------------------- #


def test_prepare_default_is_one_patch_step(repo: Path) -> None:
    info = rp.prepare(repo, None, "2026-11-25")

    assert info == {"previous": "0.9.2", "version": "0.9.3", "date": "2026-11-25"}
    assert 'base-version = "0.9.3"' in (repo / "pyproject.toml").read_text()
    # The old value is gone and nothing else in the file changed.
    expected_py = PYPROJECT.replace('base-version = "0.9.2"', 'base-version = "0.9.3"')
    assert (repo / "pyproject.toml").read_text() == expected_py


def test_patch_bump_keeps_major_minor(repo: Path) -> None:
    """The batch runner's client/runner version gate compares major.minor
    (#4211): an automatic bump may never change it."""
    for _ in range(3):
        rp.prepare(repo, None, "2026-11-25")
        current = rp.read_base_version((repo / "pyproject.toml").read_text())
        assert current.startswith("0.9.")
        (repo / "CHANGELOG.md").write_text(
            (repo / "CHANGELOG.md")
            .read_text()
            .replace("## Unreleased\n", "## Unreleased\n\n- more\n", 1)
        )


def test_prepare_inserts_heading_directly_under_unreleased(repo: Path) -> None:
    rp.prepare(repo, None, "2026-11-25")

    text = (repo / "CHANGELOG.md").read_text()
    assert "## Unreleased\n\n## 0.9.3 (2026-11-25)\n\n- entry A\n- entry B\n" in text
    # The former Unreleased body now belongs to the release; Unreleased itself
    # is empty again, and older sections are untouched.
    assert text.endswith("## 0.9.1 (2026-10-01)\n\n- older\n")


def test_changelog_edit_matches_the_publishers_own_reconstruction(repo: Path) -> None:
    rp.prepare(repo, None, "2026-11-25")

    sent = (repo / "CHANGELOG.md").read_text()
    assert sent == _reference_changelog_bump(CHANGELOG, "0.9.3", "2026-11-25")


def test_crlf_changelog_keeps_crlf(tmp_path: Path) -> None:
    crlf = CHANGELOG.replace("\n", "\r\n")
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    (tmp_path / "CHANGELOG.md").write_bytes(crlf.encode())

    rp.prepare(tmp_path, None, "2026-11-25")

    sent = (tmp_path / "CHANGELOG.md").read_bytes().decode()
    assert sent == _reference_changelog_bump(crlf, "0.9.3", "2026-11-25")


def test_version_override_is_forwarded_verbatim(repo: Path) -> None:
    info = rp.prepare(repo, "0.10.0", "2026-11-25")
    assert info["version"] == "0.10.0"
    assert "## 0.10.0 (2026-11-25)" in (repo / "CHANGELOG.md").read_text()


# --------------------------------------------------------------------------- #
# refusals (nothing is written when one fires)
# --------------------------------------------------------------------------- #


def test_refuses_non_increasing_version(repo: Path) -> None:
    for bad in ("0.9.2", "0.9.1", "0.8.9"):
        with pytest.raises(rp.PrepError, match="newer"):
            rp.prepare(repo, bad, "2026-11-25")
    assert (repo / "pyproject.toml").read_text() == PYPROJECT
    assert (repo / "CHANGELOG.md").read_text() == CHANGELOG


def test_refuses_missing_or_ambiguous_unreleased(repo: Path) -> None:
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## 0.9.1 (…)\n- x\n")
    with pytest.raises(rp.PrepError, match="Unreleased"):
        rp.prepare(repo, None, "2026-11-25")

    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n## Unreleased\n"
    )
    with pytest.raises(rp.PrepError, match="expected exactly 1"):
        rp.prepare(repo, None, "2026-11-25")


def test_refuses_an_existing_release_heading(repo: Path) -> None:
    log = CHANGELOG + "\n## 0.9.3 (2020-01-01)\n\n- ancient\n"
    (repo / "CHANGELOG.md").write_text(log)
    with pytest.raises(rp.PrepError, match="already has"):
        rp.prepare(repo, None, "2026-11-25")
    assert (repo / "CHANGELOG.md").read_text() == log


def test_refuses_bad_date_and_bad_versions(repo: Path) -> None:
    with pytest.raises(rp.PrepError, match="YYYY-MM-DD"):
        rp.prepare(repo, None, "2026-11-5")
    with pytest.raises(rp.PrepError, match="X.Y.Z"):
        rp.prepare(repo, "0.10", "2026-11-25")
    with pytest.raises(rp.PrepError, match="base-version"):
        (repo / "pyproject.toml").write_text('[project]\nname = "x"\n')
        rp.prepare(repo, None, "2026-11-25")


# --------------------------------------------------------------------------- #
# main()
# --------------------------------------------------------------------------- #


def test_main_prints_json_and_exits_2_on_refusal(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    assert rp.main(["--repo", str(repo), "prepare"]) == 0
    out = capsys.readouterr().out
    assert '"version": "0.9.3"' in out
    assert '"previous": "0.9.2"' in out

    assert rp.main(["--repo", str(repo), "prepare", "--version", "0.9.2"]) == 2
    assert "release_prep.py:" in capsys.readouterr().err


def test_this_repos_own_changelog_yields_exactly_one_unreleased() -> None:
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert len(rp._UNRELEASED_RE.findall(text)) == 1
