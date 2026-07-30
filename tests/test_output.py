"""Unit tests for the shared `klayout_tools.cli.output` envelope helper."""

import json

from klayout_tools.cli.output import emit_error, emit_success


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


def test_emit_error_text_plain_line(capsys):
    exit_code = emit_error("layers", "file not found: missing.gds", "text")

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "klt layers: file not found: missing.gds\n"
