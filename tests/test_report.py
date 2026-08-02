"""Tests for `klt report` and the `build_report` library function.

Fixtures are hand-built JSON envelope dicts matching the documented shapes
of `klt drc` (docs/cli/drc.md), `klt lvs` (docs/cli/lvs.md), and `klt
layout-metrics` (docs/cli/layout-metrics.md) -- no dependency on actually
running those commands, mirroring the "no external corpus needed"
convention of tests/test_drc.py / tests/test_layout_metrics.py.
"""

from __future__ import annotations

import io
import json

import pytest

from klayout_tools.cli import main
from klayout_tools.report import ReportError, build_report

DRC_VIOLATIONS_ENVELOPE = {
    "schema_version": 1,
    "file": "design.gds",
    "deck": "sky130",
    "dbu_um": 0.001,
    "status": "violations",
    "violation_count": 1,
    "rule_counts": {"poly.width.1": 1},
    "violations": [
        {
            "rule": "poly.width.1",
            "description": "minimum poly width",
            "check": "width",
            "layer": "poly.drawing",
            "cell": "TOP",
            "bbox": {"left": 0, "bottom": 0, "right": 100, "top": 2000},
            "polygon": None,
        }
    ],
    "coverage": {
        "deck_layers": ["65/20"],
        "layers_checked": ["65/20"],
        "layers_in_stream_without_rules": [],
        "rules_skipped": [],
    },
}

DRC_CLEAN_ENVELOPE = {
    **DRC_VIOLATIONS_ENVELOPE,
    "status": "clean",
    "violation_count": 0,
    "rule_counts": {},
    "violations": [],
}

LVS_MISMATCH_ENVELOPE = {
    "schema_version": 1,
    "engine": "klayout",
    "layout": "design.spice",
    "reference": "golden.spice",
    "top": "INV",
    "status": "mismatch",
    "mismatch_count": 1,
    "category_counts": {"net.unmatched": 1},
    "counts": {
        "nets": {"layout": 2, "reference": 3, "matched": 2},
        "devices": {"layout": 2, "reference": 2, "matched": 2},
        "pins": {"layout": 2, "reference": 2, "matched": 2},
    },
    "device_classes": ["nfet", "pfet"],
    "environment": {
        "engine": "klayout",
        "engine_version": "0.30.10",
        "layout_sha256": "abc",
        "reference_sha256": "def",
        "extracted_netlist": None,
    },
    "mismatches": [
        {
            "category": "net.unmatched",
            "severity": "error",
            "description": "reference net has no layout counterpart",
            "side": "reference",
            "net": {"layout": None, "reference": "VDD"},
            "device": None,
            "property": None,
        }
    ],
}

LVS_MATCH_ENVELOPE = {
    **LVS_MISMATCH_ENVELOPE,
    "status": "match",
    "mismatch_count": 0,
    "category_counts": {},
    "mismatches": [],
}

LAYOUT_METRICS_OK_ENVELOPE = {
    "schema_version": 1,
    "generated_at": "2026-08-01T00:00:00+00:00",
    "slug": "example-block",
    "name": "Example Block",
    "layout_file": "layout.gds",
    "layer_count": 12,
    "cell_count": 34,
    "instance_count": 120,
    "drc": {"deck": "sky130", "status": "clean", "violation_count": 0},
    "renders": {"metal1": "renders/metal1.png"},
    "status": "ok",
}

LAYOUT_METRICS_NO_ARTIFACTS_ENVELOPE = {
    "schema_version": 1,
    "generated_at": "2026-08-01T00:00:00+00:00",
    "slug": "empty-block",
    "name": "Empty Block",
    "status": "no_artifacts",
}

DRC_ERROR_ENVELOPE = {
    "schema_version": 1,
    "error": {"command": "drc", "message": "file not found: missing.gds"},
}


def _write(tmp_path, name: str, payload: dict) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return str(path)


# --------------------------------------------------------------------------- #
# build_report(): per-kind section shape
# --------------------------------------------------------------------------- #


def test_drc_envelope_renders_violations_table(tmp_path):
    path = _write(tmp_path, "drc.json", DRC_VIOLATIONS_ENVELOPE)

    report = build_report([path])

    assert report["schema_version"] == 1
    assert report["envelope_count"] == 1
    section = report["sections"][0]
    assert section["kind"] == "drc"
    assert section["source"] == path
    assert section["status"] == "violations"
    assert section["item_count"] == 1
    assert "poly.width.1" in report["markdown"]
    assert "| Rule | Cell | Layer | BBox | Description |" in report["markdown"]
    assert "poly.width.1" in report["text"]


def test_drc_clean_envelope_renders_no_violations_message_not_empty_table(tmp_path):
    path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)

    report = build_report([path])

    assert report["sections"][0]["item_count"] == 0
    assert "No violations found." in report["markdown"]
    assert "| Rule |" not in report["markdown"]
    assert "No violations found." in report["text"]


def test_lvs_envelope_renders_mismatches_table(tmp_path):
    path = _write(tmp_path, "lvs.json", LVS_MISMATCH_ENVELOPE)

    report = build_report([path])

    section = report["sections"][0]
    assert section["kind"] == "lvs"
    assert section["status"] == "mismatch"
    assert section["item_count"] == 1
    assert "| Category | Severity | Side | Description |" in report["markdown"]
    assert "net.unmatched" in report["markdown"]


def test_lvs_match_envelope_renders_no_mismatches_message(tmp_path):
    path = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)

    report = build_report([path])

    assert report["sections"][0]["item_count"] == 0
    assert "No mismatches found." in report["markdown"]


def test_layout_metrics_envelope_renders_key_metrics(tmp_path):
    path = _write(tmp_path, "metrics.json", LAYOUT_METRICS_OK_ENVELOPE)

    report = build_report([path])

    section = report["sections"][0]
    assert section["kind"] == "layout-metrics"
    assert section["status"] == "ok"
    assert section["item_count"] is None
    assert "example-block" in report["markdown"]
    assert "| Layer count | 12 |" in report["markdown"]
    assert "DRC: deck=sky130 status=clean violations=0" in report["markdown"]
    assert "Renders: metal1" in report["markdown"]


def test_layout_metrics_omits_absent_fields_not_null(tmp_path):
    path = _write(tmp_path, "metrics.json", LAYOUT_METRICS_NO_ARTIFACTS_ENVELOPE)

    report = build_report([path])

    markdown = report["markdown"]
    assert "empty-block" in markdown
    # No layer/cell/instance counts were present in the source envelope --
    # they must not appear as null/None/undefined rows.
    assert "null" not in markdown
    assert "None" not in markdown
    assert "Layer count" not in markdown
    assert "DRC:" not in markdown
    assert "Renders:" not in markdown


def test_error_envelope_renders_command_and_message(tmp_path):
    path = _write(tmp_path, "error.json", DRC_ERROR_ENVELOPE)

    report = build_report([path])

    section = report["sections"][0]
    assert section["kind"] == "error"
    assert section["status"] == "error"
    assert "## Error: drc" in report["markdown"]
    assert "file not found: missing.gds" in report["markdown"]


# --------------------------------------------------------------------------- #
# Multi-envelope combination
# --------------------------------------------------------------------------- #


def test_multiple_envelopes_combine_in_given_order(tmp_path):
    drc_path = _write(tmp_path, "drc.json", DRC_VIOLATIONS_ENVELOPE)
    metrics_path = _write(tmp_path, "metrics.json", LAYOUT_METRICS_OK_ENVELOPE)

    report = build_report([drc_path, metrics_path])

    assert report["envelope_count"] == 2
    assert [s["kind"] for s in report["sections"]] == ["drc", "layout-metrics"]
    markdown = report["markdown"]
    assert markdown.index("DRC Report") < markdown.index("Layout Metrics")


# --------------------------------------------------------------------------- #
# Malformed / unrecognized envelopes
# --------------------------------------------------------------------------- #


def test_missing_file_raises(tmp_path):
    with pytest.raises(ReportError, match="file not found"):
        build_report([str(tmp_path / "nope.json")])


def test_directory_raises(tmp_path):
    with pytest.raises(ReportError, match="not a file"):
        build_report([str(tmp_path)])


def test_malformed_json_raises(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")

    with pytest.raises(ReportError, match="not valid JSON"):
        build_report([str(path)])


def test_non_object_json_raises(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]")

    with pytest.raises(ReportError, match="must be a JSON object"):
        build_report([str(path)])


def test_missing_schema_version_raises(tmp_path):
    path = _write(tmp_path, "no_version.json", {"foo": "bar"})

    with pytest.raises(ReportError, match="schema_version"):
        build_report([path])


def test_unrecognized_shape_raises(tmp_path):
    path = _write(tmp_path, "unknown.json", {"schema_version": 1, "foo": "bar"})

    with pytest.raises(ReportError, match="unrecognized shape"):
        build_report([path])


def test_no_sources_raises():
    with pytest.raises(ReportError, match="at least one"):
        build_report([])


def test_table_cell_pipe_is_escaped_in_markdown(tmp_path):
    envelope = {
        **DRC_VIOLATIONS_ENVELOPE,
        "violations": [
            {
                **DRC_VIOLATIONS_ENVELOPE["violations"][0],
                "description": "a | pipe in the description",
            }
        ],
    }
    path = _write(tmp_path, "drc.json", envelope)

    report = build_report([path])

    assert "a \\| pipe in the description" in report["markdown"]


# --------------------------------------------------------------------------- #
# CLI (`klt report`)
# --------------------------------------------------------------------------- #


def test_cli_json_format(tmp_path, capsys):
    path = _write(tmp_path, "drc.json", DRC_VIOLATIONS_ENVELOPE)

    exit_code = main(["report", path, "--format", "json"])

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema_version"] == 1
    assert out["sections"][0]["kind"] == "drc"
    assert "poly.width.1" in out["markdown"]


def test_cli_github_summary_format(tmp_path, capsys):
    path = _write(tmp_path, "drc.json", DRC_VIOLATIONS_ENVELOPE)

    exit_code = main(["report", path, "--format", "github-summary"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.startswith("## DRC Report")
    assert "| Rule | Cell | Layer | BBox | Description |" in out


def test_cli_text_default_format(tmp_path, capsys):
    path = _write(tmp_path, "drc.json", DRC_VIOLATIONS_ENVELOPE)

    exit_code = main(["report", path])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "DRC Report" in out
    assert "## " not in out
    assert "|" not in out


def test_cli_multiple_files(tmp_path, capsys):
    drc_path = _write(tmp_path, "drc.json", DRC_VIOLATIONS_ENVELOPE)
    metrics_path = _write(tmp_path, "metrics.json", LAYOUT_METRICS_OK_ENVELOPE)

    exit_code = main(["report", drc_path, metrics_path, "--format", "github-summary"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "DRC Report" in out
    assert "Layout Metrics" in out


def test_cli_stdin_input(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(DRC_VIOLATIONS_ENVELOPE)))

    exit_code = main(["report", "-", "--format", "json"])

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sections"][0]["kind"] == "drc"


def test_cli_missing_file_exits_one_json_error(tmp_path, capsys):
    exit_code = main(["report", str(tmp_path / "missing.json"), "--format", "json"])

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "report"
    assert "file not found" in err["error"]["message"]
    assert capsys.readouterr().out == ""


def test_cli_missing_file_exits_one_text_error(tmp_path, capsys):
    exit_code = main(["report", str(tmp_path / "missing.json")])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert err.startswith("klt report:")


def test_cli_no_files_is_usage_error():
    with pytest.raises(SystemExit) as exc_info:
        main(["report"])
    assert exc_info.value.code == 2
