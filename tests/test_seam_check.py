"""Tests for `klt seam-check` and the `run_seam_check` library function.

Fixtures are built programmatically with `klayout.db` (no external corpus).
Geometry: a 3-cell abutting row of 10 um tiles, each carrying a 2 um rail on
layer 68/20 spanning the tile; a negative control cuts the rail at one seam.
"""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.seam_check import SeamCheckError, run_seam_check

_RAIL = (68, 20)
_DBU = 0.001
_TILE = 10000  # 10 um
_BAND = "0,0,30,2"


def _build(
    path,
    *,
    offsets=(0, _TILE, 2 * _TILE),
    rail_cut: tuple[int, int] | None = None,
    rail: bool = True,
    transforms=None,
    array: bool = False,
) -> None:
    layout = kdb.Layout()
    layout.dbu = _DBU
    index = layout.layer(*_RAIL)
    tile = layout.create_cell("TILE")
    # Outline (layer 1/0) gives the tile its 10x10 um bbox; the rail covers
    # the full tile width at y 0..2 um.
    tile.shapes(layout.layer(1, 0)).insert(kdb.Box(0, 0, _TILE, _TILE))
    if rail:
        tile.shapes(index).insert(kdb.Box(0, 0, _TILE, 2000))
    top = layout.create_cell("TOP")
    if array:
        top.insert(
            kdb.CellInstArray(
                tile.cell_index(),
                kdb.Trans(),
                kdb.Vector(_TILE, 0),
                kdb.Vector(0, 0),
                3,
                1,
            )
        )
    else:
        for i, x in enumerate(offsets):
            trans = (transforms or {}).get(i, kdb.Trans(x, 0))
            top.insert(kdb.CellInstArray(tile.cell_index(), trans))
    if rail_cut is not None:
        # Flat rail with a gap, replacing the instance-carried rail.
        top.clear_insts()
        top.shapes(index).insert(kdb.Box(0, 0, rail_cut[0], 2000))
        top.shapes(index).insert(kdb.Box(rail_cut[1], 0, 3 * _TILE, 2000))
    layout.write(str(path))


def test_passing_row(tmp_path):
    path = tmp_path / "row.gds"
    _build(path)
    report = run_seam_check(
        str(path), tiling=["TILE"], layer=_RAIL, bands_um=[(0, 0, 30, 2)]
    )
    assert report["status"] == "pass"
    assert report["violations"] == []
    assert report["schema_version"] == 1


def test_array_instance_tiles(tmp_path):
    path = tmp_path / "arr.gds"
    _build(path, array=True)
    assert run_seam_check(str(path), tiling=["TILE"])["status"] == "pass"


def test_one_dbu_gap_between_instances(tmp_path):
    path = tmp_path / "gap.gds"
    _build(path, offsets=(0, _TILE, 2 * _TILE + 1))
    report = run_seam_check(str(path), tiling=["TILE"])
    assert report["status"] == "fail"
    kinds = {v["kind"] for v in report["violations"]}
    assert kinds == {"gap"}
    gap = report["violations"][0]["bbox"]
    assert gap["left"] == 2 * _TILE and gap["right"] == 2 * _TILE + 1


def test_overlapping_instances(tmp_path):
    path = tmp_path / "ovl.gds"
    _build(path, offsets=(0, _TILE - 5, 2 * _TILE - 10))
    report = run_seam_check(str(path), tiling=["TILE"])
    assert report["status"] == "fail"
    assert {v["kind"] for v in report["violations"]} == {"overlap"}
    assert len(report["violations"]) == 2


def test_rotated_and_mirrored_instances_tile(tmp_path):
    path = tmp_path / "rot.gds"
    _build(
        path,
        transforms={
            1: kdb.Trans(2, False, 2 * _TILE, _TILE),  # R180 about corner
            2: kdb.Trans(0, True, 2 * _TILE, _TILE),  # mirrored
        },
    )
    assert run_seam_check(str(path), tiling=["TILE"])["status"] == "pass"


def test_rail_cut_at_seam_fails(tmp_path):
    path = tmp_path / "cut.gds"
    _build(path, rail_cut=(_TILE - 100, _TILE + 100))
    report = run_seam_check(str(path), layer=_RAIL, bands_um=[(0, 0, 30, 2)], top="TOP")
    assert report["status"] == "fail"
    kinds = {v["kind"] for v in report["violations"]}
    assert kinds == {"gap", "fragmented"}
    gap = next(v for v in report["violations"] if v["kind"] == "gap")
    assert gap["bbox"]["left"] == _TILE - 100 and gap["bbox"]["right"] == _TILE + 100


def test_empty_layer_is_violation(tmp_path):
    path = tmp_path / "row.gds"
    _build(path)
    report = run_seam_check(str(path), layer=(99, 0), bands_um=[(0, 0, 30, 2)])
    assert report["status"] == "fail"
    assert [v["kind"] for v in report["violations"]] == ["empty"]


def test_band_outside_geometry_is_empty(tmp_path):
    path = tmp_path / "row.gds"
    _build(path)
    report = run_seam_check(str(path), layer=_RAIL, bands_um=[(0, 50, 30, 52)])
    assert [v["kind"] for v in report["violations"]] == ["empty"]


def test_tiling_cell_never_placed_is_empty(tmp_path):
    path = tmp_path / "row.gds"
    _build(path)
    layout = kdb.Layout()
    layout.read(str(path))
    layout.create_cell("UNUSED")
    layout.write(str(path))
    report = run_seam_check(str(path), tiling=["UNUSED"], top="TOP")
    assert [v["kind"] for v in report["violations"]] == ["empty"]


def test_multiple_bands_and_multiple_tops(tmp_path):
    path = tmp_path / "row.gds"
    _build(path)
    layout = kdb.Layout()
    layout.read(str(path))
    other = layout.create_cell("OTHER")
    other.insert(kdb.CellInstArray(layout.cell("TILE").cell_index(), kdb.Trans()))
    layout.write(str(path))
    report = run_seam_check(
        str(path), layer=_RAIL, bands_um=[(0, 0, 10, 2), (0, 0, 30, 2)]
    )
    # OTHER only has one tile: its 30 um band is a gap.
    assert report["status"] == "fail"
    assert {v["cell"] for v in report["violations"]} == {"OTHER"}
    assert (
        run_seam_check(str(path), layer=_RAIL, bands_um=[(0, 0, 30, 2)], top="TOP")[
            "status"
        ]
        == "pass"
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"layer": _RAIL},
        {"bands_um": [(0, 0, 1, 1)]},
        {"tiling": []},
        {"layer": _RAIL, "bands_um": [(0, 0, 0, 1)]},
        {"tiling": ["NOPE"]},
        {"tiling": ["TILE"], "top": "NOPE"},
    ],
)
def test_usage_errors(tmp_path, kwargs):
    path = tmp_path / "row.gds"
    _build(path)
    with pytest.raises(SeamCheckError):
        run_seam_check(str(path), **kwargs)


def test_missing_file():
    with pytest.raises(SeamCheckError):
        run_seam_check("/nonexistent/x.gds", tiling=["TILE"])


# --- CLI ---------------------------------------------------------------------


def test_cli_pass_json(tmp_path, capsys):
    path = tmp_path / "row.gds"
    _build(path)
    code = main(
        [
            "seam-check",
            str(path),
            "--tiling",
            "TILE",
            "--layer",
            "68/20",
            "--band",
            _BAND,
            "--format",
            "json",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["status"] == "pass"


def test_cli_fail_exit_3(tmp_path, capsys):
    path = tmp_path / "gap.gds"
    _build(path, offsets=(0, _TILE, 2 * _TILE + 1))
    code = main(["seam-check", str(path), "--tiling", "TILE", "--format", "json"])
    assert code == 3
    assert json.loads(capsys.readouterr().out)["status"] == "fail"


def test_cli_text_output(tmp_path, capsys):
    path = tmp_path / "gap.gds"
    _build(path, offsets=(0, _TILE, 2 * _TILE + 1))
    assert main(["seam-check", str(path), "--tiling", "TILE"]) == 3
    assert "gap" in capsys.readouterr().out


@pytest.mark.parametrize(
    "extra",
    [[], ["--layer", "bad", "--band", _BAND], ["--layer", "68/20", "--band", "1,2,3"]],
)
def test_cli_errors_exit_1(tmp_path, extra):
    path = tmp_path / "row.gds"
    _build(path)
    assert main(["seam-check", str(path), "--format", "json", *extra]) == 1
