"""Record and replay the cell selected for an ERC run (#7022)."""

import json

import klayout.db as kdb
import pytest

from klayout_tools.erc import ErcError, rerun_erc_report, run_erc


def _inputs(directory, names):
    (directory / ".git").mkdir()
    layout = kdb.Layout()
    for name in names:
        cell = layout.create_cell(name)
        cell.shapes(layout.layer(1, 0)).insert(kdb.Box(0, 0, 1000, 1000))
        cell.shapes(layout.layer(2, 0)).insert(kdb.Box(2000, 0, 3000, 1000))
    stream = directory / "cells.gds"
    layout.write(str(stream))
    spec = directory / "erc.json"
    spec.write_text(
        json.dumps(
            {
                "stackup": [
                    {"name": "poly", "layer": "1/0", "role": "gate"},
                    {"name": "M1", "layer": "2/0"},
                ]
            }
        )
    )
    return str(stream), str(spec)


@pytest.mark.parametrize(
    "flags",
    [
        {},
        {"findings_only": True, "compact": True},
        {"findings_only": True, "omit_antenna_tables": True},
    ],
)
def test_replay_selects_the_same_top(tmp_path, flags):
    inputs = _inputs(tmp_path, ["LEFT", "RIGHT"])
    saved = tmp_path / "report.json"
    reports = []
    for name in ("LEFT", "RIGHT"):
        report = run_erc(*inputs, top=name, **flags)
        assert report["top"] == name
        saved.write_text(json.dumps(report))
        result = rerun_erc_report(str(saved))
        assert result["status"] == "match"
        assert result["fresh"]["top"] == name
        reports.append({key: value for key, value in report.items() if key != "top"})
    assert reports[0] == reports[1]


def test_auto_selected_top_and_legacy_diff(tmp_path):
    inputs = _inputs(tmp_path, ["ONLY"])
    report = run_erc(*inputs)
    assert report.pop("top") == "ONLY"
    saved = tmp_path / "report.json"
    saved.write_text(json.dumps(report))
    result = rerun_erc_report(str(saved))
    assert result["status"] == "match"
    assert result["fresh"]["top"] == "ONLY"
    report["gate_count"] += 1
    saved.write_text(json.dumps(report))
    assert [d["field"] for d in rerun_erc_report(str(saved))["drift"]] == ["gate_count"]


def test_missing_saved_cell_reports_the_ordinary_lookup_error(tmp_path):
    inputs = _inputs(tmp_path, ["ONLY"])
    report = {**run_erc(*inputs), "top": "MISSING"}
    saved = tmp_path / "report.json"
    saved.write_text(json.dumps(report))
    with pytest.raises(ErcError, match="top cell not found in stream: MISSING"):
        rerun_erc_report(str(saved))


def test_malformed_saved_cell_is_a_report_error(tmp_path):
    inputs = _inputs(tmp_path, ["ONLY"])
    saved = tmp_path / "report.json"
    saved.write_text(json.dumps({**run_erc(*inputs), "top": 0}))
    with pytest.raises(ErcError, match="'top' must name a cell"):
        rerun_erc_report(str(saved))
