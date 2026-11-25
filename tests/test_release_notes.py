"""Tests for scripts/release_notes.py (issues #5861, #5005)."""

from __future__ import annotations

import importlib.util
import subprocess
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
    assert rn.main(["0.9.1", "--changelog", str(changelog), "--no-fragments"]) == 0
    assert "- older fix" in capsys.readouterr().out
    assert rn.main(["0.8.0", "--changelog", str(changelog), "--no-fragments"]) == 2
    assert "error" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# issue #5005: heading-only sections, fragments, availability notes
# --------------------------------------------------------------------------- #

HEADING_ONLY = (
    "# Changelog\n\n"
    "## Unreleased\n\n"
    "## 0.9.3 (2026-11-25)\n\n"
    "## 0.9.2 (2026-11-14)\n\n- the fix\n"
)


def test_heading_only_section_plus_fragments_is_non_empty() -> None:
    """The publisher's bump commit adds only the heading (whatever
    ## Unreleased held becomes the body); a section that is empty because the
    entries live in fragments must still produce notes."""
    notes = rn.build_notes(
        "0.9.3", HEADING_ONLY, fragments=["- fragment A", "- fragment B"]
    )
    body = notes.split("\n\n---\n")[0]
    assert "- fragment A" in body and "- fragment B" in body
    assert body.index("- fragment A") < body.index("- fragment B")
    assert rn.TRUNCATION_NOTE not in notes


def test_empty_section_and_no_fragments_is_an_error() -> None:
    with pytest.raises(rn.NotesError, match="both empty"):
        rn.build_notes("0.9.3", HEADING_ONLY)


def test_section_body_and_fragments_are_both_included_in_order() -> None:
    notes = rn.build_notes(
        "0.9.2", CHANGELOG, fragments=["- fragment A", "- fragment B"]
    )
    body = notes.split("\n\n---\n")[0]
    assert (
        body.index("- the fix")
        < body.index("- fragment A")
        < body.index("- fragment B")
    )


def test_availability_note_resolves_against_the_superseded_release() -> None:
    changelog = (
        "# Changelog\n\n## 0.9.3 (2026-11-25)\n\n"
        "- available in the first release after `v0.9.2` (see docs).\n"
    )
    notes = rn.build_notes("0.9.3", changelog, previous_version="0.9.2", fragments=[])
    assert "`klt 0.9.3`" in notes
    assert (
        "v0.9.2" not in notes.split("\n\n---\n")[0].replace("blob/v0.9.3", "")
        or "first release after" not in notes
    )


def test_availability_note_with_an_older_anchor_stays_as_written() -> None:
    changelog = (
        "# Changelog\n\n## 0.9.3 (2026-11-25)\n\n"
        "- available in the first release after `v0.9.1` (see docs).\n"
    )
    notes = rn.build_notes("0.9.3", changelog, previous_version="0.9.2", fragments=[])
    assert "first release after `v0.9.1`" in notes
    assert "`klt 0.9.3`" not in notes


def test_soft_wrapped_availability_note_resolves() -> None:
    changelog = (
        "# Changelog\n\n## 0.9.3 (2026-11-25)\n\n"
        "- shipped in the next\n  release after v0.9.2, per the plan.\n"
    )
    notes = rn.build_notes("0.9.3", changelog, previous_version="0.9.2", fragments=[])
    assert "`klt 0.9.3`" in notes


# --------------------------------------------------------------------------- #
# fragment collection against a synthetic git repo
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


@pytest.fixture()
def fragment_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "user.email", "t@e.c")
    (repo / "changelog.d").mkdir()
    (repo / "changelog.d" / "README.md").write_text("howto\n")
    (repo / "CHANGELOG.md").write_text(CHANGELOG)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "tag", "v0.9.2")
    # Fragments added AFTER v0.9.2: two real ones (in name order) plus
    # README.md (modified, not added -- must be skipped by --diff-filter=A
    # anyway) and an empty one.
    (repo / "changelog.d" / "5001-b.md").write_text("- second fragment\n")
    (repo / "changelog.d" / "5000-a.md").write_text("- first fragment\n")
    (repo / "changelog.d" / "empty.md").write_text("  \n")
    (repo / "changelog.d" / "README.md").write_text("howto, edited\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fragments")
    return repo


def test_previous_tag_picks_the_newest_older_tag(fragment_repo: Path) -> None:
    assert rn.previous_tag("0.9.3", fragment_repo) == "v0.9.2"
    assert (
        rn.previous_tag("0.9.2", fragment_repo) == "v0.9.1"
        or rn.previous_tag("0.9.2", fragment_repo) is None
    )


def test_collect_fragments_gathers_added_fragments_in_name_order(
    fragment_repo: Path,
) -> None:
    assert rn.collect_fragments(fragment_repo, "0.9.3") == [
        "- first fragment",
        "- second fragment",
    ]


def test_no_previous_tag_collects_nothing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "user.email", "t@e.c")
    (repo / "CHANGELOG.md").write_text(CHANGELOG)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    assert rn.previous_tag("0.9.3", repo) is None
    assert rn.collect_fragments(repo, "0.9.3") == []


def test_main_collects_fragments_from_the_repo(
    fragment_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (fragment_repo / "CHANGELOG.md").write_text(HEADING_ONLY)
    rc = rn.main(["0.9.3", "--changelog", str(fragment_repo / "CHANGELOG.md")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "- first fragment" in out
    assert "- second fragment" in out


def test_main_outside_a_git_repo_needs_no_fragments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG)
    assert rn.main(["0.9.1", "--changelog", str(changelog)]) == 0
    assert "- older fix" in capsys.readouterr().out
