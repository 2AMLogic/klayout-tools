"""Strict GF180 tap enclosure at touching wells and shielded implants."""

import klayout.db as kdb
import pytest

from klayout_tools.drc import run_drc
from test_drc_edge_band import (
    COMP,
    DNWELL,
    NPLUS,
    NWELL,
    RULE,
    _markers,
    _run,
    _tap_shapes,
)


@pytest.mark.parametrize(
    ("dnwell_left", "dnwell_bottom", "expected"),
    [(1999, -1000, 1), (2000, -1000, 1), (2000, 1000, 1), (2001, -1000, 0)],
)
def test_dnwell_contact_keeps_the_opposite_tap_edge_strict(
    tmp_path, dnwell_left, dnwell_bottom, expected
):
    shapes = [
        {"layer": list(COMP), "box": [0, 0, 2000, 1000]},
        {"layer": list(NPLUS), "box": [-80, -200, 2200, 1200]},
        {"layer": list(NWELL), "box": [-1000, -1000, 1800, 2000]},
        {"layer": list(DNWELL), "box": [dnwell_left, dnwell_bottom, 3000, 2000]},
    ]
    markers = _markers(_run(tmp_path, shapes))
    assert len(markers) == expected
    if expected:
        assert sorted(map(tuple, markers[0]["polygon"])) == [
            (-80, -139),
            (-80, 1139),
            (0, 0),
            (0, 1000),
        ]


@pytest.mark.parametrize("mixed", [False, True])
def test_strict_edges_are_shielded_by_the_enclosing_implant(tmp_path, mixed):
    shapes = _tap_shapes(80, None)
    if mixed:
        # The right edge and parts of the top and bottom edges are outside the well.
        shapes.append({"layer": list(NWELL), "box": [-1000, -1000, 500, 2000]})

    def polygons(name):
        return sorted(
            sorted(map(tuple, marker["polygon"]))
            for marker in _markers(_run(tmp_path, shapes, name=name))
        )

    baseline = polygons("one-implant.gds")
    if mixed:
        # The strict part of the top edge starts at the well boundary.
        assert [(361, 1080), (500, 1000), (1000, 1000), (1080, 1080)] in baseline
    else:
        assert len(baseline) == 4
    assert any((1080, 1080) in polygon and (1000, 0) in polygon for polygon in baseline)
    shapes.append({"layer": list(NPLUS), "box": [1085, 0, 1095, 1000]})
    assert polygons("with-neighbor.gds") == baseline


def _sloped_tap():
    points = [(0, 0), (0, 1000), (700, 1000), (1000, 900), (1000, 0)]
    return kdb.Polygon([kdb.Point(x, y) for x, y in points])


def _notched_implant():
    points = [
        (-500, -500),
        (-500, 1500),
        (810, 1500),
        (810, 1120),
        (1500, 1120),
        (1500, -500),
    ]
    return kdb.Polygon([kdb.Point(x, y) for x, y in points])


def _marker_vertices(path, shapes):
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    for layer, shape in shapes:
        top.shapes(layout.layer(*layer)).insert(shape)
    layout.write(str(path))
    markers = _markers(run_drc(str(path), "gf180mcu", rules=[RULE]))
    return sorted(sorted(map(tuple, marker["polygon"])) for marker in markers)


def test_remote_banded_tap_preserves_strict_sloped_markers(tmp_path):
    shapes = [(COMP, _sloped_tap()), (NPLUS, _notched_implant())]
    baseline = _marker_vertices(tmp_path / "strict.gds", shapes)
    assert baseline == [
        [(707, 998), (810, 963), (810, 1120), (810, 1132)],
        [(707, 998), (810, 1120), (820, 960), (846, 1120)],
    ]

    # A well can also cut the same source edge just before the violation.
    # The marker is still wholly strict, even though the tap spans the well.
    before_notch = (NWELL, kdb.Box(-1000, -1000, 701, 2000))
    assert (
        _marker_vertices(tmp_path / "before-notch.gds", [*shapes, before_notch])
        == baseline
    )

    shapes.extend(
        [
            (COMP, kdb.Box(10000, 0, 11000, 1000)),
            (NPLUS, kdb.Box(9800, -200, 11200, 1200)),
            (NWELL, kdb.Box(9000, -1000, 12000, 2000)),
        ]
    )
    assert _marker_vertices(tmp_path / "remote.gds", shapes) == baseline


def test_sloped_tap_inside_well_has_only_foundry_band_markers(tmp_path):
    tap = _sloped_tap()
    shapes = [
        (COMP, tap),
        (NPLUS, tap.sized(100)),
        (NWELL, kdb.Box(-1000, -1000, 1155, 2000)),
    ]
    # NP.5di / NP.5dii at foundry revision 10ee7fc7, KLayout 0.30.12.
    assert _marker_vertices(tmp_path / "inside.gds", shapes) == [
        [(726, -100), (726, 0), (1000, -100), (1000, 0)],
        [(726, 991), (758, 1086), (1000, 900), (1031, 995)],
        [(1000, 0), (1000, 900), (1100, 0), (1100, 900)],
    ]


def test_strict_part_of_a_sloped_edge_starts_at_its_well_crossing(tmp_path):
    shapes = [
        (COMP, _sloped_tap()),
        (NPLUS, _notched_implant()),
        (NWELL, kdb.Box(-1000, -1000, 711, 2000)),
    ]
    # At x=711 the original tap edge crosses at y=996 on the 1 nm grid.
    # Cutting either already-rounded marker at x=711 instead gives y=997.
    assert _marker_vertices(tmp_path / "crossing.gds", shapes) == [
        [(711, 996), (810, 963), (810, 1120), (810, 1132)],
        [(711, 996), (810, 1120), (819, 960), (845, 1120)],
    ]
