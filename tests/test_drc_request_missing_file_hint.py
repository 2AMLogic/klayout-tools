"""Issue #5207: a missing relative ``file`` in a ``klt drc`` request *file*
names the base directory it was resolved against."""

import json

from klayout_tools.cli import main


def _error_message(capsys, argv: list[str]) -> str:
    capsys.readouterr()
    assert main(["drc", *argv, "--format", "json"]) == 1
    return json.loads(capsys.readouterr().err)["error"]["message"]


def test_relative_file_in_request_file_names_base_dir(tmp_path, capsys):
    req_dir = tmp_path / "req"
    req_dir.mkdir()
    request_path = req_dir / "request.json"
    request_path.write_text(json.dumps({"file": "layout/top.gds", "deck": "sky130"}))

    message = _error_message(capsys, [str(request_path)])

    assert f"file not found: {req_dir / 'layout/top.gds'}" in message
    assert f"request document's directory: {req_dir}" in message


def test_absolute_file_in_request_file_has_no_hint(tmp_path, capsys):
    absolute = tmp_path / "nope.gds"
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps({"file": str(absolute), "deck": "sky130"}))

    assert _error_message(capsys, [str(request_path)]) == f"file not found: {absolute}"


def test_inline_request_has_no_hint(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)

    message = _error_message(
        capsys, [json.dumps({"file": "nope.gds", "deck": "sky130"})]
    )

    assert "file not found:" in message
    assert "request document's directory" not in message
