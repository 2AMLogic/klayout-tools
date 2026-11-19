"""`klt drc --check` echoed-input diagnostics (issue #6466): a report whose
recorded input path names no existing file gets the same additive
`input_not_found` block `klt lvs --check` attaches (issues #2595/#6223), so the
`actual: null` is not mistaken for a recorded hash that moved.

Every case also asserts the comparison itself is untouched: `expected`,
`actual`, `match` and the aggregate `status` are what they were before the
diagnostic existed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from klayout_tools._provenance import content_hash
from klayout_tools.cli import main
from klayout_tools.decks import asap7_rules
from klayout_tools.drc import check_drc_report

DECK = str(asap7_rules.DECK_PATH)
FIELD = "provenance.input.content_hash"
LAYOUT_BYTES = b"gds-bytes"


def _single_report(file_value: Any, layout_hash: str | None) -> dict[str, Any]:
    """A klayout-engine report whose deck resolves and matches, so the input
    line is the only one that can fail."""
    return {
        "engine": "klayout",
        "file": file_value,
        "deck": DECK,
        "provenance": {
            "input": {"content_hash": layout_hash},
            "deck": {"name": "asap7.drc", "content_hash": content_hash(DECK)},
        },
    }


def _write(path: Path, doc: dict[str, Any]) -> str:
    path.write_text(json.dumps(doc))
    return str(path)


def _recorded_hash(tmp_path: Path) -> str:
    """The hash a producing run recorded for LAYOUT_BYTES (a file that is
    written, hashed, and -- in the missing cases -- not where the report says)."""
    recorded = tmp_path / "recorded.gds"
    recorded.write_bytes(LAYOUT_BYTES)
    digest = content_hash(str(recorded))
    assert digest is not None
    return digest


def _input_check(result: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    return next(c for c in result["checks"] if c["field"] == f"{prefix}{FIELD}")


@pytest.fixture
def elsewhere(tmp_path, monkeypatch) -> Path:
    """A cwd that is neither the report's directory nor the repo."""
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return cwd


def test_missing_absolute_path_gets_diagnostic_and_unchanged_comparison(tmp_path):
    expected = _recorded_hash(tmp_path)
    gone = str(tmp_path / "gone" / "top.gds")
    report = _write(tmp_path / "report.json", _single_report(gone, expected))
    result = check_drc_report(report)
    check = _input_check(result)
    assert (check["expected"], check["actual"], check["match"]) == (
        expected,
        None,
        False,
    )
    assert result["status"] == "drifted"
    assert check["input_not_found"] == {
        "path": gone,
        "resolved": gone,
        "found_relative_to_report": None,
        "resolved_against": None,
        "hint": None,
    }


def test_relative_path_beside_report_is_a_hint_never_silently_used(tmp_path, elsewhere):
    (tmp_path / "top.gds").write_bytes(LAYOUT_BYTES)
    expected = content_hash(str(tmp_path / "top.gds"))
    report = _write(tmp_path / "report.json", _single_report("top.gds", expected))
    result = check_drc_report(report)
    check = _input_check(result)
    # Bare strings keep their cwd anchoring: the file next to the report is
    # NOT used to turn the failed check into a pass.
    assert (check["actual"], check["match"], result["status"]) == (
        None,
        False,
        "drifted",
    )
    entry = check["input_not_found"]
    assert entry["path"] == "top.gds"
    assert entry["resolved"] == str(elsewhere / "top.gds")
    assert entry["resolved_against"] == str(elsewhere)
    assert entry["found_relative_to_report"] == str(tmp_path / "top.gds")
    assert entry["hint"] == (
        "run --check from the report's directory, or pass an absolute path as "
        "the positional <file> argument; request-file-relative paths resolve "
        f"against {tmp_path}"
    )


def test_relative_path_present_in_cwd_is_found_and_matches(tmp_path, elsewhere):
    (elsewhere / "top.gds").write_bytes(LAYOUT_BYTES)
    expected = content_hash(str(elsewhere / "top.gds"))
    report = _write(tmp_path / "report.json", _single_report("top.gds", expected))
    result = check_drc_report(report)
    check = _input_check(result)
    assert (check["match"], result["status"]) == (True, "match")
    assert "input_not_found" not in check


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "reports").mkdir()
    return repo


def test_repo_scoped_missing_path_is_diagnosed_at_the_report_repo_root(
    tmp_path, elsewhere
):
    repo = _repo(tmp_path)
    expected = _recorded_hash(tmp_path)
    report = _write(
        repo / "reports" / "report.json",
        _single_report({"path": "layout/top.gds", "scope": "repo"}, expected),
    )
    result = check_drc_report(report)
    check = _input_check(result)
    assert (check["actual"], check["match"], result["status"]) == (
        None,
        False,
        "drifted",
    )
    entry = check["input_not_found"]
    # Anchored to the report's repository, not the cwd.
    resolved = os.path.join(os.path.realpath(repo), "layout/top.gds")
    assert entry["path"] == resolved
    assert entry["resolved"] == resolved
    assert entry["resolved_against"] is None


def test_repo_scoped_existing_path_verifies_from_any_cwd(tmp_path, elsewhere):
    repo = _repo(tmp_path)
    (repo / "layout").mkdir()
    layout = repo / "layout" / "top.gds"
    layout.write_bytes(LAYOUT_BYTES)
    report = _write(
        repo / "reports" / "report.json",
        _single_report(
            {"path": "layout/top.gds", "scope": "repo"}, content_hash(str(layout))
        ),
    )
    result = check_drc_report(report)
    assert (_input_check(result)["match"], result["status"]) == (True, "match")
    assert "input_not_found" not in _input_check(result)


def test_existing_file_with_wrong_bytes_is_a_mismatch_not_a_missing_input(tmp_path):
    layout = tmp_path / "top.gds"
    layout.write_bytes(b"changed-bytes")
    report = _write(
        tmp_path / "report.json",
        _single_report(str(layout), _recorded_hash(tmp_path)),
    )
    result = check_drc_report(report)
    check = _input_check(result)
    assert (check["match"], result["status"]) == (False, "drifted")
    assert check["actual"] == content_hash(str(layout))
    assert "input_not_found" not in check


def test_existing_matching_file_has_no_diagnostic(tmp_path):
    layout = tmp_path / "top.gds"
    layout.write_bytes(LAYOUT_BYTES)
    report = _write(
        tmp_path / "report.json",
        _single_report(str(layout), content_hash(str(layout))),
    )
    result = check_drc_report(report)
    assert (_input_check(result)["match"], result["status"]) == (True, "match")
    assert "input_not_found" not in _input_check(result)


@pytest.mark.parametrize(
    "echo",
    [
        None,
        "",
        {"path": None, "scope": "external"},
        {"path": None, "scope": "absent"},
        {"scope": "repo"},
        {"path": "", "scope": "repo"},
        {"path": "top.gds", "scope": "elsewhere"},
        7,
    ],
    ids=[
        "absent",
        "empty-string",
        "external",
        "absent-scope",
        "repo-no-path",
        "repo-empty-path",
        "unknown-scope",
        "malformed",
    ],
)
def test_echoes_that_name_no_path_keep_their_current_output(tmp_path, echo):
    doc = _single_report(echo, _recorded_hash(tmp_path))
    report = _write(tmp_path / "report.json", doc)
    result = check_drc_report(report)
    check = _input_check(result)
    assert (check["actual"], check["match"], result["status"]) == (
        None,
        False,
        "drifted",
    )
    assert "input_not_found" not in check


def test_override_that_exists_wins_over_a_missing_echo(tmp_path):
    layout = tmp_path / "mine.gds"
    layout.write_bytes(LAYOUT_BYTES)
    report = _write(
        tmp_path / "report.json",
        _single_report(str(tmp_path / "gone.gds"), content_hash(str(layout))),
    )
    result = check_drc_report(report, input_path=str(layout))
    assert (_input_check(result)["match"], result["status"]) == (True, "match")
    assert "input_not_found" not in _input_check(result)


def test_missing_override_is_the_path_diagnosed(tmp_path):
    existing = tmp_path / "top.gds"
    existing.write_bytes(LAYOUT_BYTES)
    report = _write(
        tmp_path / "report.json",
        _single_report(str(existing), content_hash(str(existing))),
    )
    typo = str(tmp_path / "typo.gds")
    result = check_drc_report(report, input_path=typo)
    check = _input_check(result)
    assert (check["actual"], check["match"], result["status"]) == (
        None,
        False,
        "drifted",
    )
    assert check["input_not_found"]["path"] == typo
    assert check["input_not_found"]["resolved"] == typo
    assert check["input_not_found"]["hint"] is None


def test_existing_override_with_wrong_bytes_is_a_mismatch(tmp_path):
    existing = tmp_path / "top.gds"
    existing.write_bytes(LAYOUT_BYTES)
    other = tmp_path / "other.gds"
    other.write_bytes(b"different")
    report = _write(
        tmp_path / "report.json",
        _single_report(str(existing), content_hash(str(existing))),
    )
    result = check_drc_report(report, input_path=str(other))
    check = _input_check(result)
    assert (check["match"], result["status"]) == (False, "drifted")
    assert check["actual"] == content_hash(str(other))
    assert "input_not_found" not in check


def _crosscheck(tmp_path: Path, curated_file: str, native_file: str) -> str:
    expected = _recorded_hash(tmp_path)
    curated = _single_report(curated_file, expected)
    curated["engine"] = "curated"
    curated["deck"] = "sky130"
    from klayout_tools.decks import deck_source_path

    curated["provenance"]["deck"] = {
        "content_hash": content_hash(deck_source_path("sky130"))
    }
    native = _single_report(native_file, expected)
    return _write(
        tmp_path / "cc.json",
        {"mode": "crosscheck", "reports": {"curated": curated, "native": native}},
    )


@pytest.mark.parametrize("side", ["curated", "native"])
def test_crosscheck_side_prefixes_carry_the_diagnostic(tmp_path, side):
    present = tmp_path / "top.gds"
    present.write_bytes(LAYOUT_BYTES)
    gone = str(tmp_path / "gone.gds")
    # The recorded hash is for a file with the same bytes, so the side whose
    # input is present matches and only the missing side drifts.
    files = {"curated": str(present), "native": str(present)}
    files[side] = gone
    report = _crosscheck(tmp_path, files["curated"], files["native"])
    result = check_drc_report(report)
    other = "native" if side == "curated" else "curated"
    missing = _input_check(result, f"reports.{side}.")
    assert (missing["actual"], missing["match"]) == (None, False)
    assert missing["input_not_found"]["path"] == gone
    healthy = _input_check(result, f"reports.{other}.")
    assert healthy["match"] is True
    assert "input_not_found" not in healthy
    assert result["status"] == "drifted"


def test_cli_missing_input_json_exit_3_and_diagnostic(tmp_path, capsys):
    gone = str(tmp_path / "gone.gds")
    report = _write(
        tmp_path / "report.json", _single_report(gone, _recorded_hash(tmp_path))
    )
    assert main(["drc", "--check", report, "--format", "json"]) == 3
    payload = json.loads(capsys.readouterr().out)
    result = payload.get("data", payload)
    check = _input_check(result)
    assert result["status"] == "drifted"
    assert (check["actual"], check["match"]) == (None, False)
    assert check["input_not_found"]["path"] == gone


def test_cli_missing_input_text_explains_the_missing_file(tmp_path, capsys):
    gone = str(tmp_path / "gone.gds")
    report = _write(
        tmp_path / "report.json", _single_report(gone, _recorded_hash(tmp_path))
    )
    assert main(["drc", "--check", report, "--format", "text"]) == 3
    out = capsys.readouterr().out
    assert "input not found" in out.lower()
    assert gone in out


def test_cli_matching_input_is_exit_0(tmp_path, capsys):
    layout = tmp_path / "top.gds"
    layout.write_bytes(LAYOUT_BYTES)
    report = _write(
        tmp_path / "report.json",
        _single_report(str(layout), content_hash(str(layout))),
    )
    assert main(["drc", "--check", report, "--format", "json"]) == 0
    capsys.readouterr()
    assert main(["drc", "--check", report, "--format", "text"]) == 0
    assert "input not found" not in capsys.readouterr().out.lower()
