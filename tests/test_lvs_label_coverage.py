"""LVS diagnostic coverage on the stream and circuits actually compared."""

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.cli.lvs_cmd import _print_text
from klayout_tools.extract import run_extract
from klayout_tools.lvs import rerun_lvs_report, run_lvs
from klayout_tools.lvs_label_coverage import pin_set_difference
from test_lvs import (
    _GATE_LEVEL_LAYOUT_SPICE,
    _GATE_LEVEL_LIBRARY_SPICE,
    _GATE_LEVEL_REFERENCE_VERILOG_PORT_ALIAS,
    _label_layers_fixture,
    _label_layers_request,
    _make_fake_pdk_library,
    _write,
    _write_report,
)


def _stream(tmp_path, *, other_text=True, child_text=True):
    moved, ref = _label_layers_fixture(tmp_path)
    db = kdb.Layout()
    db.read(moved)
    top = db.top_cell()
    for index in db.layer_indexes():
        layer = db.get_info(index)
        if other_text and (layer.layer, layer.datatype) == (67, 10):
            continue
        for shape in list(top.shapes(index).each(kdb.Shapes.STexts)):
            shape.delete()
    # Native pin text in a different cell must not mask the top-cell diagnosis.
    if child_text:
        child = db.create_cell("CHILD")
        child.shapes(db.layer(67, 5)).insert(kdb.Text("internal", 0, 0))
        top.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(10000, 10000)))
    db.write(moved)
    return moved, ref


@pytest.mark.parametrize("use_override", [False, True])
def test_inline_label_coverage_agrees_with_extract(tmp_path, use_override, capsys):
    moved, ref = _stream(tmp_path)
    mapping = {"metal0": [67, 10]} if use_override else None
    report = run_lvs(json.dumps(_label_layers_request(moved, ref, mapping)))
    extract = run_extract(
        moved,
        "sky130",
        output=str(tmp_path / "inline.spice"),
        label_layers={"metal0": (67, 10)} if use_override else None,
    )
    assert report["ignored_layers"] == extract["ignored_layers"]
    _print_text(report)
    text = capsys.readouterr().out.splitlines()
    assert [line for line in text if line.startswith("warning: ")] == [
        f"warning: {w}" for w in report["warnings"]
    ]
    if use_override:
        assert report["warnings"] == []
        assert report["counts"]["pins"]["layout"] > 1
    else:
        assert {"layer": 67, "datatype": 10, "shapes": 5} in report["ignored_layers"]
        (warning,) = report["warnings"]
        assert all(token in warning for token in ("67/10", "layout.label_layers"))
        diff = report["pin_set_difference"]
        assert "A" in diff["reference_only_pins"]
        assert diff["deck_synthesized_layout_pins"] == ["vsubs"]
        assert "vsubs" not in diff["layout_only_pins"]


@pytest.mark.parametrize("child_text", [True, False])
def test_top_with_no_text_is_quiet(tmp_path, child_text):
    moved, ref = _stream(tmp_path, other_text=False, child_text=child_text)
    assert run_lvs(json.dumps(_label_layers_request(moved, ref)))["warnings"] == []


def test_old_report_replays_and_new_coverage_drift_is_visible(tmp_path):
    moved, ref = _stream(tmp_path)
    report = run_lvs(json.dumps(_label_layers_request(moved, ref)))
    path = _write_report(tmp_path / "report.json", report)
    assert (
        rerun_lvs_report(path, layout_path=moved, reference_path=ref)["status"]
        == "match"
    )
    report["ignored_layers"] = []
    path = _write_report(tmp_path / "report.json", report)
    assert (
        rerun_lvs_report(path, layout_path=moved, reference_path=ref)["status"]
        == "drifted"
    )
    for field in ("ignored_layers", "warnings", "pin_set_difference"):
        del report[field]
    path = _write_report(tmp_path / "report.json", report)
    assert (
        rerun_lvs_report(path, layout_path=moved, reference_path=ref)["status"]
        == "match"
    )


def test_preextracted_gate_level_aliases_use_the_compared_ports(tmp_path):
    root = _make_fake_pdk_library(
        tmp_path, "myvariant", "mylib", _GATE_LEVEL_LIBRARY_SPICE
    )
    layout = _write(tmp_path / "layout.spice", _GATE_LEVEL_LAYOUT_SPICE)
    ref = _write(tmp_path / "ref.v", _GATE_LEVEL_REFERENCE_VERILOG_PORT_ALIAS)
    report = run_lvs(
        json.dumps(
            {
                "layout": {"netlist": layout, "top": "top"},
                "reference": {
                    "netlist": ref,
                    "top": "top",
                    "form": "gate-level-verilog",
                    "library": "mylib",
                    "pdk": "myvariant",
                    "pdk_root": root,
                },
            }
        )
    )
    assert report["status"] == "match"
    assert report["warnings"] == report["ignored_layers"] == []
    diff = report["pin_set_difference"]
    assert diff["reference_only_pins"] == diff["layout_only_pins"] == []
    assert diff["deck_synthesized_layout_pins"] == []


def test_case_fold_and_full_counts_with_bounded_samples():
    left, right = kdb.Circuit(), kdb.Circuit()
    left.name, right.name = "left", "right"
    left.create_pin("MixedCase")
    right.create_pin("MIXEDCASE")
    for i in range(25):
        left.create_pin(f"L{i:02}")
        right.create_pin(f"R{i:02}")
    diff = pin_set_difference(left, right, None, [])
    schema_path = (
        Path(__file__).resolve().parents[1] / "docs/schemas/lvs-response.schema.json"
    )
    fields = json.loads(schema_path.read_text())["properties"]["pin_set_difference"][
        "properties"
    ]
    for side, stem in [("layout", "L"), ("reference", "R")]:
        key = f"{side}_only_pins"
        assert diff[key] == [f"{stem}{i:02}" for i in range(20)]
        assert len(diff[key]) == fields[key]["maxItems"]
        assert diff[key + "_count"] == 25
        assert diff[key + "_truncated"] is True
