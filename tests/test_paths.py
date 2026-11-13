"""Unit tests for the shared helpers in ``klayout_tools._paths``.

These are internal (leading-underscore) helpers with no public re-export;
the module-level docstring in ``_paths.py`` explains why they were pulled
out. This file covers the ones not already exercised indirectly through a
public verb's own test suite. The SPICE line pre-processing helpers that
used to live in ``_paths`` now have their own module and test file --
``_spice_text`` / ``test_spice_text.py`` (issue #5417).
"""

from __future__ import annotations

from klayout_tools._paths import looks_like_request_document


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
