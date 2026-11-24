"""`klt functional-verification --mutations`: the `<proposals>` document is
validated all-at-once, not one field per run (issue #5679), and `--help`
carries a complete example.

Kept apart from ``test_functional_verification.py`` (size ratchet). Every
failure below is raised before the baseline build, so nothing needs cocotb.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import functional_verification as fv
from klayout_tools.cli import main
from klayout_tools.functional_verification import FunctionalVerificationError
from klayout_tools.functional_verification_mutation_proposals import (
    MUTATION_PROPOSALS_EXAMPLE,
    functional_verification_doc_location,
    parse_mutation_proposals,
)

_FIELDS = ("index", "category", "file", "line", "original_code", "mutated_code")


def _write(path: Path, doc) -> str:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def _request(tmp_path: Path, sources=("dut.v",)) -> str:
    (tmp_path / "dut.v").write_text("module dut();\nendmodule\n", encoding="utf-8")
    (tmp_path / "tb.py").write_text("", encoding="utf-8")
    return _write(
        tmp_path / "request.json",
        {"sources": list(sources), "hdl_toplevel": "dut", "testbench": "tb.py"},
    )


def _full_entry(**overrides) -> dict:
    entry = {
        "index": 1,
        "category": "operator",
        "file": "dut.v",
        "line": 1,
        "original_code": "a",
        "mutated_code": "b",
    }
    entry.update(overrides)
    return entry


def _error_text(tmp_path: Path, doc, sources=("dut.v",)) -> str:
    with pytest.raises(FunctionalVerificationError) as excinfo:
        fv.run_functional_verification_with_mutations(
            _request(tmp_path, sources), _write(tmp_path / "p.json", doc)
        )
    return str(excinfo.value)


def test_empty_document_reports_both_top_level_fields(tmp_path):
    text = _error_text(tmp_path, {})
    assert text.startswith("--mutations input has 2 validation errors: ")
    assert "missing required field: scope" in text
    assert "missing required field: proposals" in text


def test_entry_missing_everything_reports_all_six_fields(tmp_path):
    text = _error_text(tmp_path, {"scope": ["dut.v"], "proposals": [{}]})
    for field in _FIELDS:
        assert f"proposals[0] is missing required field: {field}" in text


def test_report_quoted_sequence_collapses_into_one_run(tmp_path):
    """The report's eleven-run loop: `{}`-ish document, out-of-scope `scope`
    entry, an entry missing every field -- all named by one invocation."""
    text = _error_text(tmp_path, {"scope": ["a.v"], "proposals": [{}]})
    assert "not present in request.sources: a.v" in text
    assert "request.sources has: dut.v" in text  # names what IS available
    for field in _FIELDS:
        assert f"proposals[0] is missing required field: {field}" in text


def test_errors_accumulate_across_entries(tmp_path):
    doc = {
        "scope": ["dut.v"],
        "proposals": [
            {"index": 1},
            _full_entry(index=2, line="x"),
            "not-an-object",
        ],
    }
    text = _error_text(tmp_path, doc)
    assert "proposals[0] is missing required field: category" in text
    assert "proposals[1].line must be an integer" in text
    assert "proposals[2] must be a JSON object" in text


def test_bad_index_reported_alongside_other_entry_missing_field(tmp_path):
    doc = {"scope": ["dut.v"], "proposals": [_full_entry(index=0), {"index": 5}]}
    text = _error_text(tmp_path, doc)
    assert "unique and positive (got 0)" in text
    assert "proposals[1] is missing required field: file" in text


@pytest.mark.parametrize(
    ("indices", "invalid"), [([0, 0], [0]), ([-1, 0, 0, 2, 2], [-1, 0, 2])]
)
def test_repeated_invalid_indices_are_reported_once(tmp_path, indices, invalid):
    doc = {"scope": ["dut.v"], "proposals": [_full_entry(index=i) for i in indices]}
    text = _error_text(tmp_path, doc)
    messages = [
        f"mutation proposals[].index must be unique and positive (got {i})"
        for i in invalid
    ]
    assert text.endswith("; ".join(messages))
    assert text.count("must be unique and positive") == len(invalid)


def test_duplicate_index_survives_type_error_on_another_entry(tmp_path):
    doc = {
        "scope": ["dut.v"],
        "proposals": [
            _full_entry(index=3),
            _full_entry(index=3, category=""),
            _full_entry(index=4, file=7),
        ],
    }
    text = _error_text(tmp_path, doc)
    assert "unique and positive (got 3)" in text
    assert "proposals[1].category must be a non-empty string" in text
    assert "proposals[2].file must be a non-empty string" in text


def test_single_error_message_is_unchanged(tmp_path):
    text = _error_text(tmp_path, {"proposals": []})
    assert text == "mutation proposals file is missing required field: scope"


def test_proposals_not_an_array_and_bad_scope_both_reported(tmp_path):
    text = _error_text(tmp_path, {"scope": "dut.v", "proposals": {}})
    assert "'scope' must be an array of non-empty strings" in text
    assert "'proposals' must be an array" in text


def test_malformed_request_still_reports_proposals_errors(tmp_path):
    bad_request = _write(tmp_path / "request.json", {"sources": ["dut.v"]})
    proposals = _write(tmp_path / "p.json", {})
    with pytest.raises(FunctionalVerificationError) as excinfo:
        fv.run_functional_verification_with_mutations(bad_request, proposals)
    text = str(excinfo.value)
    assert text.startswith("--mutations input has 3 validation errors: ")
    assert "missing required field: scope" in text
    assert "missing required field: proposals" in text
    assert "missing required field: hdl_toplevel" in text


def test_malformed_request_alone_is_unchanged(tmp_path):
    bad_request = _write(tmp_path / "request.json", {"sources": ["dut.v"]})
    proposals = _write(tmp_path / "p.json", {"scope": [], "proposals": []})
    with pytest.raises(FunctionalVerificationError, match="missing required field"):
        fv.run_functional_verification_with_mutations(bad_request, proposals)


def test_valid_entries_still_load(tmp_path):
    path = _write(
        tmp_path / "p.json", {"scope": ["dut.v"], "proposals": [_full_entry()]}
    )
    scope, proposals = fv.load_mutation_proposals(path)
    assert scope == ["dut.v"]
    assert [p.index for p in proposals] == [1]


def test_help_example_is_complete_and_valid(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["functional-verification", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for field in ("scope", "proposals", *_FIELDS):
        assert f'"{field}"' in out
    assert '"index": 1,' in out
    assert functional_verification_doc_location() in out
    # The example is copy-pasteable: it validates as a document.
    path = _write(tmp_path / "example.json", MUTATION_PROPOSALS_EXAMPLE)
    _scope, _proposals, _indices, errors = parse_mutation_proposals(path)
    assert errors == []


def test_doc_location_prefers_bundled_copy(tmp_path, monkeypatch):
    import klayout_tools.functional_verification_mutation_proposals as mod

    pkg = tmp_path / "klayout_tools"
    (pkg / "data" / "cli").mkdir(parents=True)
    bundled = pkg / "data" / "cli" / "functional-verification.md"
    bundled.write_text("x", encoding="utf-8")
    monkeypatch.setattr(mod, "__file__", str(pkg / "mod.py"))
    assert mod.functional_verification_doc_location() == str(bundled.resolve())

    # Every pointer the verb's --help prints resolves to that same bundled
    # file (#6418). Check the subparser *description* itself: a match
    # anywhere in --help already passes via the epilog and `--mutations`.
    from klayout_tools.cli.parser import create_parser

    sub = next(
        a for a in create_parser()._actions if hasattr(a, "choices") and a.choices
    ).choices["functional-verification"]
    assert str(bundled.resolve()) in " ".join(sub.description.split())
    assert "docs/cli/functional-verification.md" not in sub.description
    assert "cocotb-verification-spike" not in sub.description
