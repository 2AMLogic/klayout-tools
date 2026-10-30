"""Tests for ``klt layout-plan check`` (issue #4649)."""

from __future__ import annotations

import json

import pytest

from klayout_tools import pdk
from klayout_tools.cli import main
from klayout_tools.layout_plan_check import check_layout_plan_document
from klayout_tools.layout_plan_execute import execute_layout_plan_document


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


def _strip(group_id, length_um, width_um):
    return {
        "id": group_id,
        "devices": [],
        "generator": "resistor_strip",
        "params": {
            "length_um": length_um,
            "width_um": width_um,
            "num": 1,
            "spacing_um": 0.0,
        },
    }


@pytest.fixture()
def plan(tmp_path):
    root = tmp_path / "pdk_install"
    (root / "sky130A" / "libs.tech").mkdir(parents=True)
    (tmp_path / "empty.spice").write_text(".subckt empty\n.ends\n")
    request = {
        "netlist": {"path": "empty.spice", "top": "empty"},
        "pdk": {"variant": "sky130A", "root": str(root)},
        "device_groups": [
            _strip("a", 2.0, 1.0),
            _strip("b", 2.0, 1.0),
            _strip("c", 2.0, 1.0),
            _strip("ring", 1.0, 1.0),
        ],
        "rows": [{"order": ["a", "b", "c"], "spacing_um": 1.0, "align": "bottom"}],
        "abutment": [{"a": "c", "b": "ring", "edge": "right", "gap_um": 0.5}],
        "options": {"output": str(tmp_path / "plan.gds")},
    }
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    return request, response["gds_path"], tmp_path


def _mutate(gds_path, out_path, fn):
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(gds_path)
    top = layout.top_cells()[0]
    fn(layout, top)
    layout.write(out_path)
    return out_path


def _move(prefix, dx_um):
    def fn(layout, top):
        for inst in list(top.each_inst()):
            if inst.cell.name.startswith(prefix):
                inst.transform(kdb_trans(layout, dx_um))

    return fn


def kdb_trans(layout, dx_um):
    import klayout.db as kdb

    return kdb.ICplxTrans(1.0, 0, False, int(round(dx_um / layout.dbu)), 0)


def _violations(response):
    return {
        (v["check"], str(v.get("pair") or v.get("group") or v.get("row")))
        for v in response["violations"]
    }


def test_executed_layout_is_compliant(plan):
    request, gds, tmp = plan
    response = check_layout_plan_document(request, gds, request_dir=str(tmp))
    assert response["compliant"], response["violations"]
    assert response["kind"] == "generic" and response["status"] == "pass"


def test_group_moved_out_of_row_order(plan):
    request, gds, tmp = plan
    bad = _mutate(gds, str(tmp / "moved.gds"), _move("a__", 20.0))
    response = check_layout_plan_document(request, bad, request_dir=str(tmp))
    assert any(v["check"] == "row_order" for v in response["violations"])
    assert response["status"] == "fail"


def test_changed_abutment_gap(plan):
    request, gds, tmp = plan
    bad = _mutate(gds, str(tmp / "gap.gds"), _move("ring__", 3.0))
    response = check_layout_plan_document(request, bad, request_dir=str(tmp))
    assert [v["check"] for v in response["violations"]] == ["abutment"]


def test_removed_group_cell(plan):
    request, gds, tmp = plan

    def drop(layout, top):
        for inst in list(top.each_inst()):
            if inst.cell.name.startswith("b__"):
                inst.delete()

    bad = _mutate(gds, str(tmp / "gone.gds"), drop)
    # Deleting the instance leaves the orphaned sub-cell as a second top cell.
    response = check_layout_plan_document(
        request, bad, request_dir=str(tmp), cell_name="layout_plan_0"
    )
    assert ("group_present", "b") in _violations(response)
    assert response["violations"][0]["observed"] == "group_missing"


def test_cli_exit_codes_and_signoff_accepts_envelope(plan, tmp_path, capsys):
    request, gds, tmp = plan
    req_path = tmp / "req.json"
    req_path.write_text(json.dumps(request))
    assert (
        main(
            ["layout-plan", "check", str(req_path), "--layout", gds, "--format", "json"]
        )
        == 0
    )
    out = capsys.readouterr().out
    env_path = tmp / "check.json"
    env_path.write_text(out)
    assert json.loads(out)["schema_version"] == 1

    bad = _mutate(gds, str(tmp / "moved.gds"), _move("a__", 20.0))
    assert (
        main(
            ["layout-plan", "check", str(req_path), "--layout", bad, "--format", "json"]
        )
        == 3
    )
    capsys.readouterr()

    assert main(["signoff", str(env_path), "--format", "json"]) == 0
    signoff = json.loads(capsys.readouterr().out)
    assert signoff["checks"][0]["kind"] == "generic"
    assert signoff["checks"][0]["passed"] is True


def test_missing_layout_is_error(plan, tmp_path):
    request, _, tmp = plan
    req_path = tmp / "req.json"
    req_path.write_text(json.dumps(request))
    assert (
        main(["layout-plan", "check", str(req_path), "--layout", str(tmp / "nope.gds")])
        == 1
    )
