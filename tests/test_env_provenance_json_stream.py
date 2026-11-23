"""`klt env-provenance lint-envelope --json-stream` (issue #5887).

A sibling of `tests/test_env_provenance.py` rather than an addition to it:
that file sits at its `file-size-baseline.json` ratchet ceiling, so the
stream-mode tests live here against the dedicated
`klayout_tools.env_provenance_json_stream` module. The rule under test: a
file of concatenated JSON documents is lintable per-document instead of
rejected as "Extra data", each finding naming its 0-based `document`, with
the default single-document mode untouched.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools import env_provenance as ep
from klayout_tools import env_provenance_json_stream as epjs
from klayout_tools.cli import main


def _write_stream(path, payloads) -> str:
    path.write_text(
        "\n".join(json.dumps(payload) for payload in payloads), encoding="utf-8"
    )
    return str(path)


def test_lint_envelope_json_stream_clean_documents_exit_clean(tmp_path):
    """The gap of issue #5887: N clean concatenated documents (one
    `json.dumps` per record) lint to a clean verdict instead of
    `Extra data`."""
    path = _write_stream(
        tmp_path / "stream.json",
        [{"def_path": "build/a.def"}, {"lib": {"path": "x.lib", "scope": "repo"}}],
    )
    report = epjs.lint_envelope_files_json_stream([path])
    assert report["status"] == "clean"
    assert report["finding_count"] == 0


def test_lint_envelope_json_stream_findings_carry_the_document_index(tmp_path):
    path = _write_stream(
        tmp_path / "stream.json",
        [
            {"def_path": "build/a.def"},
            {"def_path": "/Users/x/a.def"},
            {"def_path": "/opt/p/b.def"},
        ],
    )
    report = epjs.lint_envelope_files_json_stream([path])
    assert report["status"] == "violations"
    assert report["finding_count"] == 2
    findings = report["files"][0]["findings"]
    assert [(f["document"], f["field"]) for f in findings] == [
        (1, "def_path"),
        (2, "def_path"),
    ]


def test_lint_envelope_json_stream_bare_scalars_and_no_separator(tmp_path):
    """Edge cases from the issue's test plan: documents that are bare
    strings/numbers, and two objects with no separator between `}{`. A
    top-level bare string reports the `<root>` field."""
    path = tmp_path / "stream.json"
    path.write_text(
        '"/Users/x/a.def" 1 {"a":1}{"b":"/opt/p/b.def"}\n', encoding="utf-8"
    )
    report = epjs.lint_envelope_files_json_stream([path])
    assert [(f["document"], f["field"]) for f in report["files"][0]["findings"]] == [
        (0, "<root>"),
        (3, "b"),
    ]


def test_lint_envelope_json_stream_rejects_a_corrupt_trailing_document(tmp_path):
    path = tmp_path / "stream.json"
    path.write_text(json.dumps({"a": 1}) + '\n{"broken": ', encoding="utf-8")
    with pytest.raises(ep.EnvironmentProvenanceError, match="not valid JSON"):
        epjs.lint_envelope_files_json_stream([path])


@pytest.mark.parametrize("content", ["", "   \n\t\n"])
def test_lint_envelope_json_stream_rejects_an_empty_file(tmp_path, content):
    path = tmp_path / "stream.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ep.EnvironmentProvenanceError, match="not valid JSON"):
        epjs.lint_envelope_files_json_stream([path])


def test_lint_envelope_default_mode_still_rejects_a_stream(tmp_path):
    """Without the flag nothing changes: a concatenated stream is the
    existing single-document parse error, and the default-mode report has
    no `document` key."""
    path = _write_stream(tmp_path / "stream.json", [{"a": 1}, {"b": 2}])
    with pytest.raises(ep.EnvironmentProvenanceError, match="not valid JSON"):
        ep.lint_envelope_files([path])


def test_cli_lint_envelope_json_stream_exits_three_and_reports_documents(
    tmp_path, capsys
):
    path = _write_stream(
        tmp_path / "stream.json",
        [{"def_path": "build/a.def"}, {"def_path": "/Users/x/a.def"}],
    )
    assert main(
        ["env-provenance", "lint-envelope", path, "--json-stream", "--format", "json"]
    ) == (ep.LEAKS_FOUND_EXIT_CODE)
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "violations"
    finding = payload["files"][0]["findings"][0]
    assert finding["document"] == 1
    assert finding["field"] == "def_path"


def test_cli_lint_envelope_json_stream_clean_exits_zero(tmp_path, capsys):
    path = _write_stream(tmp_path / "stream.json", [{"a": 1}, {"b": 2}])
    assert (
        main(
            [
                "env-provenance",
                "lint-envelope",
                path,
                "--json-stream",
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "clean"


def test_cli_lint_envelope_json_stream_corrupt_tail_is_an_application_error(
    tmp_path, capsys
):
    path = tmp_path / "stream.json"
    path.write_text(json.dumps({"a": 1}) + " oops", encoding="utf-8")
    assert main(["env-provenance", "lint-envelope", str(path), "--json-stream"]) == 1
    assert "not valid JSON" in capsys.readouterr().err


def test_cli_lint_envelope_json_stream_text_output_names_the_document(tmp_path, capsys):
    path = _write_stream(
        tmp_path / "stream.json",
        [{"def_path": "build/a.def"}, {"def_path": "/Users/x/a.def"}],
    )
    main(["env-provenance", "lint-envelope", path, "--json-stream"])
    out = capsys.readouterr().out
    assert f"{path}[1]: def_path: /Users/x/a.def" in out
    assert "repo-relative" in out
