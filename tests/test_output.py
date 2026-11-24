"""Unit tests for the shared `klayout_tools.cli.output` envelope helper."""

import io
import json
import sys

import pytest

from klayout_tools.cli.output import (
    ERROR_EXIT_CODE,
    EXIT_USAGE_ERROR,
    emit_error,
    emit_success,
    render_check_text,
)


def test_emit_success_json_writes_exact_payload(capsys):
    payload = {"schema_version": 1, "file": "x.gds", "layer_count": 0, "layers": []}

    emit_success(payload, "json", text_renderer=lambda p: None)

    out = capsys.readouterr().out
    assert json.loads(out) == payload
    # Trailing newline, indented JSON (human/diff-friendly, not minified).
    assert out.endswith("\n")
    assert "\n" in out.rstrip("\n")


def test_emit_success_text_delegates_to_renderer(capsys):
    payload = {"schema_version": 1, "file": "x.gds"}
    calls = []

    def renderer(p):
        calls.append(p)
        print("rendered!")

    emit_success(payload, "text", text_renderer=renderer)

    assert calls == [payload]
    out = capsys.readouterr().out
    assert out == "rendered!\n"
    # Text format never emits raw JSON.
    assert "{" not in out


#: Text no Windows-1252 stdout can encode (issue #5573: a claimant's free text).
_UNENCODABLE = "レビュー待ち"
_ESCAPED = "\\u30ec\\u30d3\\u30e5\\u30fc\\u5f85\\u3061"


def _legacy_stdout(monkeypatch, errors="strict"):
    """Swap in a cp1252 stdout with the given handler; return ``(bytes, stream)``."""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors=errors, write_through=True)
    monkeypatch.setattr(sys, "stdout", stream)
    return raw, stream


def test_emit_success_text_escapes_what_a_strict_stdout_cannot_encode(monkeypatch):
    """The render survives, in full, with the unencodable characters as
    ``\\uXXXX`` escapes and the encodable ones (``ü``) untouched; the
    stream's own handler is restored afterwards."""
    raw, stream = _legacy_stdout(monkeypatch)

    def renderer(p):
        print("before")
        print(f"{p['note']} ü")
        print("after")

    emit_success({"note": _UNENCODABLE}, "text", text_renderer=renderer)

    assert raw.getvalue().decode("cp1252") == f"before\n{_ESCAPED} ü\nafter\n"
    assert stream.errors == "strict"


def test_emit_success_text_restores_the_handler_when_the_renderer_raises(monkeypatch):
    _, stream = _legacy_stdout(monkeypatch)

    def renderer(p):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        emit_success({}, "text", text_renderer=renderer)

    assert stream.errors == "strict"


def test_emit_success_text_keeps_a_handler_the_caller_chose(monkeypatch):
    """``PYTHONIOENCODING=cp1252:replace`` is the caller's explicit choice:
    it is honoured (``?``), not overridden by the escape fallback."""
    raw, stream = _legacy_stdout(monkeypatch, errors="replace")

    emit_success(
        {"note": _UNENCODABLE},
        "text",
        text_renderer=lambda p: print(p["note"]),
    )

    assert raw.getvalue() == b"??????\n"
    assert stream.errors == "replace"


def test_emit_success_json_keeps_the_original_string_on_a_legacy_stdout(monkeypatch):
    """The JSON is the contract: ASCII-escaped on the wire, so a strict legacy
    stdout carries it without loss and without any fallback."""
    raw, _ = _legacy_stdout(monkeypatch)
    payload = {"schema_version": 1, "note": _UNENCODABLE}

    emit_success(payload, "json", text_renderer=lambda p: None)

    assert json.loads(raw.getvalue().decode("cp1252")) == payload


def test_emit_error_json_shape_and_exit_code(capsys):
    exit_code = emit_error("layers", "file not found: missing.gds", "json")

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error == {
        "schema_version": 1,
        "error": {"command": "layers", "message": "file not found: missing.gds"},
    }


def test_emit_error_code_is_emitted_only_when_set(capsys):
    """Issue #2721: `error.code` is additive -- present when a caller passes
    one, absent (not null) otherwise, and never in the text rendering."""
    assert emit_error("sim", "no capacity", "json", code="batch_no_capacity") == 1
    error = json.loads(capsys.readouterr().err)
    assert error == {
        "schema_version": 1,
        "error": {
            "command": "sim",
            "message": "no capacity",
            "code": "batch_no_capacity",
        },
    }

    emit_error("sim", "no capacity", "json", code=None)
    assert "code" not in json.loads(capsys.readouterr().err)["error"]

    emit_error("sim", "no capacity", "text", code="batch_no_capacity")
    assert capsys.readouterr().err == "klt sim: no capacity\n"


def test_emit_error_text_plain_line(capsys):
    exit_code = emit_error("layers", "file not found: missing.gds", "text")

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "klt layers: file not found: missing.gds\n"


def test_emit_error_exit_code_is_overridable_without_changing_envelope(capsys):
    """A caller-supplied `exit_code` changes only the return value.

    Issue #2029: a usage error a command detects itself (past argparse) still
    owes the documented envelope, but must keep exit 2 rather than collapsing
    into ERROR_EXIT_CODE (1).
    """
    exit_code = emit_error(
        "gen", "a generator name is required", "json", exit_code=EXIT_USAGE_ERROR
    )

    assert exit_code == EXIT_USAGE_ERROR == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "schema_version": 1,
        "error": {"command": "gen", "message": "a generator name is required"},
    }


def test_emit_error_exit_code_defaults_to_error_exit_code(capsys):
    assert emit_error("layers", "boom", "text") == ERROR_EXIT_CODE == 1
    capsys.readouterr()


def test_render_check_text_plain_check_list(capsys):
    render_check_text(
        {
            "report": "r.json",
            "status": "drift",
            "checks": [
                {"field": "a", "match": True},
                {"field": "b", "match": False, "expected": "x", "actual": "y"},
            ],
        }
    )

    assert capsys.readouterr().out == (
        "report: r.json\nstatus: drift\n"
        "  [OK] a\n  [DRIFTED] b\n"
        "      expected: x\n      actual:   y\n"
    )


def test_render_check_text_input_not_found_and_advisories(capsys):
    render_check_text(
        {
            "report": "r.json",
            "status": "drift",
            "checks": [
                {
                    "field": "layout_sha",
                    "match": False,
                    "expected": "x",
                    "actual": None,
                    "input_not_found": {
                        "resolved": "/cwd/a.gds",
                        "found_relative_to_report": "/rep/a.gds",
                    },
                }
            ],
            "advisories": [{"field": "engine", "report": "1", "current": "2"}],
        }
    )

    out = capsys.readouterr().out
    assert "actual:   None (input not found: /cwd/a.gds)" in out
    assert "exists relative to the report's own directory (/rep/a.gds)" in out
    assert "  [DRIFT] engine: 1 (report) vs 2 (current)" in out
