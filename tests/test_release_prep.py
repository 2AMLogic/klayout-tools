"""Tests for scripts/release_prep.py and the release workflows (issue #4920)."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
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


EMPTY_CHANGELOG = "# Changelog\n\n## Unreleased\n\n## 0.8.0 (2026-10-24)\n\n- old\n"


def test_changelog_fragments_start_with_a_bullet() -> None:
    offenders = []
    for path in sorted((REPO_ROOT / "changelog.d").glob("*.md")):
        if path.name == "README.md" or not path.is_file():
            continue
        entry = path.read_text(encoding="utf-8")
        if entry.strip() and not entry.startswith("- "):
            offenders.append(path.name)
    assert not offenders, "Expected a leading '- ' in " + ", ".join(offenders)


def test_prepare_releases_fragments_in_filename_order(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    fragments = repo / "changelog.d"
    fragments.mkdir()
    (fragments / "200-second.md").write_text("- second\n\n")
    (fragments / "100-first.md").write_text("- first\n  continued")
    (fragments / "150-empty.md").write_text(" \n\n")
    (fragments / "README.md").write_text("# Changelog fragments\n")
    (fragments / "draft.txt").write_text("draft")
    (fragments / "subdirectory.md").mkdir()

    assert rp.main(["--repo", str(repo), "prepare", "--date", "2026-11-09"]) == 0

    changelog = repo / "CHANGELOG.md"
    expected = (
        "# Changelog\n\n## Unreleased\n\n## 0.9.0 (2026-11-09)\n\n"
        "- a fix\n\n- first\n  continued\n\n- second\n\n"
        "## 0.8.0 (2026-10-24)\n\n- old\n"
    )
    assert changelog.read_text() == expected
    assert {path.name for path in fragments.iterdir()} == {
        "150-empty.md",
        "README.md",
        "draft.txt",
        "subdirectory.md",
    }

    rp.prepare(repo, "0.9.0", "2026-11-10")
    assert changelog.read_text() == expected

    pending = fragments / "300-next.md"
    pending.write_text("- next release\n")
    rp.prepare(repo, "0.9.0", "2026-11-10")
    assert changelog.read_text() == expected
    assert pending.read_text() == "- next release\n"


def test_prepare_releases_fragments_with_no_legacy_entries(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    changelog = repo / "CHANGELOG.md"
    changelog.write_text(EMPTY_CHANGELOG)
    fragments = repo / "changelog.d"
    fragments.mkdir()
    entry = fragments / "100-fix.md"
    entry.write_text("- a fix\n")

    rp.prepare(repo, None, "2026-11-09")

    assert changelog.read_text() == EMPTY_CHANGELOG.replace(
        "## Unreleased\n\n", "## Unreleased\n\n## 0.9.0 (2026-11-09)\n\n- a fix\n\n"
    )
    assert not entry.exists()


def test_prepare_with_empty_fragment_directory_keeps_legacy_output(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    (repo / "changelog.d").mkdir()
    rp.prepare(repo, None, "2026-11-09")
    assert (repo / "CHANGELOG.md").read_bytes() == CHANGELOG.replace(
        "## Unreleased\n\n", "## Unreleased\n\n## 0.9.0 (2026-11-09)\n\n"
    ).encode("utf-8")


def test_independent_changelog_fragments_merge_cleanly(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "--initial-branch=base")
    git("config", "user.name", "Release Test")
    git("config", "user.email", "release-test@example.com")
    git("config", "commit.gpgsign", "false")
    (tmp_path / "CHANGELOG.md").write_text(EMPTY_CHANGELOG)
    git("add", "CHANGELOG.md")
    git("commit", "-m", "Base changelog")

    for branch, filename in [("first", "100-first.md"), ("second", "200-second.md")]:
        git("switch", "-c", branch, "base")
        fragments = tmp_path / "changelog.d"
        fragments.mkdir(exist_ok=True)
        (fragments / filename).write_text(f"- {branch} change\n")
        git("add", f"changelog.d/{filename}")
        git("commit", "-m", f"Add {branch} fragment")

    tree = git("merge-tree", "--write-tree", "first", "second")
    assert git("show", f"{tree}:changelog.d/100-first.md") == "- first change"
    assert git("show", f"{tree}:changelog.d/200-second.md") == "- second change"


def test_prepare_refuses_empty_unreleased_section(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "CHANGELOG.md").write_text(EMPTY_CHANGELOG)
    with pytest.raises(rp.PrepError, match="nothing to release"):
        rp.prepare(repo, None, "2026-11-09")
    # The refusal happens before any write: the tree is untouched.
    assert (repo / "pyproject.toml").read_text() == PYPROJECT
    assert (repo / "CHANGELOG.md").read_text() == EMPTY_CHANGELOG


def test_prepare_refuses_whitespace_only_unreleased_section(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n   \n\n## 0.8.0 (2026-10-24)\n"
    )
    with pytest.raises(rp.PrepError, match="nothing to release"):
        rp.prepare(repo, None, "2026-11-09")
    assert (repo / "pyproject.toml").read_text() == PYPROJECT


def test_add_changelog_heading_refuses_empty_section_at_eof() -> None:
    text = "# Changelog\n\n## Unreleased\n"
    with pytest.raises(rp.PrepError, match="nothing to release"):
        rp.add_changelog_heading(text, "0.9.0", "2026-11-09")


def test_rerun_after_release_cut_skips_empty_section_gate(tmp_path: Path) -> None:
    # Post-release state: the cut left '## Unreleased' empty with the
    # '## X.Y.Z (' heading present; an idempotent re-run must not trip the gate.
    repo = _repo(tmp_path)
    rp.prepare(repo, None, "2026-11-09")
    log = (repo / "CHANGELOG.md").read_text()
    assert log.startswith("# Changelog\n\n## Unreleased\n\n## 0.9.0 (2026-11-09)")
    snapshot = {p.name: p.read_text() for p in repo.rglob("*") if p.is_file()}
    rp.prepare(repo, "0.9.0", "2026-11-10")  # no raise
    assert snapshot == {p.name: p.read_text() for p in repo.rglob("*") if p.is_file()}


def test_cli_prepare_empty_section_exits_2(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path)
    (repo / "CHANGELOG.md").write_text(EMPTY_CHANGELOG)
    assert rp.main(["--repo", str(repo), "prepare", "--date", "2026-11-09"]) == 2
    assert "nothing to release" in capsys.readouterr().err


def test_fragment_notes_resolve_only_in_new_section(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    fragment = repo / "changelog.d" / "1234-added.md"
    fragment.parent.mkdir()
    current = "the first release after `v0.8.0`"
    wrapped = "the next\n  release after v0.8.0"
    fragment.write_text(f"- {current}, {wrapped}, and {current}.\n")
    changelog = repo / "CHANGELOG.md"
    historic = f"- historical: {current}.\n- wrapped: {wrapped}.\n"
    changelog.write_text(CHANGELOG + historic)

    rp.prepare(repo, None, "2026-11-09")
    assembled = changelog.read_text()
    assert "- `klt 0.9.0`, `klt 0.9.0`, and `klt 0.9.0`." in assembled
    assert assembled.endswith(historic)
    assert not fragment.exists()
    # Rerunning the same cut changes nothing and leaves later fragments pending.
    pending = repo / "changelog.d" / "1300-next-cycle.md"
    pending.write_text(f"- {wrapped}\n")
    rp.prepare(repo, "0.9.0", "2026-11-10")
    assert changelog.read_text() == assembled
    assert pending.read_text() == f"- {wrapped}\n"


# --- availability-note matrix (issue #7493) ---------------------------------
#
# anchor vs. previous (0.8.0) | docs/cli/signoff.md | fragment / Unreleased
# older                       | refuse             | refuse
# equal                       | resolve            | resolve in the new section
# newer                       | keep for a later cut | refuse (the source is consumed)

SOURCES = ["signoff", "fragment", "unreleased"]
_NOTE_WORDS = ("the", "first", "release", "after")


def _note(anchor: str, *, wrap_at: int | None = None, gap: str = "\n") -> str:
    """A "the first release after `vX`" note, optionally broken at one gap."""
    words = [*_NOTE_WORDS, f"`v{anchor}`"]
    out = words[0]
    for index, word in enumerate(words[1:]):
        out += (gap if index == wrap_at else " ") + word
    return out


def _place(repo: Path, source: str, text: str) -> Path:
    """Make ``text`` the whole content of ``source`` and return its path."""
    if source == "signoff":
        path = repo / "docs" / "cli" / "signoff.md"
    elif source == "fragment":
        path = repo / "changelog.d" / "100-note.md"
        path.parent.mkdir(exist_ok=True)
    else:
        path = repo / "CHANGELOG.md"
        text = (
            f"# Changelog\n\n## Unreleased\n\n{text}\n## 0.8.0 (2026-10-24)\n\n- old\n"
        )
    path.write_text(text, encoding="utf-8")
    return path


def _tree(repo: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}


def _released_text(repo: Path, source: str) -> str:
    """The text a source's notes end up in once the cut is prepared."""
    if source == "signoff":
        return (repo / "docs" / "cli" / "signoff.md").read_text(encoding="utf-8")
    log = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    new_section = log.split("\n## 0.8.0", 1)[0]
    assert "## 0.9.0 (2026-11-09)" in new_section
    return new_section


WRAPS = [
    pytest.param(None, "\n", id="flat"),
    *(pytest.param(i, "\n", id=f"wrap{i}") for i in range(4)),
    *(pytest.param(i, "\n    ", id=f"wrap{i}-indented") for i in range(4)),
    pytest.param(1, " \n\t", id="wrap1-trailing-space"),
]


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize(("wrap_at", "gap"), WRAPS)
def test_equal_anchor_resolves_flat_and_wrapped_notes(
    tmp_path: Path, source: str, wrap_at: int | None, gap: str
) -> None:
    repo = _repo(tmp_path)
    note = _note("0.8.0", wrap_at=wrap_at, gap=gap)
    path = _place(repo, source, f"- ships in {note}.\n")
    # A copy of the note in an already-released section must stay as written.
    historical = f"- historical: {note}\n"
    changelog = repo / "CHANGELOG.md"
    changelog.write_text(changelog.read_text() + historical)

    rp.prepare(repo, None, "2026-11-09")

    released = _released_text(repo, source)
    assert "- ships in `klt 0.9.0`.\n" in released
    assert "release after" not in re.sub(r"\s+", " ", released)
    assert changelog.read_text().endswith(historical)
    if source == "fragment":
        assert not path.exists()


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("wrap_at", [None, 1, 3])
def test_older_anchor_is_refused_everywhere(
    tmp_path: Path, source: str, wrap_at: int | None, capsys
) -> None:
    repo = _repo(tmp_path)
    path = _place(repo, source, f"- stale: {_note('0.7.0', wrap_at=wrap_at)}.\n")
    before = _tree(repo)

    assert rp.main(["--repo", str(repo), "prepare", "--date", "2026-11-09"]) == 2
    err = capsys.readouterr().err
    assert f"{path.relative_to(repo)}:" in err and "v0.7.0" in err
    assert _tree(repo) == before


@pytest.mark.parametrize("wrap_at", [None, 1, 3])
def test_newer_anchor_is_kept_in_signoff_for_a_later_cut(
    tmp_path: Path, wrap_at: int | None
) -> None:
    repo = _repo(tmp_path)
    future = _note("0.9.5", wrap_at=wrap_at)
    path = _place(repo, "signoff", f"- now: {_note('0.8.0')}.\n- later: {future}.\n")
    rp.prepare(repo, None, "2026-11-09")
    assert path.read_text() == f"- now: `klt 0.9.0`.\n- later: {future}.\n"


@pytest.mark.parametrize("source", ["fragment", "unreleased"])
@pytest.mark.parametrize("wrap_at", [None, 1, 3])
def test_newer_anchor_in_a_consumed_source_is_refused(
    tmp_path: Path, source: str, wrap_at: int | None, capsys
) -> None:
    repo = _repo(tmp_path)
    path = _place(repo, source, f"- later: {_note('0.9.5', wrap_at=wrap_at)}.\n")
    before = _tree(repo)

    assert rp.main(["--repo", str(repo), "prepare", "--date", "2026-11-09"]) == 2
    err = capsys.readouterr().err
    assert f"release_prep.py: {path.relative_to(repo)}:" in err and "v0.9.5" in err
    # Nothing is published or consumed: every input keeps its original bytes.
    assert _tree(repo) == before


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("anchor", ["0.8.0", "0.7.0"])
@pytest.mark.parametrize("blank", ["\n\n", "\n \n", "\n\n  "])
def test_blank_line_separates_paragraphs_so_no_placeholder_forms(
    tmp_path: Path, source: str, anchor: str, blank: str
) -> None:
    repo = _repo(tmp_path)
    text = f"- one paragraph ends the first{blank}release after `v{anchor}`.\n"
    path = _place(repo, source, text)

    rp.prepare(repo, None, "2026-11-09")  # no refusal, no resolution

    assert text in _released_text(repo, source)
    if source == "signoff":
        assert path.read_text() == text


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("trailing", ["", "\n"])
def test_flat_and_wrapped_notes_in_one_input_both_resolve(
    tmp_path: Path, source: str, trailing: str
) -> None:
    repo = _repo(tmp_path)
    flat = _note("0.8.0")
    wrapped = _note("0.8.0", wrap_at=2, gap="\n  ")
    _place(repo, source, f"- one {flat}\n- two {wrapped}{trailing}")

    rp.prepare(repo, None, "2026-11-09")

    released = _released_text(repo, source)
    assert "- one `klt 0.9.0`\n- two `klt 0.9.0`" in released


@pytest.mark.parametrize(
    ("source", "anchor"),
    [
        ("signoff", "0.7.0"),
        ("fragment", "0.7.0"),
        ("fragment", "0.9.5"),
        ("unreleased", "0.7.0"),
        ("unreleased", "0.9.5"),
    ],
)
def test_wrapped_valid_note_does_not_shift_the_reported_line(
    tmp_path: Path, source: str, anchor: str, capsys
) -> None:
    repo = _repo(tmp_path)
    body = (
        "intro\n\n- ok: the first\n  release after `v0.8.0`.\n"
        f"- bad: the next\n  release after `v{anchor}`.\n"
    )
    path = _place(repo, source, body)
    line = path.read_text().splitlines().index("- bad: the next") + 1
    before = _tree(repo)

    assert rp.main(["--repo", str(repo), "prepare", "--date", "2026-11-09"]) == 2
    err = capsys.readouterr().err
    assert f"release_prep.py: {path.relative_to(repo)}:{line}: " in err
    assert f"anchored on v{anchor}" in err
    assert _tree(repo) == before


def test_reported_line_counts_physical_newlines_only() -> None:
    # str.splitlines() also breaks on form feed; an editor's line numbers do not.
    text = "a\x0cb\n- the first release\n  after `v0.7.0`\n"
    with pytest.raises(rp.PrepError, match=r"x\.md:2: .*v0\.7\.0"):
        rp.check_signoff_note_anchors(text, "0.8.0", source="x.md")


def test_guard_refuses_newer_anchor_only_for_consumed_text() -> None:
    text = "- the next release after v0.9.5\n"
    rp.check_signoff_note_anchors(text, "0.8.0")  # persistent: pass through
    with pytest.raises(rp.PrepError, match=r"x\.md:1: .*v0\.9\.5"):
        rp.check_signoff_note_anchors(text, "0.8.0", source="x.md", consumed=True)


def test_stale_fragment_note_reports_source_line_before_writes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    fragments = repo / "changelog.d"
    fragments.mkdir()
    (fragments / "100-valid.md").write_text("- first release after `v0.8.0`\n")
    (fragments / "200-stale.md").write_text(
        "\n\n- stale in the first release after `v0.7.0`.\n"
    )
    before = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}

    with pytest.raises(rp.PrepError, match=r"changelog\.d/200-stale\.md:3:"):
        rp.prepare(repo, None, "2026-11-09")
    assert {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()} == before


def test_prepare_refuses_stale_anchor(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    stale = "intro\n\nfirst ships in the first release after `v0.7.0`.\n"
    (repo / "docs/cli/signoff.md").write_text(stale)
    fragment = repo / "changelog.d" / "1234.added.md"
    fragment.parent.mkdir()
    fragment.write_bytes(b"- a pending entry\n")
    with pytest.raises(rp.PrepError, match=r"0\.7\.0") as excinfo:
        rp.prepare(repo, None, "2026-11-09")
    assert ":3:" in str(excinfo.value)  # names the offending line
    # The refusal happens before any write: the tree is untouched (issue #6211).
    assert (repo / "pyproject.toml").read_text() == PYPROJECT
    assert (repo / "CHANGELOG.md").read_text() == CHANGELOG
    assert (repo / "docs/cli/signoff.md").read_text() == stale
    # Pending fragments survive the refusal byte-for-byte, so a stale anchor
    # cannot cost the operator unreleased entries (issue #6604).
    assert fragment.read_bytes() == b"- a pending entry\n"


def test_prepare_leaves_newer_anchor_untouched(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "docs/cli/signoff.md").write_text(
        "first ships in the first release after `v0.8.0` "
        "and later the next release after v0.9.5 too.\n"
    )
    rp.prepare(repo, None, "2026-11-09")
    text = (repo / "docs/cli/signoff.md").read_text()
    assert "`klt 0.9.0` and later the next release after v0.9.5" in text


def test_prepare_rerun_skips_stale_anchor_check(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    rp.prepare(repo, None, "2026-11-09")  # current is now 0.9.0
    (repo / "docs/cli/signoff.md").write_text(
        "first ships in the first release after `v0.7.0`.\n"
    )
    rp.prepare(repo, "0.9.0", "2026-11-10")  # idempotent re-run: no raise
    assert "v0.7.0" in (repo / "docs/cli/signoff.md").read_text()


def test_releasing_convention_prose_is_not_scanned(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "RELEASING.md").write_text(
        'Fill in availability notes. Replace "the next release after 0.7.0"\n'
        "in docs/cli/signoff.md with X.Y.Z.\n"
    )
    rp.prepare(repo, None, "2026-11-09")  # must not raise: only signoff.md is scanned


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


@pytest.mark.parametrize("with_fragments", [False, True])
def test_prepare_resolves_legacy_unreleased_entries(
    tmp_path: Path, with_fragments: bool
):
    repo = _repo(tmp_path)
    note = "the first release after `v0.8.0`"
    changelog = repo / "CHANGELOG.md"
    historical = "## 0.8.0 (2026-10-24)\n\n- " + note + "\n"
    changelog.write_text(
        f"# Changelog\n\n## Unreleased\n\n- Legacy: {note}\n\n{historical}"
    )
    if with_fragments:
        fragment = repo / "changelog.d" / "100.md"
        fragment.parent.mkdir()
        fragment.write_text("- Fragment\n")
    rp.prepare(repo, None, "2026-11-09")
    result = changelog.read_text()
    assert "- Legacy: `klt 0.9.0`\n" in result
    assert result.endswith(historical)


def test_prepare_stale_unreleased_anchor_reports_file_line_before_writing(
    tmp_path: Path,
):
    repo = _repo(tmp_path)
    changelog = repo / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\nIntro\n\n## Unreleased\n\n- Old\n"
        "  the first release after `v0.7.0`\n"
    )
    fragment = repo / "changelog.d" / "200.md"
    fragment.parent.mkdir()
    fragment.write_text("- Pending\n")
    before = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    with pytest.raises(rp.PrepError, match=r"CHANGELOG\.md:8:"):
        rp.prepare(repo, None, "2026-11-09")
    assert {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()} == before


def test_refresh_collects_late_notes_once_and_leaves_history_intact(tmp_path):
    repo = _repo(tmp_path)
    rp.prepare(repo, "0.9.0", "2026-11-01")
    changelog = repo / "CHANGELOG.md"
    original = changelog.read_text()
    history = original.split("## 0.8.0", 1)[1]
    changelog.write_text(
        original.replace("## Unreleased", "## Unreleased\n\n- late legacy")
    )
    fragments = repo / "changelog.d"
    fragments.mkdir()
    pending = fragments / "99-late.md"
    pending.write_text("- late fragment: the first release after `v0.8.0`\n")
    note = repo / "docs/cli/signoff.md"
    note.write_text(SIGNOFF)
    before = changelog.read_text()
    assert (
        rp.main(["--repo", str(repo), "refresh", "--version", "0.9.0", "--check"]) == 1
    )
    assert (
        changelog.read_text() == before
        and pending.is_file()
        and note.read_text() == SIGNOFF
    )
    args = ["--repo", str(repo), "refresh", "--version", "0.9.0"]
    assert rp.main(args) == 0
    refreshed = changelog.read_text()
    assert refreshed.startswith("# Changelog\n\n## Unreleased\n\n## 0.9.0 (2026-11-01)")
    assert "- a fix\n\n- late legacy\n\n- late fragment: `klt 0.9.0`" in refreshed
    assert refreshed.split("## 0.8.0", 1)[1] == history
    assert not pending.exists() and "`klt 0.9.0`" in note.read_text()
    assert rp.main([*args, "--check"]) == rp.main(args) == 0
    assert changelog.read_text() == refreshed
    (fragments / "next.md").write_text("- next version\n")
    assert rp.prepare(repo, "0.10.0", "2026-12-01")["version"] == "0.10.0"


@pytest.mark.parametrize(
    ("invalid", "anchor"),
    [
        ("branch", "0.7.0"),
        ("fragment", "0.7.0"),
        ("unreleased", "0.7.0"),
        ("prepared", "0.7.0"),
        # Every refresh input lands in a release section, so a newer anchor
        # would be orphaned there too (issue #7493).
        ("fragment", "0.9.5"),
        ("unreleased", "0.9.5"),
        ("prepared", "0.9.5"),
    ],
)
def test_invalid_refresh_preserves_all_inputs(tmp_path, invalid, anchor, capsys):
    repo = _repo(tmp_path)
    rp.prepare(repo, "0.9.0", "2026-11-01")
    directory = repo / "changelog.d"
    directory.mkdir()
    fragment = directory / "late.md"
    fragment.write_text("- another fragment\n")
    stale = f"- stale: first release after `v{anchor}`"
    source = fragment
    if invalid == "fragment":
        fragment.write_text(f"\n\n{stale}\n")
    elif invalid in {"unreleased", "prepared"}:
        source = repo / "CHANGELOG.md"
        heading = (
            "## Unreleased" if invalid == "unreleased" else "## 0.9.0 (2026-11-01)"
        )
        source.write_text(source.read_text().replace(heading, f"{heading}\n\n{stale}"))
    paths = [
        repo / "pyproject.toml",
        repo / "CHANGELOG.md",
        repo / "docs/cli/signoff.md",
        fragment,
    ]
    before = [p.read_bytes() for p in paths]
    version = "0.9.1" if invalid == "branch" else "0.9.0"
    assert rp.main(["--repo", str(repo), "refresh", "--version", version]) == 2
    assert [p.read_bytes() for p in paths] == before
    if invalid != "branch":
        line = source.read_text().splitlines().index(stale) + 1
        assert (
            f"release_prep.py: {source.relative_to(repo)}:{line}:"
            in capsys.readouterr().err
        )


def test_refresh_keeps_newer_signoff_anchor_for_a_later_cut(tmp_path):
    repo = _repo(tmp_path)
    rp.prepare(repo, "0.9.0", "2026-11-01")
    future = "- later: the first release\n  after `v0.9.5`.\n"
    note = repo / "docs/cli/signoff.md"
    note.write_text(f"- now: the first\n  release after `v0.8.0`.\n{future}")
    assert rp.main(["--repo", str(repo), "refresh", "--version", "0.9.0"]) == 0
    assert note.read_text() == f"- now: `klt 0.9.0`.\n{future}"
