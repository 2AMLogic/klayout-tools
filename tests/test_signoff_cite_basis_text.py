"""The text ``cite:`` line discloses ``content_hash_basis`` (issue #6861).

JSON carried the basis since #4893, but ``--format text`` printed the bare
``content_hash=`` -- so a digest a grader took from a committed store
manifest (or any future basis token) read exactly like one it re-hashed off
disk. The fix is token-agnostic: whatever string the citation carries
renders verbatim beside the hash, with no gloss table to maintain."""

from __future__ import annotations

import hashlib

import pytest

from klayout_tools.cli.color import PLAIN
from klayout_tools.cli.signoff_cmd import (
    _print_cite_line,
    _print_input_verified,
    _print_tier_report_text,
)
from klayout_tools.signoff import build_tier_report
from test_signoff import FUNCTIONAL_VERIFICATION_SDF_ENVELOPE, _manifest, _write

#: A minimal citation dict of the shape both call sites (the met-item path
#: and the coverage-undisclosed refusal) hand ``_print_cite_line``.
CITATION = {
    "file": "block/fv/fv.json",
    "command": None,
    "kind": "functional-verification",
    "check_status": "pass",
    "content_hash": "sha256:" + "a" * 64,
    "exit_status": 0,
}

#: Today's line, byte-for-byte, for the absent-basis case -- the guarantee
#: that no existing text-format assertion changes.
LINE_WITHOUT_BASIS = (
    f"        cite: block/fv/fv.json (kind=functional-verification, "
    f"status=pass, content_hash=sha256:{'a' * 64}, exit_status=0)"
)


def _rendered(citation: dict, capsys) -> str:
    _print_cite_line(citation)
    return capsys.readouterr().out.rstrip("\n")


def _with_basis(token: str) -> str:
    return LINE_WITHOUT_BASIS.replace(
        f"content_hash=sha256:{'a' * 64},",
        f"content_hash=sha256:{'a' * 64}, content_hash_basis={token},",
    )


def test_absent_basis_renders_todays_line_byte_for_byte(capsys):
    assert _rendered(CITATION, capsys) == LINE_WITHOUT_BASIS


def test_none_basis_renders_todays_line_byte_for_byte(capsys):
    assert _rendered({**CITATION, "content_hash_basis": None}, capsys) == (
        LINE_WITHOUT_BASIS
    )


def test_evidence_file_basis_renders_after_the_hash(capsys):
    assert _rendered(
        {**CITATION, "content_hash_basis": "evidence_file"}, capsys
    ) == _with_basis("evidence_file")


def test_unknown_future_token_renders_bare(capsys):
    # No allow-list, no gloss table: #6550's `artifact_manifest` or any
    # later token renders verbatim rather than crashing or vanishing.
    assert _rendered(
        {**CITATION, "content_hash_basis": "artifact_manifest"}, capsys
    ) == _with_basis("artifact_manifest")


def test_null_content_hash_with_a_basis_does_not_crash(capsys):
    out = _rendered(
        {**CITATION, "content_hash": None, "content_hash_basis": "evidence_file"},
        capsys,
    )
    assert "content_hash=None, content_hash_basis=evidence_file," in out


def _file_bytes_pin(fv_path: str) -> str:
    with open(fv_path, "rb") as handle:
        return "sha256:" + hashlib.sha256(handle.read()).hexdigest()


def test_text_report_shows_basis_for_fv_pinned_citation(tmp_path, capsys):
    """End to end: a legacy FV envelope pinned to its own bytes grades met
    (issue #4893) and the rendered text report carries the basis on the
    ``cite:`` line, so a text reader can tell a pin from a re-hash."""
    fv_path = _write(tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_SDF_ENVELOPE)
    spec = {"file": fv_path, "content_hash": _file_bytes_pin(fv_path)}
    _print_tier_report_text(
        build_tier_report(_manifest(kind="digital", evidence={"7": spec})), PLAIN
    )

    out = capsys.readouterr().out
    assert "content_hash_basis=evidence_file," in out
    assert (
        "        input: re-hashed the evidence file itself -- matches"
        in out.splitlines()
    )


@pytest.mark.parametrize(
    ("basis", "verified", "statement"),
    [
        (
            "artifact_manifest",
            None,
            "used the sha256 recorded for the member in the committed store manifest "
            "-- the artifact itself was not read",
        ),
        (None, True, "re-hashed the artifact this envelope names -- matches"),
        (
            None,
            False,
            "CHANGED -- the artifact this envelope names no longer matches "
            "the content_hash it recorded",
        ),
        (
            None,
            None,
            "not re-hashed (content_hash compared against this envelope's own "
            "claim only -- the artifact itself was not read)",
        ),
    ],
)
def test_input_line_names_the_hash_source(basis, verified, statement, capsys):
    citation = {**CITATION, "input_verified": verified}
    if basis is not None:
        citation["content_hash_basis"] = basis
    _print_input_verified(citation)
    assert capsys.readouterr().out == f"        input: {statement}\n"


def test_text_report_without_a_pin_shows_todays_line(tmp_path, capsys):
    """The same fixture graded without a pin re-hashes the file, carries no
    basis key, and renders exactly the line main renders today."""
    fv_path = _write(tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_SDF_ENVELOPE)
    _print_tier_report_text(
        build_tier_report(_manifest(kind="digital", evidence={"7": {"file": fv_path}})),
        PLAIN,
    )

    out = capsys.readouterr().out
    cite = next(line for line in out.splitlines() if "cite: " in line)
    assert "content_hash_basis" not in cite
    assert cite.rstrip().endswith("exit_status=0)")
