"""Tests for `klt diff` and the `diff_report` library function.

Fixtures are generated programmatically with `klayout.db`.
"""

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.layout_diff import LayoutDiffError, diff_report


def _build(
    *,
    dbu: float = 0.001,
    box_right: int = 1000,
    path_width: int = 200,
    text_x: int = 10,
    inst_x: int = 2000,
    sub_name: str = "SUB",
    array: bool = False,
    reverse_order: bool = False,
) -> kdb.Layout:
    layout = kdb.Layout()
    layout.dbu = dbu
    top = layout.create_cell("TOP")
    sub = layout.create_cell(sub_name)
    li = layout.layer(1, 0)
    sub.shapes(li).insert(kdb.Box(0, 0, 100, 100))
    if array:
        top.insert(
            kdb.CellInstArray(
                sub.cell_index(),
                kdb.Trans(inst_x, 0),
                kdb.Vector(500, 0),
                kdb.Vector(0, 500),
                3,
                2,
            )
        )
    else:
        top.insert(kdb.CellInstArray(sub.cell_index(), kdb.Trans(inst_x, 0)))
    items = [
        lambda: top.shapes(li).insert(kdb.Box(0, 0, box_right, 1000)),
        lambda: top.shapes(layout.layer(2, 0)).insert(
            kdb.Path([kdb.Point(0, 0), kdb.Point(0, 5000)], path_width)
        ),
        lambda: top.shapes(layout.layer(3, 0)).insert(kdb.Text("VDD", text_x, 20)),
        lambda: top.shapes(layout.layer(4, 0)).insert(
            kdb.Polygon([kdb.Point(0, 0), kdb.Point(0, 500), kdb.Point(500, 500)])
        ),
    ]
    for fn in reversed(items) if reverse_order else items:
        fn()
    return layout


def _write(layout: kdb.Layout, path: Path, *, timestamps: bool = False) -> str:
    opts = kdb.SaveLayoutOptions()
    opts.gds2_write_timestamps = timestamps
    layout.write(str(path), opts)
    return str(path)


@pytest.fixture
def base(tmp_path: Path) -> str:
    return _write(_build(), tmp_path / "base.gds")


def _diff(tmp_path: Path, other: kdb.Layout, base: str) -> dict:
    return diff_report(base, _write(other, tmp_path / "other.gds"))


def test_identical(tmp_path: Path, base: str) -> None:
    other = _write(_build(), tmp_path / "same.gds")
    report = diff_report(base, other)
    assert report["equivalent"] is True
    assert report["status"] == "equivalent"
    assert report["stream_identical"] is True
    assert report["a"]["canonical_hash"] == report["b"]["canonical_hash"]
    assert report["differences"] == []


def test_timestamp_and_order_only(tmp_path: Path, base: str) -> None:
    other = _write(_build(reverse_order=True), tmp_path / "ts.gds", timestamps=True)
    report = diff_report(base, other)
    assert report["equivalent"] is True
    assert report["a"]["canonical_hash"] == report["b"]["canonical_hash"]
    assert report["a"]["stream_sha256"] != report["b"]["stream_sha256"]
    assert report["stream_identical"] is False


def test_shape_changed(tmp_path: Path, base: str) -> None:
    report = _diff(tmp_path, _build(box_right=1100), base)
    assert report["equivalent"] is False
    assert report["a"]["canonical_hash"] != report["b"]["canonical_hash"]
    (cell,) = report["differences"]
    assert cell["cell"] == "TOP"
    (layer,) = cell["layers"]
    assert (layer["layer"], layer["datatype"]) == (1, 0)
    assert layer["only_in_a"][0]["bbox"] == [0, 0, 1000, 1000]
    assert layer["only_in_b"][0]["bbox"] == [0, 0, 1100, 1000]


def test_path_width_change(tmp_path: Path, base: str) -> None:
    report = _diff(tmp_path, _build(path_width=300), base)
    (layer,) = report["differences"][0]["layers"]
    assert layer["layer"] == 2
    assert layer["only_in_a"][0]["width"] == 200
    assert layer["only_in_b"][0]["width"] == 300


def test_text_move(tmp_path: Path, base: str) -> None:
    report = _diff(tmp_path, _build(text_x=99), base)
    (layer,) = report["differences"][0]["layers"]
    assert layer["layer"] == 3
    assert layer["only_in_a"][0]["string"] == "VDD"
    assert layer["only_in_a"][0]["x"] == 10
    assert layer["only_in_b"][0]["x"] == 99


def test_instance_transform_change(tmp_path: Path, base: str) -> None:
    report = _diff(tmp_path, _build(inst_x=2500), base)
    inst = report["differences"][0]["instances"]
    assert inst["only_in_a"][0]["x"] == 2000
    assert inst["only_in_b"][0]["x"] == 2500
    assert report["differences"][0]["layers"] == []


def test_array_instance_vs_single(tmp_path: Path, base: str) -> None:
    report = _diff(tmp_path, _build(array=True), base)
    inst = report["differences"][0]["instances"]
    assert "array" not in inst["only_in_a"][0]
    array = inst["only_in_b"][0]["array"]
    # The stream reader may swap the a/b axes; the array size is what matters.
    assert sorted((array["na"], array["nb"])) == [2, 3]


def test_dbu_change(tmp_path: Path, base: str) -> None:
    report = _diff(tmp_path, _build(dbu=0.005), base)
    assert report["equivalent"] is False
    assert report["dbu_equal"] is False
    assert report["a"]["dbu_um"] == 0.001
    assert report["b"]["dbu_um"] == 0.005


def test_cell_rename(tmp_path: Path, base: str) -> None:
    report = _diff(tmp_path, _build(sub_name="SUB2"), base)
    assert report["equivalent"] is False
    assert report["cells_only_in_a"] == ["SUB"]
    assert report["cells_only_in_b"] == ["SUB2"]


def test_top_scopes_hierarchy(tmp_path: Path, base: str) -> None:
    changed = _build()
    changed.cell("SUB").shapes(changed.layer(1, 0)).insert(kdb.Box(5, 5, 6, 6))
    other = _write(changed, tmp_path / "other.gds")
    assert diff_report(base, other)["equivalent"] is False
    assert diff_report(base, other, top="TOP")["equivalent"] is False
    # SUB differs, TOP-only scope still includes SUB (called cell); a leaf
    # with no difference outside the hierarchy would compare equal.
    extra = _build()
    extra.create_cell("ORPHAN").shapes(extra.layer(9, 0)).insert(kdb.Box(0, 0, 1, 1))
    extra_path = _write(extra, tmp_path / "extra.gds")
    assert diff_report(base, extra_path)["equivalent"] is False
    assert diff_report(base, extra_path, top="TOP")["equivalent"] is True


def test_empty_layouts(tmp_path: Path) -> None:
    a = _write(kdb.Layout(), tmp_path / "a.gds")
    b = _write(kdb.Layout(), tmp_path / "b.gds")
    report = diff_report(a, b)
    assert report["equivalent"] is True
    assert report["a"]["cell_count"] == 0


def test_oasis_input(tmp_path: Path, base: str) -> None:
    oas = tmp_path / "base.oas"
    _build().write(str(oas))
    assert diff_report(base, str(oas))["equivalent"] is True


def test_unreadable_file(tmp_path: Path, base: str) -> None:
    with pytest.raises(LayoutDiffError, match="file not found"):
        diff_report(base, str(tmp_path / "missing.gds"))


def test_unknown_top(base: str) -> None:
    with pytest.raises(LayoutDiffError, match="top cell not found"):
        diff_report(base, base, top="NOPE")


def test_truncation_cap(tmp_path: Path) -> None:
    many = kdb.Layout()
    cell = many.create_cell("TOP")
    li = many.layer(1, 0)
    for i in range(20):
        cell.shapes(li).insert(kdb.Box(i * 10, 0, i * 10 + 5, 5))
    a = _write(many, tmp_path / "a.gds")
    empty = kdb.Layout()
    empty.create_cell("TOP")
    b = _write(empty, tmp_path / "b.gds")
    report = diff_report(a, b, max_items=5)
    (layer,) = report["differences"][0]["layers"]
    assert layer["only_in_a_count"] == 20
    assert len(layer["only_in_a"]) == 5
    assert report["truncated"] is True


def test_max_items_validation(base: str) -> None:
    with pytest.raises(LayoutDiffError, match="max-items"):
        diff_report(base, base, max_items=0)


def test_cli_exit_codes(
    tmp_path: Path, base: str, capsys: pytest.CaptureFixture[str]
) -> None:
    same = _write(_build(), tmp_path / "same.gds")
    assert main(["diff", base, same, "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "equivalent"

    changed = _write(_build(box_right=1100), tmp_path / "changed.gds")
    assert main(["diff", base, changed, "--format", "json"]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "different"

    assert main(["diff", base, str(tmp_path / "missing.gds"), "--format", "json"]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "diff"

    assert main(["diff", base, changed]) == 3
    assert "status: different" in capsys.readouterr().out
