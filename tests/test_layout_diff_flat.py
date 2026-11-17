"""Tests for ``klt diff --flattened`` (issue #5729): hierarchy-insensitive compare."""

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.layout_diff import LayoutDiffError, diff_report


def _write(layout: kdb.Layout, path: Path) -> str:
    layout.write(str(path))
    return str(path)


def _layout(
    inner: str = "inner",
    *,
    moved: int = 0,
    extra_layer: bool = False,
    label: str | None = None,
    label_only: bool = False,
    dbu: float = 0.001,
    nested: bool = False,
) -> kdb.Layout:
    """The issue's reproducer: one box in a child cell, one box in TOP."""
    ly = kdb.Layout()
    ly.dbu = dbu
    top = ly.create_cell("TOP")
    child = ly.create_cell(inner)
    li = ly.layer(1, 0)
    child.shapes(li).insert(kdb.Box(0, 0, 1000 + moved, 500))
    if nested:
        mid = ly.create_cell("mid")
        mid.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(0, 0)))
        top.insert(kdb.CellInstArray(mid.cell_index(), kdb.Trans(0, 0)))
    else:
        top.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(0, 0)))
    top.shapes(li).insert(kdb.Box(2000, 0, 3000, 500))
    if extra_layer:
        top.shapes(ly.layer(7, 0)).insert(kdb.Box(0, 0, 10, 10))
    if label is not None:
        top.shapes(ly.layer(1, 0)).insert(kdb.Text(label, 5, 5))
    if label_only:
        top.shapes(ly.layer(50, 0)).insert(kdb.Text("L", 0, 0))
    return ly


@pytest.fixture
def a(tmp_path: Path) -> str:
    return _write(_layout(), tmp_path / "a.gds")


def _flat(a: str, tmp_path: Path, other: kdb.Layout, **kw) -> dict:
    return diff_report(
        a, _write(other, tmp_path / "b.gds"), top="TOP", flattened=True, **kw
    )


def test_renamed_inner_cell_is_equivalent_only_when_flattened(
    tmp_path: Path, a: str
) -> None:
    b = _write(_layout("inner__core"), tmp_path / "b.gds")
    assert diff_report(a, b, top="TOP")["equivalent"] is False  # default unchanged
    report = diff_report(a, b, top="TOP", flattened=True)
    assert report["equivalent"] is True
    (layer,) = report["layers"]
    # guard against a vacuous pass: both sides must actually have been read
    assert layer["shapes_a"] == layer["shapes_b"] == 2
    assert report["a"]["bbox_um"]["width"] == 3.0
    # an empty XOR is stated as 0, never an absent key
    assert layer["xor_polygon_count"] == 0
    assert layer["xor_area_dbu2"] == 0
    assert layer["xor_area_um2"] == 0
    assert "mode" not in diff_report(a, b, top="TOP")


def test_one_dbu_move_reports_xor(tmp_path: Path, a: str) -> None:
    report = _flat(a, tmp_path, _layout(moved=1))
    assert report["equivalent"] is False
    (layer,) = report["layers"]
    assert layer["xor_polygon_count"] == 1
    assert layer["xor_area_dbu2"] == 500
    assert layer["xor_area_um2"] == pytest.approx(500 * 0.001**2)
    assert report["bbox"]["equal"] is True


def test_layer_added_in_b_only_is_visible_as_itself(tmp_path: Path, a: str) -> None:
    report = _flat(a, tmp_path, _layout(extra_layer=True))
    assert report["equivalent"] is False
    assert report["drawn_layers"]["only_in_b"] == [{"layer": 7, "datatype": 0}]
    assert report["drawn_layers"]["only_in_a"] == []
    shared = [e for e in report["layers"] if (e["layer"], e["datatype"]) == (1, 0)]
    assert shared[0]["xor_polygon_count"] == 0  # the XOR alone cannot see it
    # symmetric: the layer is also caught when only A drew it
    swapped = diff_report(
        _write(_layout(extra_layer=True), tmp_path / "c.gds"),
        a,
        top="TOP",
        flattened=True,
    )
    assert swapped["drawn_layers"]["only_in_a"] == [{"layer": 7, "datatype": 0}]


def test_label_change_detected_outside_the_polygon_xor(tmp_path: Path) -> None:
    a = _write(_layout(label="VDD"), tmp_path / "a.gds")
    report = _flat(a, tmp_path, _layout(label="VSS"))
    assert report["equivalent"] is False
    assert report["layers"][0]["xor_polygon_count"] == 0
    assert report["drawn_layers"]["equal"] is True
    (layer,) = report["labels"]["layers"]
    assert (layer["layer"], layer["texttype"]) == (1, 0)
    assert layer["only_in_a"][0]["text"] == "VDD"
    assert layer["only_in_b"][0]["text"] == "VSS"


def test_label_only_pair_not_in_drawn_set(tmp_path: Path, a: str) -> None:
    report = _flat(a, tmp_path, _layout(label_only=True))
    assert report["drawn_layers"] == {"equal": True, "only_in_a": [], "only_in_b": []}
    assert report["b"]["drawn_layers"] == [{"layer": 1, "datatype": 0}]
    assert report["equivalent"] is False  # ...but the label itself differs
    assert report["labels"]["layers"][0]["layer"] == 50


def test_repartitioned_hierarchy_is_equivalent(tmp_path: Path, a: str) -> None:
    assert _flat(a, tmp_path, _layout("deep", nested=True))["equivalent"] is True
    # a 2-copy array equals two explicit placements
    arr = kdb.Layout()
    top = arr.create_cell("TOP")
    sub = arr.create_cell("S")
    sub.shapes(arr.layer(1, 0)).insert(kdb.Box(0, 0, 100, 100))
    top.insert(
        kdb.CellInstArray(
            sub.cell_index(),
            kdb.Trans(0, 0),
            kdb.Vector(500, 0),
            kdb.Vector(0, 500),
            2,
            1,
        )
    )
    pair = kdb.Layout()
    top = pair.create_cell("TOP")
    for x in (0, 500):
        top.shapes(pair.layer(1, 0)).insert(kdb.Box(x, 0, x + 100, 100))
    pa = _write(arr, tmp_path / "arr.gds")
    assert _flat(pa, tmp_path, pair)["equivalent"] is True


def test_empty_streams_are_equivalent(tmp_path: Path) -> None:
    empty = kdb.Layout()
    empty.create_cell("TOP")
    a = _write(empty, tmp_path / "e.gds")
    report = _flat(a, tmp_path, empty)
    assert report["equivalent"] is True
    assert report["layers"] == []


def test_dbu_mismatch_is_different(tmp_path: Path, a: str) -> None:
    report = _flat(a, tmp_path, _layout(dbu=0.002))
    assert report["equivalent"] is False
    assert report["dbu_equal"] is False
    assert report["layers"][0]["xor_polygon_count"] is None


def test_max_items_truncates_but_counts_stay_exact(tmp_path: Path) -> None:
    def many(offset: int) -> kdb.Layout:
        ly = kdb.Layout()
        top = ly.create_cell("TOP")
        for layer in range(1, 6):
            top.shapes(ly.layer(layer, 0)).insert(kdb.Box(0, 0, 10 + offset, 10))
        return ly

    a = _write(many(0), tmp_path / "a.gds")
    report = _flat(a, tmp_path, many(5), max_items=2)
    assert report["different_layer_count"] == 5
    assert report["layer_count"] == 5
    assert len(report["layers"]) == 2
    assert report["truncated"] is True


def test_unknown_top_is_an_error(a: str) -> None:
    with pytest.raises(LayoutDiffError, match="top cell not found"):
        diff_report(a, a, top="NOPE", flattened=True)


def test_cli_exit_codes_and_envelope(
    tmp_path: Path, a: str, capsys: pytest.CaptureFixture[str]
) -> None:
    renamed = _write(_layout("inner__core"), tmp_path / "r.gds")
    assert main(["diff", a, renamed, "--top", "TOP"]) == 3
    capsys.readouterr()
    assert (
        main(["diff", a, renamed, "--top", "TOP", "--flattened", "--format", "json"])
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["schema_version"] == 1 and out["mode"] == "flattened"

    moved = _write(_layout(moved=1), tmp_path / "m.gds")
    assert main(["diff", a, moved, "--top", "TOP", "--flattened"]) == 3
    assert "status: different (flattened)" in capsys.readouterr().out

    assert main(["diff", a, a, "--top", "NOPE", "--flattened", "--format", "json"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["command"] == "diff"
