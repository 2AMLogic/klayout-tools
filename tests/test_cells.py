"""Tests for `klt cells` and the `cells_report` library function.

Fixtures are generated programmatically with `klayout.db` inside the tests —
no dependency on an external corpus.
"""

import json

import klayout.db as kdb
import pytest

from klayout_tools.cells import CellsError, cells_report
from klayout_tools.cli import main


def _make_layout() -> kdb.Layout:
    """A layout exercising the full schema.

    - TOP: a top cell with its own shapes, one plain instance of SUB, and
      one *arrayed* instance of ARR (3x4 = 12 copies) — exercises the
      placement-record-vs-expanded-count distinction for ``instances``.
    - SUB: a leaf cell (no children), instantiated once by TOP.
    - ARR: a leaf cell (no children), instantiated by TOP via a 3x4 array.
    - LONELY_TOP: a second, disjoint top cell with no shapes and no
      children — exercises ``top_cell_count > 1`` and ``bbox_um: null``.
    """
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    sub = layout.create_cell("SUB")
    arr = layout.create_cell("ARR")
    lonely_top = layout.create_cell("LONELY_TOP")

    m1 = layout.layer(1, 0)

    top.shapes(m1).insert(kdb.Box(0, 0, 10, 10))
    top.insert(kdb.CellInstArray(sub.cell_index(), kdb.Trans()))
    top.insert(
        kdb.CellInstArray(
            arr.cell_index(),
            kdb.Trans(),
            kdb.Vector(100, 0),
            kdb.Vector(0, 100),
            3,
            4,
        )
    )

    sub.shapes(m1).insert(kdb.Box(20, 20, 30, 30))
    arr.shapes(m1).insert(kdb.Box(1, 1, 2, 2))

    # lonely_top: deliberately no shapes, no children.
    del lonely_top

    return layout


def test_cells_report_oasis(tmp_path):
    path = tmp_path / "design.oas"
    _make_layout().write(str(path))

    report = cells_report(str(path))

    assert report["file"] == str(path)
    assert report["dbu_um"] == 0.001
    assert report["cell_count"] == 4
    assert report["top_cell_count"] == 2

    cells = report["cells"]
    assert len(cells) == 4
    # Sorted by index ascending.
    assert [entry["index"] for entry in cells] == sorted(
        entry["index"] for entry in cells
    )

    by_name = {entry["name"]: entry for entry in cells}

    top = by_name["TOP"]
    assert top["is_top"] is True
    assert top["shapes"] == 1
    # Two placement records (SUB once, ARR as one 3x4 array) — not 1 + 12.
    assert top["instances"] == 2
    assert top["children"] == ["ARR", "SUB"]
    assert top["parents"] == []
    assert top["bbox_um"] is not None

    sub = by_name["SUB"]
    assert sub["is_top"] is False
    assert sub["shapes"] == 1
    assert sub["instances"] == 0
    assert sub["children"] == []
    assert sub["parents"] == ["TOP"]
    assert sub["bbox_um"] == {"left": 0.02, "bottom": 0.02, "right": 0.03, "top": 0.03}

    arr = by_name["ARR"]
    assert arr["is_top"] is False
    assert arr["shapes"] == 1
    assert arr["instances"] == 0
    assert arr["children"] == []
    assert arr["parents"] == ["TOP"]
    assert arr["bbox_um"] == {
        "left": 0.001,
        "bottom": 0.001,
        "right": 0.002,
        "top": 0.002,
    }

    lonely_top = by_name["LONELY_TOP"]
    assert lonely_top["is_top"] is True
    assert lonely_top["shapes"] == 0
    assert lonely_top["instances"] == 0
    assert lonely_top["children"] == []
    assert lonely_top["parents"] == []
    assert lonely_top["bbox_um"] is None


def test_cells_report_gds(tmp_path):
    path = tmp_path / "design.gds"
    _make_layout().write(str(path))

    report = cells_report(str(path))

    by_name = {entry["name"]: entry for entry in report["cells"]}
    assert by_name["TOP"]["instances"] == 2
    assert by_name["SUB"]["shapes"] == 1
    assert by_name["ARR"]["shapes"] == 1
    assert by_name["LONELY_TOP"]["bbox_um"] is None


def test_top_filter(tmp_path):
    path = tmp_path / "design.gds"
    _make_layout().write(str(path))

    full = cells_report(str(path))
    filtered = cells_report(str(path), top=True)

    # Whole-layout totals unchanged regardless of --top.
    assert filtered["cell_count"] == full["cell_count"] == 4
    assert filtered["top_cell_count"] == full["top_cell_count"] == 2

    # `cells` is narrowed to a strict subset containing only top cells.
    filtered_names = {entry["name"] for entry in filtered["cells"]}
    assert filtered_names == {"TOP", "LONELY_TOP"}
    assert all(entry["is_top"] for entry in filtered["cells"])

    full_names = {entry["name"] for entry in full["cells"]}
    assert filtered_names <= full_names


def test_json_contract(tmp_path, capsys):
    """`--format json` emits exactly the documented schema."""
    path = tmp_path / "design.oas"
    _make_layout().write(str(path))

    assert main(["cells", str(path), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert set(data.keys()) == {
        "schema_version",
        "file",
        "dbu_um",
        "cell_count",
        "top_cell_count",
        "cells",
    }
    assert data["schema_version"] == 1
    assert isinstance(data["file"], str)
    assert isinstance(data["dbu_um"], float)
    assert isinstance(data["cell_count"], int)
    assert isinstance(data["top_cell_count"], int)
    assert isinstance(data["cells"], list)

    for entry in data["cells"]:
        assert set(entry.keys()) == {
            "name",
            "index",
            "is_top",
            "shapes",
            "instances",
            "children",
            "parents",
            "bbox_um",
        }
        assert isinstance(entry["name"], str)
        assert isinstance(entry["index"], int)
        assert isinstance(entry["is_top"], bool)
        assert isinstance(entry["shapes"], int)
        assert isinstance(entry["instances"], int)
        assert isinstance(entry["children"], list)
        assert isinstance(entry["parents"], list)
        assert entry["bbox_um"] is None or isinstance(entry["bbox_um"], dict)
        if entry["bbox_um"] is not None:
            assert set(entry["bbox_um"].keys()) == {"left", "bottom", "right", "top"}

    # Sorted ascending by index.
    indices = [entry["index"] for entry in data["cells"]]
    assert indices == sorted(indices)


def test_json_contract_with_top_flag(tmp_path, capsys):
    path = tmp_path / "design.oas"
    _make_layout().write(str(path))

    assert main(["cells", str(path), "--top", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert data["schema_version"] == 1
    assert data["cell_count"] == 4
    assert data["top_cell_count"] == 2
    assert {entry["name"] for entry in data["cells"]} == {"TOP", "LONELY_TOP"}
    assert all(entry["is_top"] for entry in data["cells"])


def test_default_format_is_text(tmp_path, capsys):
    path = tmp_path / "design.gds"
    _make_layout().write(str(path))

    assert main(["cells", str(path)]) == 0
    out = capsys.readouterr().out
    assert "file:" in out
    assert "TOP" in out and "SUB" in out
    # Not JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cells_report_raises_on_missing():
    with pytest.raises(CellsError):
        cells_report("/no/such/path/design.gds")
