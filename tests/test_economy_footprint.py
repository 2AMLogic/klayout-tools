"""Tests for ``klt economy --cell-footprint`` (issue #5180)."""

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.economy import EconomyError
from klayout_tools.economy_footprint import economy_report_with_footprint

# --- cell footprint (issue #5180) -----------------------------------------


def _make_footprint_layout(names: bool = True) -> kdb.Layout:
    """TOP places: PR (prBoundary 2x1 um, drawn 3x1.5 bbox), LEFONLY (bbox
    1x1, LEF 0.5x0.5), TAP (bbox-only 0.5x0.5, tapvpwrvgnd), and an
    arrayed PR (2 members)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    metal = layout.layer(1, 0)
    pr = layout.layer(235, 4)

    cpr = layout.create_cell("sky130_fd_sc_hd__dfxtp_1")
    cpr.shapes(metal).insert(kdb.Box(-500, -250, 2500, 1250))
    cpr.shapes(pr).insert(kdb.Box(0, 0, 2000, 1000))
    clef = layout.create_cell("sky130_fd_sc_hd__inv_1")
    clef.shapes(metal).insert(kdb.Box(0, 0, 1000, 1000))
    ctap = layout.create_cell("sky130_fd_sc_hd__tapvpwrvgnd_1")
    ctap.shapes(metal).insert(kdb.Box(0, 0, 500, 500))

    def place(cell, x, y, name=None):
        inst = top.insert(kdb.CellInstArray(cell.cell_index(), kdb.Trans(x, y)))
        if name is not None and names:
            inst.set_property(1, name)

    place(cpr, 0, 0, "u_a/r0")
    place(clef, 5000, 0, "u_a/g1")
    place(ctap, 0, 5000)  # no name -> (top)
    place(clef, 0, 8000, "u_b/g2")
    arr = top.insert(
        kdb.CellInstArray(
            cpr.cell_index(),
            kdb.Trans(0, 20000),
            kdb.Vector(4000, 0),
            kdb.Vector(0, 0),
            2,
            1,
        )
    )
    if names:
        arr.set_property(1, "u_b/arr")
    return layout


def test_footprint_absent_by_default(tmp_path):
    path = tmp_path / "fp.gds"
    _make_footprint_layout().write(str(path))
    assert "cell_footprint" not in economy_report_with_footprint(str(path))


def test_footprint_sources_and_sum_invariant(tmp_path):
    path = tmp_path / "fp.gds"
    _make_footprint_layout().write(str(path))
    lef = tmp_path / "c.lef"
    lef.write_text(
        "MACRO sky130_fd_sc_hd__inv_1\n  CLASS CORE ;\n  SIZE 0.5 BY 0.5 ;\n"
        "END sky130_fd_sc_hd__inv_1\n"
    )
    fp = economy_report_with_footprint(
        str(path), group_by_instance_prefix="/", lef_paths=(str(lef),)
    )["cell_footprint"]
    masters = {m["master"]: m for m in fp["masters"]}
    assert masters["sky130_fd_sc_hd__dfxtp_1"]["source"] == "prboundary"
    assert masters["sky130_fd_sc_hd__dfxtp_1"]["footprint_um2"] == pytest.approx(2.0)
    assert masters["sky130_fd_sc_hd__dfxtp_1"]["instance_count"] == 3
    assert masters["sky130_fd_sc_hd__inv_1"]["source"] == "lef"
    assert masters["sky130_fd_sc_hd__inv_1"]["footprint_um2"] == pytest.approx(0.25)
    assert masters["sky130_fd_sc_hd__tapvpwrvgnd_1"]["source"] == "bbox"
    assert masters["sky130_fd_sc_hd__tapvpwrvgnd_1"]["category"] == "tap"
    assert masters["sky130_fd_sc_hd__dfxtp_1"]["category"] == "sequential"
    assert masters["sky130_fd_sc_hd__inv_1"]["category"] == "logic"

    expected = 3 * 2.0 + 2 * 0.25 + 0.25
    assert fp["total_footprint_um2"] == pytest.approx(expected)
    assert fp["physical_only_um2"] == pytest.approx(0.25)
    grouping = fp["grouping"]
    assert grouping["status"] == "ok"
    groups = {g["group"]: g for g in grouping["groups"]}
    assert set(groups) == {"u_a", "u_b", "(top)"}
    assert groups["u_a"]["footprint_um2"] == pytest.approx(2.25)
    assert groups["u_b"]["instance_count"] == 3  # inv + 2 array members
    assert groups["(top)"]["category_um2"] == {"tap": pytest.approx(0.25)}
    assert sum(g["footprint_um2"] for g in groups.values()) == pytest.approx(
        fp["total_footprint_um2"]
    )


def test_footprint_without_names_is_unavailable(tmp_path):
    path = tmp_path / "fp.gds"
    _make_footprint_layout(names=False).write(str(path))
    fp = economy_report_with_footprint(str(path), group_by_instance_prefix="/")[
        "cell_footprint"
    ]
    assert fp["grouping"]["status"] == "unavailable"
    assert fp["total_footprint_um2"] > 0


def test_footprint_bbox_fallback_without_lef(tmp_path):
    path = tmp_path / "fp.gds"
    _make_footprint_layout().write(str(path))
    fp = economy_report_with_footprint(str(path), cell_footprint=True)["cell_footprint"]
    inv = next(m for m in fp["masters"] if m["master"].endswith("inv_1"))
    assert inv["source"] == "bbox"
    assert fp["grouping"] is None


def test_lef_requires_footprint(tmp_path):
    path = tmp_path / "fp.gds"
    _make_footprint_layout().write(str(path))
    with pytest.raises(EconomyError):
        economy_report_with_footprint(str(path), lef_paths=("x.lef",))


def test_cli_cell_footprint(tmp_path, capsys):
    path = tmp_path / "fp.gds"
    _make_footprint_layout().write(str(path))
    assert (
        main(
            [
                "economy",
                str(path),
                "--group-by-instance-prefix",
                "/",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["cell_footprint"]["grouping"]["status"] == "ok"
    assert main(["economy", str(path), "--cell-footprint"]) == 0
    assert "cell_footprint:" in capsys.readouterr().out
