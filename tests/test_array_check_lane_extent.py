"""Schema-v2 single-row compatibility and transformed lane extents (#5698)."""

import klayout.db as kdb
import pytest

from klayout_tools.array_check import run_array_check
from test_array_check import _DBU, _L, _write_row


@pytest.mark.parametrize(
    "offset,kind,extent", [(-1.0, "overlap", 7.0), (1.0, "gap", 9.0)]
)
def test_single_lane_violation_records_match_schema_v2(tmp_path, offset, kind, extent):
    path = tmp_path / "row.gds"
    _write_row(path, shift_last=offset)
    report = run_array_check(str(path), cell="UNIT", pitch_um=2.0, count=5)
    assert report["schema_version"] == 2 and report["lanes"] == 1
    # Measurements and descriptions captured from the merge base on the same GDS.
    assert {
        v["kind"]: (v["measured_um"], v["expected_um"], v["description"], v["lane"])
        for v in report["violations"]
    } == {
        "count": (4.0, 5.0, "4 placement(s) of UNIT found, expected 5", 0),
        kind: (1.0, 0.0, f"1.0 um {kind} between consecutive placements", 0),
        "length": (
            extent,
            10.0,
            f"array length {extent} um != count x pitch 10.0 um",
            0,
        ),
    }


@pytest.mark.parametrize("direction,rotation", [("x", 0), ("y", 90)])
def test_nested_placement_does_not_shorten_lane(tmp_path, direction, rotation):
    layout = kdb.Layout()
    layout.dbu = _DBU
    unit = layout.create_cell("UNIT")
    unit.shapes(layout.layer(*_L)).insert(kdb.Box(0, 0, 2000, 1000))
    top = layout.create_cell("TOP")
    axis = kdb.ICplxTrans(1, rotation, False, 0, 0)
    top.insert(kdb.CellInstArray(unit.cell_index(), axis))
    inner = kdb.ICplxTrans(0.5, 90, False, 1500, 0)
    top.insert(kdb.CellInstArray(unit.cell_index(), axis * inner))
    path = tmp_path / "nested.gds"
    layout.write(str(path))
    report = run_array_check(
        str(path), cell="UNIT", pitch_um=1.0, count=3, direction=direction
    )
    (length,) = [v for v in report["violations"] if v["kind"] == "length"]
    assert report["lanes"] == 1
    assert (
        length["measured_um"]
        == length["bbox"]["width" if direction == "x" else "height"]
        == 2.0
    )
