"""Unit tests for the shared helpers in ``klayout_tools._paths``.

These are internal (leading-underscore) helpers with no public re-export;
the module-level docstrings in ``_paths.py`` and in each duplicating
caller (``pex.py``, ``op_sanity.py``, ``netlist_normalize.py``) explain
why they were pulled out. This file covers the ones not already
exercised indirectly through a public verb's own test suite.
"""

from __future__ import annotations

import json
import re

import pytest

from klayout_tools._paths import (
    _load_request_json,
    fold_spice_continuations,
    looks_like_request_document,
    parse_layer_datatype,
    parse_layer_datatype_strict,
)
from klayout_tools.cli._parsing import parse_layer_spec


def test_fold_spice_continuations_empty_input():
    assert fold_spice_continuations([]) == []


def test_fold_spice_continuations_no_continuation_lines():
    lines = ["M1 out in vss vss nfet", ".ends"]
    assert fold_spice_continuations(lines) == lines


def test_fold_spice_continuations_single_continuation():
    lines = [".subckt amp inp inn", "+ out vdd vss"]
    assert fold_spice_continuations(lines) == [".subckt amp inp inn out vdd vss"]


def test_fold_spice_continuations_multiple_chained_continuations():
    lines = [
        "XM1 out in 0 0 sky130_fd_pr__pfet_01v8",
        "+ L = 0.15",
        "+ W={2*wu}",
        "+ nf=1",
    ]
    assert fold_spice_continuations(lines) == [
        "XM1 out in 0 0 sky130_fd_pr__pfet_01v8 L = 0.15 W={2*wu} nf=1"
    ]


def test_fold_spice_continuations_leading_continuation_with_nothing_to_fold_onto():
    """A ``+`` line with no preceding entry is passed through verbatim --
    it is not folded (there is nothing to fold onto) and not dropped or
    raised on. Callers that want an orphan continuation dropped instead
    (``op_sanity.py``) filter this function's return value themselves."""
    lines = ["+ out vdd vss", ".ends"]
    assert fold_spice_continuations(lines) == ["+ out vdd vss", ".ends"]


def test_fold_spice_continuations_leading_whitespace_before_plus():
    lines = [".subckt amp inp inn", "  + out vdd vss"]
    assert fold_spice_continuations(lines) == [".subckt amp inp inn out vdd vss"]


def test_fold_spice_continuations_preserves_non_continuation_entries_between():
    lines = ["A", "+ a-cont", "B", "+ b-cont"]
    assert fold_spice_continuations(lines) == ["A a-cont", "B b-cont"]


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


# --------------------------------------------------------------------------- #
# _load_request_json error messages (issue #5530)
# --------------------------------------------------------------------------- #


class _LoaderError(Exception):
    """Stand-in for a caller's own error class (``EvalError`` et al.)."""


def test_load_request_json_undecodable_file_names_the_path(tmp_path):
    # `UnicodeDecodeError` carries no filename of its own, so the message has
    # to supply it -- `klt eval` sends two files through this loader.
    path = tmp_path / "latin1.json"
    path.write_bytes(b"\xff\xfe{}")
    with pytest.raises(_LoaderError, match=re.escape(str(path))) as excinfo:
        _load_request_json(str(path), _LoaderError)
    assert "could not read request" in str(excinfo.value)


def test_load_request_json_malformed_json_names_the_path(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    with pytest.raises(_LoaderError, match=re.escape(str(path))) as excinfo:
        _load_request_json(str(path), _LoaderError)
    assert "is not valid JSON" in str(excinfo.value)


def test_load_request_json_path_with_spaces_and_quote_is_readable(tmp_path):
    # A path that is itself awkward to quote must not break message
    # formatting (the message single-quotes the path).
    path = tmp_path / "a dir with spaces" / "it's bad.json"
    path.parent.mkdir()
    path.write_text("{not json")
    with pytest.raises(_LoaderError, match=re.escape(str(path))):
        _load_request_json(str(path), _LoaderError)


def test_load_request_json_valid_document_round_trips(tmp_path):
    path = tmp_path / "ok.json"
    path.write_text(json.dumps({"hello": "world"}))
    assert _load_request_json(str(path), _LoaderError) == {"hello": "world"}


class _SpecError(Exception):
    """Stand-in for a caller's own exception class."""


#: The acceptance table from issue #5605 -- every ``"<layer>/<datatype>"``
#: parser in the package shares one rule, pinned here in exactly one place:
#: digits only, surrounding whitespace tolerated, signs rejected.
_LAYER_DATATYPE_ACCEPTANCE_TABLE = [
    ("68/20", (68, 20)),
    (" 68 / 20 ", (68, 20)),
    ("-68/20", None),
    ("68/-20", None),
    ("+68/20", None),
    ("68/20/3", None),
]


@pytest.mark.parametrize(
    ("raw", "expected"),
    _LAYER_DATATYPE_ACCEPTANCE_TABLE,
    ids=[raw for raw, _ in _LAYER_DATATYPE_ACCEPTANCE_TABLE],
)
def test_parse_layer_datatype_acceptance_table(raw, expected):
    """The shared acceptance rule (issue #5605): ``raw`` parses to
    ``expected``, or is rejected (``expected is None``). A negative GDS
    layer or datatype is never valid, so signed input is malformed --
    previously four of the five hand-rolled parsers accepted ``-68/20``
    while the fifth rejected it."""
    if expected is None:
        with pytest.raises(ValueError):
            parse_layer_datatype_strict(raw)
    else:
        assert parse_layer_datatype_strict(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    _LAYER_DATATYPE_ACCEPTANCE_TABLE,
    ids=[raw for raw, _ in _LAYER_DATATYPE_ACCEPTANCE_TABLE],
)
def test_parse_layer_datatype_spec_wrapper_follows_the_table(raw, expected):
    """The spec-file wrapper raises the caller's error class, naming spec,
    field and offending value, on exactly the rows the shared rule rejects."""
    if expected is None:
        with pytest.raises(_SpecError, match=re.escape(repr(raw))) as excinfo:
            parse_layer_datatype(raw, "stackup.json", "vias[0].layer", _SpecError)
        assert "spec 'stackup.json': vias[0].layer" in str(excinfo.value)
    else:
        assert (
            parse_layer_datatype(raw, "stackup.json", "vias[0].layer", _SpecError)
            == expected
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    _LAYER_DATATYPE_ACCEPTANCE_TABLE,
    ids=[raw for raw, _ in _LAYER_DATATYPE_ACCEPTANCE_TABLE],
)
def test_parse_layer_spec_cli_wrapper_follows_the_table(raw, expected):
    """The CLI-flag wrapper delegates to the same rule and phrases the
    error with the flag name and the caller's error class."""
    if expected is None:
        with pytest.raises(_SpecError, match=re.escape(repr(raw))) as excinfo:
            parse_layer_spec(raw, "--window-layer", _SpecError)
        assert "--window-layer" in str(excinfo.value)
    else:
        assert parse_layer_spec(raw, "--window-layer", _SpecError) == expected
