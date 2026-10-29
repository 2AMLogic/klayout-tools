"""``request.output_dbu_um`` (issue #4482): the merged GDS can be written at
a finer dbu than the tech LEF's ``DATABASE MICRONS``."""

from __future__ import annotations

from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools import place_and_route
from klayout_tools.place_and_route_gds_merge import _output_dbu_factor
from klayout_tools.place_and_route_request import _validate_output_dbu_um
from test_place_and_route_min_area import _build_fixture

PlaceAndRouteError = place_and_route.PlaceAndRouteError


def _fixture(tmp_path: Path, name: str) -> dict[str, object]:
    root = tmp_path / name
    root.mkdir()
    return _build_fixture(root)


def _read(path: str | Path) -> kdb.Layout:
    layout = kdb.Layout()
    layout.read(str(path))
    return layout


@pytest.mark.parametrize("value", [True, "0.1", 0, -0.001, float("inf")])
def test_validate_rejects_non_positive_numbers(value):
    with pytest.raises(PlaceAndRouteError, match="output_dbu_um"):
        _validate_output_dbu_um(value)


def test_validate_default_is_none():
    assert _validate_output_dbu_um(None) is None
    assert _validate_output_dbu_um(0.00025) == 0.00025


def test_factor_rules():
    assert _output_dbu_factor(0.001, None) == 1
    assert _output_dbu_factor(0.001, 0.001) == 1
    assert _output_dbu_factor(0.001, 0.00025) == 4
    for bad in (0.002, 0.0003):
        with pytest.raises(PlaceAndRouteError, match="whole number"):
            _output_dbu_factor(0.001, bad)


def test_merge_refines_dbu_losslessly(tmp_path):
    base_kwargs = _fixture(tmp_path, "base")
    base_info = place_and_route._merge_def_to_gds(**base_kwargs)
    assert base_info["output_dbu_um"] == pytest.approx(0.001)
    base = _read(base_kwargs["out_path"])

    fine_kwargs = _fixture(tmp_path, "fine")
    fine_info = place_and_route._merge_def_to_gds(**fine_kwargs, output_dbu_um=0.00025)
    assert fine_info["output_dbu_um"] == pytest.approx(0.00025)
    fine = _read(fine_kwargs["out_path"])

    assert fine.dbu == pytest.approx(0.00025)
    base_top, fine_top = base.top_cell(), fine.top_cell()
    assert fine_top.dbbox() == base_top.dbbox()
    for index in base.layer_indexes():
        info = base.get_info(index)
        fine_index = fine.find_layer(info.layer, info.datatype)
        base_region = kdb.Region(base_top.begin_shapes_rec(index)).merged()
        fine_region = kdb.Region(fine_top.begin_shapes_rec(fine_index)).merged()
        assert fine_region.area() * fine.dbu**2 == pytest.approx(
            base_region.area() * base.dbu**2
        )


def test_merge_same_dbu_is_noop(tmp_path):
    kwargs = _fixture(tmp_path, "a")
    place_and_route._merge_def_to_gds(**kwargs)
    kwargs2 = _fixture(tmp_path, "b")
    place_and_route._merge_def_to_gds(**kwargs2, output_dbu_um=0.001)
    assert (
        Path(kwargs["out_path"]).read_bytes() == Path(kwargs2["out_path"]).read_bytes()
    )


def test_merge_bad_ratio_writes_nothing(tmp_path):
    kwargs = _build_fixture(tmp_path)
    with pytest.raises(PlaceAndRouteError, match="whole number"):
        place_and_route._merge_def_to_gds(**kwargs, output_dbu_um=0.0003)
    assert not Path(kwargs["out_path"]).exists()
