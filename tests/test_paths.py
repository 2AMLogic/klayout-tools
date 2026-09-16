"""Unit tests for the shared helpers in ``klayout_tools._paths``.

These are internal (leading-underscore) helpers with no public re-export;
the module-level docstrings in ``_paths.py`` and in each duplicating
caller (``pex.py``, ``op_sanity.py``, ``netlist_normalize.py``) explain
why they were pulled out. This file covers the ones not already
exercised indirectly through a public verb's own test suite.
"""

from __future__ import annotations

from klayout_tools._paths import _fold_spice_continuations, looks_like_request_document


def test_fold_spice_continuations_empty_input():
    assert _fold_spice_continuations([]) == []


def test_fold_spice_continuations_no_continuation_lines():
    lines = ["M1 out in vss vss nfet", ".ends"]
    assert _fold_spice_continuations(lines) == lines


def test_fold_spice_continuations_single_continuation():
    lines = [".subckt amp inp inn", "+ out vdd vss"]
    assert _fold_spice_continuations(lines) == [".subckt amp inp inn out vdd vss"]


def test_fold_spice_continuations_multiple_chained_continuations():
    lines = [
        "XM1 out in 0 0 sky130_fd_pr__pfet_01v8",
        "+ L = 0.15",
        "+ W={2*wu}",
        "+ nf=1",
    ]
    assert _fold_spice_continuations(lines) == [
        "XM1 out in 0 0 sky130_fd_pr__pfet_01v8 L = 0.15 W={2*wu} nf=1"
    ]


def test_fold_spice_continuations_leading_continuation_with_nothing_to_fold_onto():
    """A ``+`` line with no preceding entry is passed through verbatim --
    it is not folded (there is nothing to fold onto) and not dropped or
    raised on. Callers that want an orphan continuation dropped instead
    (``op_sanity.py``) filter this function's return value themselves."""
    lines = ["+ out vdd vss", ".ends"]
    assert _fold_spice_continuations(lines) == ["+ out vdd vss", ".ends"]


def test_fold_spice_continuations_leading_whitespace_before_plus():
    lines = [".subckt amp inp inn", "  + out vdd vss"]
    assert _fold_spice_continuations(lines) == [".subckt amp inp inn out vdd vss"]


def test_fold_spice_continuations_preserves_non_continuation_entries_between():
    lines = ["A", "+ a-cont", "B", "+ b-cont"]
    assert _fold_spice_continuations(lines) == ["A a-cont", "B b-cont"]


def test_looks_like_request_document_stdin_sentinel():
    assert looks_like_request_document("-") is True


def test_looks_like_request_document_nonexistent_path_starting_with_brace():
    # A typo'd/nonexistent path starting with "{" is still treated as
    # (malformed) inline JSON, not a missing layout file -- issue #1867.
    assert looks_like_request_document("{not-a-real-file.gds") is True


def test_looks_like_request_document_inline_json_not_a_file():
    assert looks_like_request_document('{"file": "x.gds"}') is True


def test_looks_like_request_document_existing_file_starting_with_brace_is_not_json(
    tmp_path, monkeypatch
):
    # Regression test for issue #1922: a genuine layout file whose bare
    # relative name starts with "{" must be routed to content-sniffing
    # (and, since it is not JSON, treated as a layout path) rather than
    # being misclassified as inline JSON purely from its literal spelling.
    layout = tmp_path / "{weird}.gds"
    layout.write_bytes(b"\x00\x06\x00\x02not real GDS but not JSON either")
    monkeypatch.chdir(tmp_path)
    assert looks_like_request_document("{weird}.gds") is False


def test_looks_like_request_document_existing_file_starting_with_brace_and_json_content(
    tmp_path, monkeypatch
):
    # Same bare-name-starts-with-"{" shape, but this time the file's
    # *contents* are genuinely a JSON request document -- content-sniffing
    # should still say True, just via the file-content branch rather than
    # the raw-string branch.
    request_file = tmp_path / "{weird}.json"
    request_file.write_text('{"layout": "top.gds"}')
    monkeypatch.chdir(tmp_path)
    assert looks_like_request_document("{weird}.json") is True


def test_looks_like_request_document_existing_file_not_starting_with_brace(
    tmp_path, monkeypatch
):
    layout = tmp_path / "top.gds"
    layout.write_bytes(b"\x00\x06\x00\x02not real GDS")
    monkeypatch.chdir(tmp_path)
    assert looks_like_request_document("top.gds") is False


def test_looks_like_request_document_unreadable_file_falls_through(
    tmp_path, monkeypatch
):
    # A directory named like a file: os.path.isfile is False for
    # directories, so this exercises the "not an existing file" path
    # rather than the OSError branch, but confirms the bare-name-with-"{"
    # is *not* misclassified just because a same-named directory exists.
    directory = tmp_path / "{weird-dir}"
    directory.mkdir()
    monkeypatch.chdir(tmp_path)
    assert looks_like_request_document("{weird-dir}") is True
