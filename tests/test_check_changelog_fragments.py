"""Tests for the changelog-fragment shape gate (issue #7619).

`scripts/check_changelog_fragments.py` enforces what `changelog.d/README.md`
documents: a fragment is a Markdown bullet carrying an issue reference. It is
exercised on synthetic fragment directories, plus the real tree and the CI
wiring so the gate cannot be silently dropped or made advisory.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_changelog_fragments.py"

_spec = importlib.util.spec_from_file_location("check_changelog_fragments", SCRIPT)
assert _spec is not None and _spec.loader is not None
ccf = importlib.util.module_from_spec(_spec)
sys.modules["check_changelog_fragments"] = ccf
_spec.loader.exec_module(ccf)


def _write(directory: Path, name: str, text: str | bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8", newline="")
    return path


def _run(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--dir", str(directory)],
        capture_output=True,
        text=True,
        check=False,
    )


# --- the shape rule ---------------------------------------------------------


def test_valid_bullet_passes(tmp_path: Path) -> None:
    _write(
        tmp_path, "1234-fix.md", "- **LVS counts repeated mismatches once** (#1234).\n"
    )
    assert ccf.check_directory(tmp_path) == []
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_bare_paragraph_fails_naming_file_and_reason(tmp_path: Path) -> None:
    _write(tmp_path, "7370-bare.md", "Grade limits against supply (#7370).\n")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "7370-bare.md" in result.stderr
    assert "bullet" in result.stderr


def test_bullet_without_issue_reference_fails(tmp_path: Path) -> None:
    _write(tmp_path, "6905-no-ref.md", "- Route checks pin connectivity.\n")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "6905-no-ref.md" in result.stderr
    assert "#" in result.stderr and "issue reference" in result.stderr


def test_bare_paragraph_without_reference_reports_both_reasons(tmp_path: Path) -> None:
    _write(tmp_path, "9-both.md", "Neither bullet nor reference.\n")
    ((path, reasons),) = ccf.check_directory(tmp_path)
    assert path.name == "9-both.md"
    assert len(reasons) == 2


def test_multi_line_bullet_passes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "6905-multi.md",
        "- `klt place-and-route` checks required terminals against the\n"
        "  final GDS and names physically split nets. (#6905)\n",
    )
    assert ccf.check_directory(tmp_path) == []


def test_reference_only_on_a_continuation_line_passes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "5-cont.md",
        "- A headline with no reference here\n  continued, see #5.\n",
    )
    assert ccf.check_directory(tmp_path) == []


@pytest.mark.parametrize(
    "ref", ["(#1234)", "(issue #1234)", "(#1234, 2026-11-17)", "2am#1234", "#1234"]
)
def test_accepted_reference_forms(tmp_path: Path, ref: str) -> None:
    _write(tmp_path, "1234-ref.md", f"- Something changed {ref}.\n")
    assert ccf.check_directory(tmp_path) == []


def test_date_is_not_required(tmp_path: Path) -> None:
    # The README example used to demand `(#N, YYYY-MM-DD)`; only 6 of 1465
    # released bullets carry a date, so the rule is the reference alone.
    _write(tmp_path, "1234-nodate.md", "- Something changed (#1234).\n")
    assert ccf.check_directory(tmp_path) == []


def test_heading_only_fragment_fails(tmp_path: Path) -> None:
    _write(tmp_path, "8-heading.md", "# Heading (#8)\n")
    ((path, reasons),) = ccf.check_directory(tmp_path)
    assert path.name == "8-heading.md"
    assert any("bullet" in r for r in reasons)


def test_leading_blank_lines_are_skipped_like_release_prep(tmp_path: Path) -> None:
    _write(tmp_path, "3-blank.md", "\n\n- After blank lines (#3).\n")
    assert ccf.check_directory(tmp_path) == []
    _write(tmp_path, "4-blank-bare.md", "\n\nBare after blank lines (#4).\n")
    assert [p.name for p, _ in ccf.check_directory(tmp_path)] == ["4-blank-bare.md"]


def test_crlf_line_endings_are_handled(tmp_path: Path) -> None:
    _write(tmp_path, "6-crlf.md", b"- CRLF bullet\r\n  continued (#6).\r\n")
    assert ccf.check_directory(tmp_path) == []
    _write(tmp_path, "7-crlf-bare.md", b"CRLF paragraph (#7).\r\n")
    assert [p.name for p, _ in ccf.check_directory(tmp_path)] == ["7-crlf-bare.md"]


def test_bullet_marker_needs_the_following_space(tmp_path: Path) -> None:
    _write(tmp_path, "10-dash.md", "-no space (#10).\n")
    assert [p.name for p, _ in ccf.check_directory(tmp_path)] == ["10-dash.md"]


def test_invalid_utf8_is_reported_not_a_traceback(tmp_path: Path) -> None:
    _write(tmp_path, "11-latin1.md", b"- caf\xe9 (#11).\n")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "11-latin1.md" in result.stderr
    assert "UTF-8" in result.stderr
    assert "Traceback" not in result.stderr


# --- which files are in scope (mirrors release_prep._read_changelog_fragments) --


def test_readme_empty_and_non_markdown_files_are_ignored(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "# Changelog fragments\n\nNo bullet, no ref.\n")
    _write(tmp_path, "100-empty.md", "")
    _write(tmp_path, "101-blank.md", " \n\n")
    _write(tmp_path, "draft.txt", "not a fragment at all")
    _write(tmp_path, ".gitkeep", "")
    (tmp_path / "subdirectory.md").mkdir()
    assert ccf.check_directory(tmp_path) == []
    assert _run(tmp_path).returncode == 0


def test_every_offender_is_listed_in_filename_order(tmp_path: Path) -> None:
    _write(tmp_path, "300-c.md", "- no ref\n")
    _write(tmp_path, "100-a.md", "bare\n")
    _write(tmp_path, "200-ok.md", "- fine (#200).\n")
    result = _run(tmp_path)
    assert result.returncode == 1
    names = [
        line.split(":", 1)[0].rsplit("/", 1)[-1]
        for line in result.stderr.splitlines()
        if ".md:" in line
    ]
    assert names[0] == "100-a.md"
    assert names[-1] == "300-c.md"
    assert "200-ok.md" not in result.stderr


def test_missing_directory_is_an_error_not_a_pass(tmp_path: Path) -> None:
    result = _run(tmp_path / "does-not-exist")
    assert result.returncode == 2
    assert "does-not-exist" in result.stderr


# --- the real tree and the CI wiring ---------------------------------------


def test_real_changelog_d_is_clean() -> None:
    offenders = ccf.check_directory(REPO_ROOT / "changelog.d")
    assert not offenders, "\n".join(f"{p.name}: {'; '.join(r)}" for p, r in offenders)


def test_ci_runs_gate_without_continue_on_error() -> None:
    text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    i = text.index("Changelog fragment shape")
    block = text[i : text.index("\n      - name:", i)]
    assert "scripts/check_changelog_fragments.py" in block
    assert "continue-on-error: true" not in block


def test_readme_documents_the_enforced_rule() -> None:
    readme = (REPO_ROOT / "changelog.d" / "README.md").read_text()
    assert "scripts/check_changelog_fragments.py" in readme
    assert "#1234" in readme
    # The README must not demand a date the check (and 99.6% of released
    # entries) does not require.
    assert "2026-11-17" not in readme
