"""Strict placement distinguishes decimal contact, actual overlap and bbox growth."""

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.gen_compose import check_placement_overlap
from test_gen_compose import _isolate as _isolate
from test_gen_compose import pdk_root as pdk_root
from test_gen_compose_placement_gate import _gate_block


def _box(left, right):
    return {"x0": left, "x1": right, "y0": 0.0, "y1": 1.0}


def _run(tmp_path, pdk_root, blocks, placement):
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": blocks,
                "placement": placement,
                "options": {
                    "cell_name": "placed",
                    "output": str(tmp_path / "out.gds"),
                    "check_placement": True,
                },
            }
        )
    )
    return main(["gen-compose", str(request), "--format", "json"])


@pytest.mark.parametrize(
    "source,b_x", [("row", None), ("origins_um", 0.5), ("target_bbox_um", 0.1)]
)
def test_decimal_edge_contact_after_translation(tmp_path, pdk_root, source, b_x):
    blocks = [
        {
            "id": name,
            "generator_report": _gate_block(
                tmp_path, name, _box(x0, x1), (x0, 0.0, x1, 1.0)
            ),
        }
        for name, x0, x1 in [("a", 0.0, 0.1), ("b", -0.4, 0.6)]
    ]
    placement = {"order": ["a", "b"]}
    if source == "row":
        placement.update(strategy="row", spacing_um=0.0)
    else:
        placement.update(strategy="explicit")
        placement[source] = {"a": {"x": 0, "y": 0}, "b": {"x": b_x, "y": 0}}
    assert _run(tmp_path, pdk_root, blocks, placement) == 0
    assert (tmp_path / "out.gds").is_file()


@pytest.mark.parametrize("source", ["generator_report", "cell"])
@pytest.mark.parametrize("axis", ["x", "y"])
def test_invalid_box_produces_cli_error(tmp_path, pdk_root, capsys, source, axis):
    box = _box(0, 1)
    box[axis + "1"] = -1
    report = _gate_block(tmp_path, "a", box, (0, 0, 1, 1))
    block = (
        report
        if source == "generator_report"
        else {k: report[k] for k in ("gds_path", "cell_name", "bbox_um")}
    )
    code = _run(
        tmp_path,
        pdk_root,
        [{"id": "a", source: block}],
        {"strategy": "row", "order": ["a"]},
    )
    assert code == 1
    assert (
        "footprint of block 'a'"
        in json.loads(capsys.readouterr().err)["error"]["message"]
    )
    assert not (tmp_path / "out.gds").exists()


@pytest.mark.parametrize("edge,sign", [("x0", -1), ("y0", -1), ("x1", 1), ("y1", 1)])
def test_growth_threshold_is_inclusive_on_each_edge(edge, sign):
    declared = {"x0": -2.0, "x1": 2.0, "y0": -2.0, "y1": 2.0}

    def check(distance):
        measured = {**declared, edge: declared[edge] + sign * distance}
        return check_placement_overlap({"a": declared}, {"a": measured})

    assert check(0.0001) == []
    assert len(check(0.00010001)) == 1
