"""Round-trip and CLI error contracts for editing a hierarchical base layout."""

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.draw import draw

PATCH = {"layer": [67, 20], "rect_um": [2, 2, 3, 3]}


@pytest.fixture
def seed(tmp_path):
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    child = layout.create_cell("CHILD")
    top.shapes(layout.layer(67, 20)).insert(kdb.Box(0, 0, 500, 500))
    child.shapes(layout.layer(68, 20)).insert(kdb.Box(0, 0, 1000, 1000))
    top.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(4000, 0)))
    path = tmp_path / "seed.gds"
    layout.write(str(path))
    return path


def test_keep_hierarchy_patches_top_in_place(seed):
    report = draw(
        {
            "params": {
                "base_gds": str(seed),
                "keep_hierarchy": True,
                "erase_um": [],
                "shapes": [PATCH],
                "labels": [{"layer": [67, 20], "text": "PATCH", "at_um": [2, 2]}],
            },
            "options": {"output": str(seed)},
        }
    )

    layout = kdb.Layout()
    layout.read(str(seed))
    top = layout.top_cell()
    assert layout.cells() == 2
    (instance,) = top.each_inst()
    assert instance.cell.name == "CHILD"
    assert instance.trans == kdb.Trans(4000, 0)
    (child_shape,) = instance.cell.shapes(layout.layer(68, 20)).each()
    assert child_shape.box == kdb.Box(0, 0, 1000, 1000)
    shapes = list(top.shapes(layout.layer(67, 20)).each())
    assert sorted(s.box for s in shapes if s.is_box()) == [
        kdb.Box(0, 0, 500, 500),
        kdb.Box(2000, 2000, 3000, 3000),
    ]
    assert [s.text.string for s in shapes if s.is_text()] == ["PATCH"]
    assert report["shape_count"] == 2
    assert report["label_count"] == 1
    assert report["layers"] == [
        {"layer": 67, "datatype": 20, "name": None, "shapes": 2}
    ]
    assert report["bbox_um"] == {"x0": 0, "y0": 0, "x1": 5, "y1": 3}


def test_explicit_false_matches_flat_default(seed, tmp_path):
    out = tmp_path / "patch.gds"
    params = {"base_gds": str(seed), "shapes": [PATCH]}
    options = {"output": str(out)}
    default_report = draw({"params": params, "options": options})
    default_bytes = out.read_bytes()
    layout = kdb.Layout()
    layout.read(str(out))
    assert layout.cells() == 1
    assert default_report["shape_count"] == 3
    report = draw({"params": {**params, "keep_hierarchy": False}, "options": options})
    assert report == default_report
    assert out.read_bytes() == default_bytes


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"keep_hierarchy": True}, "requires params.base_gds"),
        ({"keep_hierarchy": False}, "requires params.base_gds"),
        ({"base_gds": "seed.gds", "keep_hierarchy": 1}, "must be a boolean"),
        ({"base_gds": "seed.gds", "keep_hierarchy": "false"}, "must be a boolean"),
        ({"base_gds": "seed.gds", "keep_hierarchy": None}, "must be a boolean"),
        (
            {
                "base_gds": "seed.gds",
                "keep_hierarchy": True,
                "erase_um": [{"rect_um": [0, 0, 1, 1]}],
            },
            "erase_um",
        ),
    ],
)
def test_invalid_keep_hierarchy_returns_error(
    params, message, seed, monkeypatch, capsys
):
    monkeypatch.chdir(seed.parent)
    original = seed.read_bytes()
    code = main(
        [
            "draw",
            "--params",
            json.dumps({**params, "shapes": [PATCH]}),
            "-o",
            str(seed),
            "--format",
            "json",
        ]
    )
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)["error"]
    assert error["command"] == "draw"
    assert "params.keep_hierarchy" in error["message"]
    assert message in error["message"]
    assert seed.read_bytes() == original
