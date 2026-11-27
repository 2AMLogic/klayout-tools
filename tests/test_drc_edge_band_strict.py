"""GF180 tap enclosure on coarse grids and at well and implant boundaries."""

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


def _marker_vertices(path, shapes, dbu=0.001):
    layout = kdb.Layout()
    layout.dbu = dbu
    top = layout.create_cell("TOP")
    for layer, shape in shapes:
        top.shapes(layout.layer(*layer)).insert(shape)
    layout.write(str(path))
    markers = _markers(run_drc(str(path), "gf180mcu", rules=[RULE]))
    return sorted(sorted(map(tuple, marker["polygon"])) for marker in markers)


@pytest.mark.parametrize(
    ("dbu", "side", "well_margin", "implant_margin", "expected"),
    [
        (0.008, 125, 125, 2, 4),  # NP.5dii: 16 nm < 20 nm
        (0.008, 125, 125, 3, 0),
        (0.064, 16, 4, 2, 4),  # NP.5di: 128 nm < 160 nm
        (0.064, 16, 4, 3, 0),
        (0.006, 167, 71, 13, 4),  # The -429 nm well inset snaps to -71 DBU.
        (0.006, 167, 72, 13, 0),
        # KLayout divides the physical 0.16 um by the DBU. On this grid
        # that quotient is just below 62.5 in floating point.
        (0.00256, 400, 100, 62, 0),
    ],
)
def test_half_grid_well_depth_distances(
    tmp_path, dbu, side, well_margin, implant_margin, expected
):
    tap = kdb.Box(0, 0, side, side)
    shapes = [
        (COMP, tap),
        (NPLUS, tap.enlarged(implant_margin, implant_margin)),
        (NWELL, tap.enlarged(well_margin, well_margin)),
    ]
    assert len(_marker_vertices(tmp_path / "tap.gds", shapes, dbu)) == expected


@pytest.mark.parametrize(
    ("well_left", "overlaps_other_well", "expect_marker"),
    [
        (999, False, True),
        (1000, False, False),
        (1001, False, False),
        (1000, True, True),
    ],
)
def test_tap_must_overlap_a_well_to_use_the_cut_implant(
    tmp_path, well_left, overlaps_other_well, expect_marker
):
    shapes = [
        (COMP, kdb.Box(0, 0, 1000, 1000)),
        (NPLUS, kdb.Box(-200, -200, 1200, 1200)),
        (NWELL, kdb.Box(well_left, -1200, 1050, 2200)),
        (DNWELL, kdb.Box(1100, -2500, 3500, 3500)),
    ]
    if overlaps_other_well:
        # The foundry selects whole taps against all Nwell, then measures
        # their edges in the band of pure wells only.
        shapes.extend(
            [
                (NWELL, kdb.Box(-1000, -1200, 500, 2200)),
                (DNWELL, kdb.Box(-2000, -2500, -900, 3500)),
            ]
        )
    marker = [(1000, 0), (1000, 1000), (1100, 0), (1100, 1000)]
    assert _marker_vertices(tmp_path / "tap.gds", shapes) == (
        [marker] if expect_marker else []
    )


def test_external_well_contact_keeps_drawn_implant_violations(tmp_path):
    shapes = _tap_shapes(80, None)
    baseline = _markers(_run(tmp_path, shapes))
    assert len(baseline) == 4
    shapes.append({"layer": list(NWELL), "box": [1000, -1200, 1050, 2200]})
    assert _markers(_run(tmp_path, shapes)) == baseline


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


def test_dnwell_cut_in_implant_sets_band_enclosure(tmp_path):
    shapes = [
        (COMP, kdb.Box(0, 0, 1000, 1000)),
        (NPLUS, kdb.Box(-200, -200, 1200, 1200)),
        (NWELL, kdb.Box(-1200, -1200, 1050, 2200)),
    ]
    assert _marker_vertices(tmp_path / "drawn.gds", shapes) == []

    # The well and tap are clear of DNWELL. Only the implant is cut.
    shapes.append((DNWELL, kdb.Box(1100, -2500, 3500, 3500)))
    assert _marker_vertices(tmp_path / "cut.gds", shapes) == [
        [(1000, 0), (1000, 1000), (1100, 0), (1100, 1000)]
    ]


@pytest.mark.parametrize("interior_well", [False, True])
def test_strict_corner_is_reported_once(tmp_path, interior_well):
    shapes = [
        {"layer": list(COMP), "box": [0, 0, 1000, 1000]},
        # Two rectangles form an implant with a notch at (1025, 1158).
        {"layer": list(NPLUS), "box": [-500, -500, 1500, 1158]},
        {"layer": list(NPLUS), "box": [-500, 1158, 1025, 1500]},
    ]
    if interior_well:
        shapes.append({"layer": list(NWELL), "box": [300, 300, 500, 500]})

    assert [m["bbox"] for m in _markers(_run(tmp_path, shapes))] == [
        {"left": 1000, "bottom": 1000, "right": 1025, "top": 1158}
    ]
