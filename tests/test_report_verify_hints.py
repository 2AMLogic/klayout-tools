"""Issue #4795: diagnostics that explain a stale pin / unresolved --check input."""

from __future__ import annotations

import hashlib
import json
import os

from klayout_tools._report_verify_hints import (
    input_hash_check,
    input_not_found_entry,
    stale_pin,
)
from klayout_tools.erc import check_erc_report


def test_input_not_found_relative_path_names_anchor_and_working_dir(
    tmp_path, monkeypatch
):
    pair = tmp_path / "pair"
    pair.mkdir()
    report = pair / "report.json"
    monkeypatch.chdir(tmp_path)
    found = str(pair / "ref.spice")
    entry = input_not_found_entry("ref.spice", str(report), found)
    assert os.path.realpath(entry["resolved_against"]) == os.path.realpath(tmp_path)
    assert (
        os.path.realpath(pair) in os.path.realpath(entry["hint"])
        or str(pair) in (entry["hint"])
    )
    assert entry["found_relative_to_report"] == found
    assert entry["path"] == "ref.spice"


def test_input_not_found_absolute_path_has_no_anchor_or_hint(tmp_path):
    path = str(tmp_path / "gone.gds")
    entry = input_not_found_entry(path, str(tmp_path / "report.json"), None)
    assert entry["resolved_against"] is None
    assert entry["hint"] is None
    assert entry["resolved"] == path


def test_input_not_found_repo_scoped_echo_names_the_repo_root(tmp_path):
    """Issue #5845: a repo-scoped `{path, scope}` echo reports the repo root
    `resolve_committed_path` anchored it on; other echo shapes report
    `null` (issue #2595's cwd/absolute anchors cover those)."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    report = repo / "pair" / "report.json"

    entry = input_not_found_entry(
        str(repo / "ref.spice"),
        str(report),
        None,
        echoed={"path": "ref.spice", "scope": "repo"},
    )
    assert entry["resolved_repo_root"] == str(repo)

    bare = input_not_found_entry("ref.spice", str(report), None, echoed="ref.spice")
    assert bare["resolved_repo_root"] is None

    external = input_not_found_entry(
        str(repo / "ref.spice"),
        str(report),
        None,
        echoed={"path": None, "scope": "external"},
    )
    assert external["resolved_repo_root"] is None


def test_input_not_found_without_override_hint_names_no_flag(tmp_path):
    """Issue #6840: ``override_hint=None`` means no flag can fix this input
    (the runset deck on ``klt lvs --check``), so the remedy names no flag."""
    entry = input_not_found_entry(
        "pdk.lvs",
        str(tmp_path / "report.json"),
        str(tmp_path / "pdk.lvs"),
        override_hint=None,
    )
    assert entry["hint"] == (
        "run --check from the report's directory; "
        f"request-file-relative paths resolve against {tmp_path}"
    )


def test_input_not_found_default_hint_pins_the_layout_wording(tmp_path):
    """Issue #6840's second observation: since #6802/#6830 retargeted
    ``tests/test_lvs.py``'s assertion, nothing pins the default remedy --
    the one ``klt lvs --check``'s layout entry emits. Pin it byte-for-byte."""
    entry = input_not_found_entry(
        "l.gds", str(tmp_path / "report.json"), str(tmp_path / "l.gds")
    )
    assert entry["hint"] == (
        "run --check from the report's directory, or pass --layout/an absolute path; "
        f"request-file-relative paths resolve against {tmp_path}"
    )


def test_input_hash_check_null_hint_when_not_beside_the_report(tmp_path):
    """The flag-less remedy still appears only when the input sits beside
    the report: missing everywhere, or recorded absolute, the hint stays
    ``null`` (issue #6840 edge cases; the absolute shape is #6223's)."""
    absent = input_hash_check(
        "provenance.runset.content_hash",
        None,
        "gone.lvs",
        str(tmp_path / "report.json"),
        override_hint=None,
    )
    assert absent["input_not_found"]["hint"] is None
    absolute = input_hash_check(
        "provenance.runset.content_hash",
        None,
        str(tmp_path / "elsewhere" / "pdk.lvs"),
        str(tmp_path / "report.json"),
        override_hint=None,
    )
    assert absolute["input_not_found"]["hint"] is None


# --- `klt erc --check` carries the shared `input_not_found` block (#6655) ---
#
# ERC's block has five keys, `klt drc --check`'s set; `klt lvs --check`'s
# three entries add a sixth, `resolved_repo_root`, which ERC does not emit.
# `tests/test_erc.py` sits at its file-size ceiling, so the five-key pin
# lives here (the shared helper's own file), driving `check_erc_report`
# over hand-written reports the way `tests/test_drc_check_input_resolution.py`
# does for `klt drc --check`.


def _erc_report(tmp_path, *, file_value, spec_value, input_hash, spec_hash):
    doc = {
        "file": file_value,
        "spec": spec_value,
        "provenance": {
            "input": {"content_hash": input_hash},
            "spec": {"content_hash": spec_hash},
        },
    }
    path = tmp_path / "erc-report.json"
    path.write_text(json.dumps(doc))
    return str(path)


def _check(result, field):
    return next(c for c in result["checks"] if c["field"] == field)


def test_erc_check_input_not_found_pins_the_five_key_shape(tmp_path, monkeypatch):
    """A relative `file` echo that misses at the cwd but sits beside the
    report gets the five-key block, with no "or pass ..." clause (`klt erc
    --check` has no input-override flag); the untouched `spec` line gets
    none."""
    pair = tmp_path / "pair"
    pair.mkdir()
    (pair / "basic.gds").write_bytes(b"gds-bytes")
    (pair / "basic.erc.json").write_text("{}")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    report = _erc_report(
        pair,
        file_value="basic.gds",
        spec_value=str(pair / "basic.erc.json"),
        input_hash="sha256:recorded-input",
        spec_hash="sha256:recorded-spec",
    )

    result = check_erc_report(report)

    assert result["status"] == "drifted"
    assert _check(result, "provenance.input.content_hash")["input_not_found"] == {
        "path": "basic.gds",
        "resolved": str(elsewhere / "basic.gds"),
        "found_relative_to_report": str(pair / "basic.gds"),
        "resolved_against": str(elsewhere),
        "hint": (
            "run --check from the report's directory; "
            f"request-file-relative paths resolve against {pair}"
        ),
    }
    assert "input_not_found" not in _check(result, "provenance.spec.content_hash")


def test_erc_check_input_not_found_spec_line_absolute(tmp_path):
    """The spec line carries the same block; its absolute echo has no anchor
    and therefore no hint."""
    spec = tmp_path / "basic.erc.json"
    report = _erc_report(
        tmp_path,
        file_value=str(tmp_path / "basic.gds"),
        spec_value=str(spec),
        input_hash="sha256:recorded-input",
        spec_hash="sha256:recorded-spec",
    )

    result = check_erc_report(report)

    assert result["status"] == "drifted"
    assert _check(result, "provenance.spec.content_hash")["input_not_found"] == {
        "path": str(spec),
        "resolved": str(spec),
        "found_relative_to_report": None,
        "resolved_against": None,
        "hint": None,
    }


def test_erc_check_input_not_found_absent_when_input_resolves(tmp_path):
    """No block when the echoed file exists (a genuine content mismatch is a
    plain drifted hash) and none when the echo names no path at all."""
    gds = tmp_path / "basic.gds"
    gds.write_bytes(b"gds-bytes")
    report = _erc_report(
        tmp_path,
        file_value=str(gds),
        spec_value=str(tmp_path / "basic.erc.json"),
        input_hash="sha256:recorded-input",
        spec_hash="sha256:recorded-spec",
    )

    drifted = check_erc_report(report)

    input_check = _check(drifted, "provenance.input.content_hash")
    assert input_check["match"] is False
    assert "input_not_found" not in input_check

    echoless = _erc_report(
        tmp_path,
        file_value={"path": None, "scope": "external"},
        spec_value=str(tmp_path / "basic.erc.json"),
        input_hash="sha256:recorded-input",
        spec_hash="sha256:recorded-spec",
    )

    external = check_erc_report(echoless)

    assert "input_not_found" not in _check(external, "provenance.input.content_hash")


def test_stale_pin_names_the_failed_comparison():
    detail = stale_pin(
        {"content_hash": "sha256:stale-revision"}, {"content_hash": "sha256:layoutA"}
    )
    assert detail["content_hash_pin"] == "stale"
    assert detail["hash_check"] == {
        "field": "provenance.input.content_hash",
        "expected": "sha256:stale-revision",
        "actual": "sha256:layoutA",
        "match": False,
    }


def test_stale_pin_without_input_provenance_gets_the_generic_hint():
    """Issue #5837: even with no path/paths/samples to name, the detail
    explains what ``content_hash`` must be -- never a bare
    ``{content_hash_pin, hash_check}`` with no remedy."""
    detail = stale_pin({"content_hash": "sha256:a"}, {"content_hash": "sha256:b"})
    assert detail["content_hash_pin"] == "stale"
    assert detail["hash_check"]["match"] is False
    assert "compared_against" not in detail
    assert "pin_is_envelope_file_hash" not in detail
    assert detail["hint"] == (
        "content_hash must be the envelope's provenance.input.content_hash "
        "(the recorded input hash), not the hash of the evidence envelope file"
    )


def test_stale_pin_of_envelope_own_hash_is_flagged_without_a_path(tmp_path):
    """Issue #5837: the envelope-own-hash check runs for a path-less
    ``provenance.input`` too -- the pin that names the record, not what the
    record is about, is the mistake the flag exists for."""
    envelope = tmp_path / "drc.json"
    envelope.write_text("{}")
    pin = "sha256:" + hashlib.sha256(envelope.read_bytes()).hexdigest()
    detail = stale_pin(
        {"content_hash": pin},
        {
            "content_hash": "sha256:layoutA",
            "envelope": {"provenance": {"input": {"content_hash": "sha256:layoutA"}}},
            "file": str(envelope),
        },
    )
    assert detail["pin_is_envelope_file_hash"] is True
    assert str(envelope) in detail["hint"]
    assert "itself" in detail["hint"]
    assert "compared_against" not in detail


def test_stale_pin_of_unreadable_evidence_file_no_flag_no_crash(tmp_path):
    detail = stale_pin(
        {"content_hash": "sha256:a"},
        {
            "content_hash": "sha256:b",
            "envelope": {"provenance": {"input": {"content_hash": "sha256:b"}}},
            "file": str(tmp_path / "missing.json"),
        },
    )
    assert "pin_is_envelope_file_hash" not in detail
    assert detail["hint"].startswith("content_hash must be")
