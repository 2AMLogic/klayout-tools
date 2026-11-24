"""Issue #4314: a ``sim``-only digital item 5 (the full-custom sub-case) needs
a measured per-corner timing margin, declared as a ``timing_margin`` spec row."""

from __future__ import annotations

import copy
import json

import pytest

from klayout_tools.signoff import build_tier_report

CORNERS = ["tt/1.8V/27C", "ss/1.6V/125C"]


def _envelope(
    *, tag="slack", limits=True, status="pass", missing_at=None, fail_at=None
):
    corners = []
    for cid in CORNERS:
        ms = []
        if cid != missing_at:
            ms.append(
                {"name": "slack_ns", "status": "fail" if cid == fail_at else "pass"}
            )
        corners.append({"corner_id": cid, "status": "pass", "measurements": ms})
    rollup = {"name": "slack_ns", "status": status}
    if tag:
        rollup["spec_row"] = tag
    if limits:
        rollup["limits"] = {"min": 0.0}
    return {
        "schema_version": 1,
        "netlist": "design.spice",
        "status": "pass",
        "corner_count": 2,
        "passed": 2,
        "failed": 0,
        "errored": 0,
        "environment": {},
        "provenance": {
            "klt_version": "0.2.0",
            "klayout_version": "0.30.10",
            "pdk": {"name": "sky130A", "source": "volare", "version": "20240101"},
            "deck": {"name": "sky130.lib.spice", "content_hash": "sha256:models"},
            "input": {"content_hash": "sha256:netlistA", "role": "netlist"},
        },
        "measurements": [rollup],
        "corners": corners,
    }


def _grade(tmp_path, envelope, *, rows, kind="digital", entry=None):
    path = tmp_path / "sim.json"
    path.write_text(json.dumps(envelope))
    if entry is None:
        entry = (
            {"file": str(path), "rows": [r["id"] for r in rows]} if rows else str(path)
        )
    manifest = {"block": "b", "kind": kind, "evidence": {"5": entry}}
    if rows is not None:
        manifest["spec_rows"] = {"5": rows}
    item = next(i for i in build_tier_report(manifest)["items"] if i["id"] == 5)
    return item


MARGIN = [{"id": "slack", "timing_margin": True}]


def test_declared_row_with_tag_at_all_corners_is_met(tmp_path):
    item = _grade(tmp_path, _envelope(), rows=MARGIN)
    assert item["status"] == "met"


def test_no_margin_row_declared(tmp_path):
    item = _grade(tmp_path, _envelope(), rows=None)
    assert item["reason"] == "timing_margin_missing"
    assert item["detail"]["missing_timing_margin"] == [{"row": None, "corners": []}]


def test_rows_declared_without_flag_still_missing(tmp_path):
    item = _grade(tmp_path, _envelope(), rows=[{"id": "slack"}])
    assert item["reason"] == "timing_margin_missing"


def test_missing_at_one_corner_names_it(tmp_path):
    item = _grade(tmp_path, _envelope(missing_at=CORNERS[1]), rows=MARGIN)
    assert item["reason"] == "timing_margin_missing"
    assert item["detail"]["missing_timing_margin"] == [
        {"row": "slack", "corners": [CORNERS[1]]}
    ]


def test_tag_without_limits_is_missing(tmp_path):
    item = _grade(tmp_path, _envelope(limits=False), rows=MARGIN)
    assert item["reason"] == "timing_margin_missing"


def test_manifest_asserted_rows_without_envelope_tag_is_missing(tmp_path):
    entry = {"file": str(tmp_path / "sim.json"), "rows": ["slack"]}
    item = _grade(tmp_path, _envelope(tag=None), rows=MARGIN, entry=entry)
    assert item["reason"] == "timing_margin_missing"


def test_failing_margin_is_check_failed(tmp_path):
    env = _envelope(status="fail", fail_at=CORNERS[0])
    env["status"] = "fail"
    env["failed"] = 1
    item = _grade(tmp_path, env, rows=MARGIN)
    assert item["status"] == "unmet"
    assert item["reason"] == "check_failed"


def test_analog_ignores_timing_margin_flag(tmp_path):
    item = _grade(tmp_path, _envelope(tag=None), rows=None, kind="analog")
    assert item["status"] == "met"
    item = _grade(tmp_path, _envelope(tag=None), rows=MARGIN, kind="analog")
    assert item["reason"] != "timing_margin_missing"


@pytest.mark.parametrize("bad", ["yes", 1, None])
def test_malformed_flag_is_invalid_evidence(tmp_path, bad):
    rows = copy.deepcopy(MARGIN)
    rows[0]["timing_margin"] = bad
    item = _grade(tmp_path, _envelope(), rows=rows)
    assert item["reason"] == "invalid_evidence"
