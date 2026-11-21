"""Tests for ``klt economy --attribute --flatten-wrappers`` (issue #5179).

Split out of ``test_economy.py`` for the file-size ratchet.
"""

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.economy import economy_report

# --- attribution --flatten-wrappers (issue #5179) -------------------------


def _make_wrapper_layout(array: bool = False, wrapper_trans=None):
    layout = kdb.Layout()
    li = layout.layer(1, 0)
    a = layout.create_cell("A")
    a.shapes(li).insert(kdb.Box(0, 0, 1000, 1000))
    b = layout.create_cell("B")
    b.shapes(li).insert(kdb.Box(0, 0, 1000, 1000))
    guard = layout.create_cell("GUARD")
    guard.shapes(li).insert(kdb.Box(0, 0, 500, 500))
    comp = layout.create_cell("COMP")
    comp.insert(kdb.CellInstArray(a.cell_index(), kdb.Trans(kdb.Vector(0, 0))))
    if array:
        comp.insert(
            kdb.CellInstArray(
                b.cell_index(),
                kdb.Trans(kdb.Vector(2000, 0)),
                kdb.Vector(1500, 0),
                kdb.Vector(0, 1500),
                2,
                1,
            )
        )
    else:
        comp.insert(kdb.CellInstArray(b.cell_index(), kdb.Trans(kdb.Vector(2000, 0))))
    # Wrapper's own shape between the inner blocks.
    comp.shapes(li).insert(kdb.Box(1100, 0, 1200, 100))
    top = layout.create_cell("TOP")
    top.insert(kdb.CellInstArray(guard.cell_index(), kdb.Trans(kdb.Vector(0, 0))))
    top.insert(
        kdb.CellInstArray(
            comp.cell_index(), wrapper_trans or kdb.Trans(kdb.Vector(0, 0))
        )
    )
    return layout


def test_flatten_wrappers_leaf_names_and_sums(tmp_path):
    path = tmp_path / "w.gds"
    _make_wrapper_layout().write(str(path))
    base = economy_report(str(path), attribute=True)["attribution"]
    assert [c["name"] for c in base["children"]] == ["COMP[0]", "GUARD[0]"]
    assert base["instance_overlap_um2"] > 0

    attr = economy_report(str(path), attribute=True, flatten_wrappers=True)[
        "attribution"
    ]
    names = {c["name"] for c in attr["children"]}
    assert names == {"COMP[0]/A[0]", "COMP[0]/B[0]", "GUARD[0]"}
    assert attr["sums_to_total"] is True
    assert attr["unattributed_um2"] == 0
    # GUARD overlaps A (both at origin), but no wrapper bbox is counted.
    assert attr["instance_overlap_um2"] == pytest.approx(0.5 * 0.5)
    # The wrapper's interior gap (A..B) is now an ordinary gap component.
    gaps = attr["gap_components"]
    assert sum(g["area_um2"] for g in gaps) == pytest.approx(1.0)
    assert all("COMP[0]" not in g["position"] or "/" in g["position"] for g in gaps)


def test_flatten_wrappers_array_and_transform(tmp_path):
    path = tmp_path / "w.gds"
    _make_wrapper_layout(
        array=True, wrapper_trans=kdb.Trans(1, True, kdb.Vector(10000, 0))
    ).write(str(path))
    attr = economy_report(str(path), attribute=True, flatten_wrappers=True)[
        "attribution"
    ]
    names = sorted(c["name"] for c in attr["children"])
    assert names == [
        "COMP[0]/A[0]",
        "COMP[0]/B[0]",
        "COMP[0]/B[1]",
        "GUARD[0]",
    ]
    assert attr["sums_to_total"] is True


def test_flatten_wrappers_requires_attribute(tmp_path, capsys):
    path = tmp_path / "w.gds"
    _make_wrapper_layout().write(str(path))
    rc = main(["economy", str(path), "--flatten-wrappers", "--format", "json"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "--flatten-wrappers requires --attribute" in captured.out + captured.err


def test_flatten_wrappers_cli_deterministic(tmp_path, capsys):
    path = tmp_path / "w.gds"
    _make_wrapper_layout().write(str(path))
    outs = []
    for _ in range(2):
        argv = ["economy", str(path), "--attribute", "--flatten-wrappers"]
        assert main(argv + ["--format", "json"]) == 0
        outs.append(capsys.readouterr().out)
    assert outs[0] == outs[1]
