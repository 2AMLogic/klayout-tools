"""Release availability notes across signoff docs, fragments and legacy entries."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from test_release_prep import CHANGELOG, _repo, rp


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
