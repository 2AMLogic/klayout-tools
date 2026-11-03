"""Tests for `klt fill` and the `run_fill` library function."""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.drc import run_drc
from klayout_tools.fill import FillError, run_fill

_DBU = 0.001
_MET1 = (68, 20)
_MET1_FILL = (36, 28)
_M1_GF = (34, 0)
_M1_GF_FILL = (34, 4)
_NDMY = (111, 5)


def _um(v):
    return round(v / _DBU)


def _box(x0, y0, x1, y1):
    return kdb.Box(_um(x0), _um(y0), _um(x1), _um(y1))


def _write(path, shapes, top="TOP", sub=None):
    layout = kdb.Layout()
    layout.dbu = _DBU
    cell = layout.create_cell(top)
    for lay, box in shapes:
        cell.shapes(layout.layer(*lay)).insert(_box(*box))
    if sub:
        child = layout.create_cell("CHILD")
        for lay, box in sub:
            child.shapes(layout.layer(*lay)).insert(_box(*box))
        cell.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans()))
    layout.write(str(path))


def _read(path, lay):
    layout = kdb.Layout()
    layout.read(str(path))
    top = layout.top_cell()
    idx = layout.find_layer(*lay)
    if idx is None:
        return kdb.Region(), layout
    return kdb.Region(layout.begin_shapes(top, idx)), layout


def _fill(tmp_path, shapes, **kw):
    src = tmp_path / "in.gds"
    _write(src, shapes, sub=kw.pop("sub", None))
    out = tmp_path / "out.gds"
    kw.setdefault("extent_um", (0, 0, 40, 40))
    kw.setdefault("spacing_um", 1.0)
    kw.setdefault("window_um", 20.0)
    pdk = kw.pop("pdk", "sky130")
    layers = kw.pop("layers", [_MET1])
    return run_fill(str(src), pdk, layers, str(out), **kw), src, out


def test_sky130_fill_only_named_layer_inside_boundary_and_spacing(tmp_path):
    other = (69, 20)
    report, src, out = _fill(
        tmp_path, [(_MET1, (10, 10, 12, 12)), (other, (0, 0, 5, 5))]
    )
    fill, layout = _read(out, _MET1_FILL)
    assert not fill.is_empty()
    assert report["layers"][0]["fill_layer"] == "36/28"
    assert report["layers"][0]["fill_shape_count"] == fill.count()
    assert (fill - kdb.Region(_box(0, 0, 40, 40))).is_empty()
    halo = kdb.Region(_box(10, 10, 12, 12)).sized(_um(1.0))
    assert (fill & halo).is_empty()
    assert layout.find_layer(41, 28) is None  # other layer's fill untouched


def test_exclusion_region_respected(tmp_path):
    _, _, out = _fill(tmp_path, [(_MET1, (0, 0, 1, 1))], exclude_um=[(10, 10, 30, 30)])
    fill, _ = _read(out, _MET1_FILL)
    assert not fill.is_empty()
    assert (fill & kdb.Region(_box(10, 10, 30, 30))).is_empty()


def test_gf180mcu_fill_and_ndmy_marker(tmp_path):
    report, _, out = _fill(
        tmp_path,
        [(_M1_GF, (0, 0, 1, 1)), (_NDMY, (20, 0, 40, 40))],
        pdk="gf180mcu",
        layers=[_M1_GF],
    )
    fill, _ = _read(out, _M1_GF_FILL)
    assert not fill.is_empty()
    assert report["layers"][0]["fill_layer"] == "34/4"
    assert (fill & kdb.Region(_box(20, 0, 40, 40))).is_empty()


def test_density_increases_and_target_reached(tmp_path):
    report, _, _ = _fill(tmp_path, [(_MET1, (0, 0, 1, 1))], target_density=0.2)
    entry = report["layers"][0]
    assert entry["density_after"]["mean"] > entry["density_before"]["mean"]
    assert entry["target_reached"] is True
    assert entry["density_after"]["min"] >= 0.2


def test_target_not_reached_when_geometry_blocks(tmp_path):
    report, _, _ = _fill(
        tmp_path, [(_MET1, (0, 0, 1, 1))], target_density=0.9, fill_gap_um=1.0
    )
    assert report["layers"][0]["target_reached"] is False


def test_unmapped_layer_errors(tmp_path):
    with pytest.raises(FillError, match="99/0"):
        _fill(tmp_path, [(_MET1, (0, 0, 1, 1))], layers=[(99, 0)])


def test_spacing_required_and_boundary_required(tmp_path):
    with pytest.raises(FillError, match="--spacing"):
        _fill(tmp_path, [], spacing_um=None)
    with pytest.raises(FillError, match="no boundary"):
        _fill(tmp_path, [], extent_um=None)


def test_prboundary_boundary_and_missing(tmp_path):
    with pytest.raises(FillError, match="prBoundary"):
        _fill(tmp_path, [], extent_um=None, boundary="prBoundary")
    _, _, out = _fill(
        tmp_path, [((235, 4), (0, 0, 10, 10))], extent_um=None, boundary="prBoundary"
    )
    fill, _ = _read(out, _MET1_FILL)
    assert not fill.is_empty()
    assert (fill - kdb.Region(_box(0, 0, 10, 10))).is_empty()


def test_edge_cases(tmp_path):
    # boundary smaller than one fill tile -> no fill
    report, _, _ = _fill(tmp_path, [], extent_um=(0, 0, 0.5, 0.5))
    assert report["layers"][0]["fill_shape_count"] == 0
    # exclusion covers everything
    report, _, _ = _fill(tmp_path, [], exclude_um=[(-1, -1, 41, 41)])
    assert report["layers"][0]["fill_shape_count"] == 0


def test_hierarchical_input_fills_top_cell_and_avoids_child_shapes(tmp_path):
    _, _, out = _fill(tmp_path, [], sub=[(_MET1, (10, 10, 12, 12))])
    fill, layout = _read(out, _MET1_FILL)
    assert not fill.is_empty()
    assert layout.cell("CHILD").shapes(layout.layer(*_MET1_FILL)).is_empty()
    assert (fill & kdb.Region(_box(10, 10, 12, 12)).sized(_um(1))).is_empty()


def test_input_unchanged_and_no_new_drc_violations(tmp_path):
    shapes = [(_MET1, (10, 10, 12, 12))]
    _, src, out = _fill(tmp_path, shapes)
    fill_in, _ = _read(src, _MET1_FILL)
    assert fill_in.is_empty()  # input never modified
    base = run_drc(str(src), "sky130")
    after = run_drc(str(out), "sky130")
    assert after["violation_count"] <= base["violation_count"]


def test_cli_json_and_error(tmp_path, capsys):
    src = tmp_path / "in.gds"
    _write(src, [(_MET1, (0, 0, 1, 1))])
    out = tmp_path / "o.gds"
    rc = main(
        [
            "fill",
            str(src),
            "--pdk",
            "sky130",
            "--layers",
            "[[68,20]]",
            "--extent",
            "0,0,20,20",
            "--spacing",
            "1",
            "-o",
            str(out),
            "--format",
            "json",
        ]
    )
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["layers"][0]["fill_shape_count"] > 0
    rc = main(
        [
            "fill",
            str(src),
            "--pdk",
            "sky130",
            "--layers",
            "[[1,1]]",
            "--extent",
            "0,0,20,20",
            "--spacing",
            "1",
            "-o",
            str(out),
            "--format",
            "json",
        ]
    )
    assert rc == 1
