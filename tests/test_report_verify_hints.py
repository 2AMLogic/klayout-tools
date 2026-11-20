"""Issue #4795: diagnostics that explain a stale pin / unresolved --check input."""

from __future__ import annotations

import hashlib
import os

import pytest

from klayout_tools._report_verify_hints import (
    input_hash_check,
    input_not_found_entry,
    stale_pin,
)


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
        include_repo_root=True,
        echoed={"path": "ref.spice", "scope": "repo"},
    )
    assert entry["resolved_repo_root"] == str(repo)

    bare = input_not_found_entry(
        "ref.spice", str(report), None, include_repo_root=True, echoed="ref.spice"
    )
    assert bare["resolved_repo_root"] is None

    external = input_not_found_entry(
        str(repo / "ref.spice"),
        str(report),
        None,
        include_repo_root=True,
        echoed={"path": None, "scope": "external"},
    )
    assert external["resolved_repo_root"] is None


def test_input_not_found_repo_scoped_echo_under_override_names_no_repo_root(
    tmp_path,
):
    """Issue #6461: the override gate is the function's own, not a
    caller-side substitution -- an override supplied the reported ``path``
    (anchored on the cwd), so a repo-scoped echo's root must not be named
    as an anchor the path never used."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    report = repo / "pair" / "report.json"
    override = str(tmp_path / "override.spice")

    entry = input_not_found_entry(
        override,
        str(report),
        None,
        include_repo_root=True,
        echoed={"path": "ref.spice", "scope": "repo"},
        override=override,
    )
    assert entry["resolved_repo_root"] is None


_REPO_ECHO = {"path": "ref.spice", "scope": "repo"}
_EXTERNAL_ECHO = {"path": None, "scope": "external"}


@pytest.mark.parametrize(
    ("echoed", "override", "anchored_on_repo"),
    [
        pytest.param(None, "override.spice", False, id="absent-echo+override"),
        pytest.param("ref.spice", None, False, id="bare-string"),
        pytest.param("ref.spice", "override.spice", False, id="bare-string+override"),
        pytest.param(_REPO_ECHO, None, True, id="repo-scoped"),
        pytest.param(_REPO_ECHO, "override.spice", False, id="repo-scoped+override"),
        pytest.param(
            _EXTERNAL_ECHO, "override.spice", False, id="external-echo+override"
        ),
    ],
)
def test_input_hash_check_repo_root_key_presence_follows_the_opt_in(
    tmp_path, echoed, override, anchored_on_repo
):
    """Issue #6461 (Judge finding on #6807): whenever a caller opts in with
    ``include_repo_root`` and an ``input_not_found`` block is emitted at all,
    ``resolved_repo_root`` is *present* -- ``null`` unless a repo-scoped echo
    anchored the path -- whatever the echo's shape. Key presence is the
    caller's opt-in, never inferred from the echo being non-``None``: a
    report with no ``layout``/``reference`` echo plus an override must keep
    ``"resolved_repo_root": null`` (documented in ``docs/cli/lvs.md``)."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    report = str(repo / "pair" / "report.json")
    override = str(tmp_path / override) if override else None

    def block(include_repo_root):
        check = input_hash_check(
            "environment.layout_sha256",
            "0" * 64,
            echoed,
            report,
            override,
            include_repo_root=include_repo_root,
        )
        assert "input_not_found" in check, "case must emit the diagnostic"
        return check["input_not_found"]

    opted_in = block(True)
    assert "resolved_repo_root" in opted_in
    assert opted_in["resolved_repo_root"] == (str(repo) if anchored_on_repo else None)
    assert "resolved_repo_root" not in block(False)


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
