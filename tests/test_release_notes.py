"""Tests for scripts/release_notes.py (issue #5861)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "release_notes", REPO_ROOT / "scripts" / "release_notes.py"
)
rn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rn)

CHANGELOG = (
    "# Changelog\n\n"
    "## Unreleased\n\n- next cycle\n\n"
    "## 0.9.2 (2026-11-14)\n\n- the fix\n- the feature\n\n"
    "## 0.9.1 (2026-11-05)\n\n- older fix\n"
)


def test_extracts_middle_section_without_heading_or_neighbours() -> None:
    assert rn.extract_section(CHANGELOG, "0.9.2") == "- the fix\n- the feature"


def test_extracts_last_section() -> None:
    assert rn.extract_section(CHANGELOG, "0.9.1") == "- older fix"


def test_missing_version_is_an_error() -> None:
    with pytest.raises(rn.NotesError, match="0.8.0"):
        rn.extract_section(CHANGELOG, "0.8.0")


def test_empty_section_is_an_error() -> None:
    changelog = "# Changelog\n\n## 0.9.2 (2026-11-14)\n\n## 0.9.1 (…)\n- x\n"
    with pytest.raises(rn.NotesError, match="empty"):
        rn.extract_section(changelog, "0.9.2")


@pytest.mark.parametrize("bad", ["v0.9.2", "0.9", "Unreleased", ""])
def test_bad_version_is_an_error(bad: str) -> None:
    with pytest.raises(rn.NotesError, match="X.Y.Z"):
        rn.extract_section(CHANGELOG, bad)


def test_small_section_intact_with_footer() -> None:
    notes = rn.build_notes("0.9.2", CHANGELOG)
    assert "- the fix\n- the feature" in notes
    assert rn.TRUNCATION_NOTE not in notes
    assert "https://github.com/2AMLogic/klayout-tools/blob/v0.9.2/CHANGELOG.md" in notes
    assert "https://pypi.org/project/klayout-tools/0.9.2/" in notes


def test_repo_override_shapes_the_changelog_link() -> None:
    notes = rn.build_notes("0.9.2", CHANGELOG, repo="acme/tool")
    assert "https://github.com/acme/tool/blob/v0.9.2/CHANGELOG.md" in notes


def test_truncation_cuts_at_a_line_boundary_under_the_cap() -> None:
    section = "\n".join(f"- entry {i} " + "x" * 40 for i in range(200))
    changelog = f"# Changelog\n\n## 0.9.2 (2026-11-14)\n\n{section}\n\n## 0.9.1 (…)\n"
    notes = rn.build_notes("0.9.2", changelog, max_chars=2_000)
    body, _, footer = notes.partition(rn.TRUNCATION_NOTE)
    assert footer  # the link footer survives truncation
    assert len(notes) <= 2_000
    # every retained line is a verbatim prefix of the section (line boundary)
    assert section.startswith(body.rstrip("\n"))
    assert body.rstrip("\n") != section  # it did actually cut


def test_truncation_boundary_is_exact() -> None:
    section = "\n".join(f"- entry {i}" for i in range(50))
    changelog = f"# Changelog\n\n## 0.9.2 (2026-11-14)\n\n{section}\n"
    whole = len(rn.build_notes("0.9.2", changelog, max_chars=10**9))
    assert rn.TRUNCATION_NOTE not in rn.build_notes("0.9.2", changelog, max_chars=whole)
    assert rn.TRUNCATION_NOTE in rn.build_notes("0.9.2", changelog, max_chars=whole - 1)


def test_pathological_single_line_is_hard_cut_and_bounded() -> None:
    changelog = f"# Changelog\n\n## 0.9.2 (2026-11-14)\n\n{'x' * 900}\n"
    notes = rn.build_notes("0.9.2", changelog, max_chars=1_000)
    assert len(notes) <= 1_000
    assert rn.TRUNCATION_NOTE in notes


def test_absurd_max_chars_is_an_error() -> None:
    with pytest.raises(rn.NotesError, match="max_chars"):
        rn.build_notes("0.9.2", CHANGELOG, max_chars=10)


def test_main_writes_notes_and_exits_2_on_bad_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG)
    assert rn.main(["0.9.1", "--changelog", str(changelog)]) == 0
    assert "- older fix" in capsys.readouterr().out
    assert rn.main(["0.8.0", "--changelog", str(changelog)]) == 2
    assert "error" in capsys.readouterr().err
