"""Strict GF180 tap enclosure at touching wells and shielded implants.

``NP.5b`` measures by ``projection`` (issue #7970): a pair's implant edge is
reported over the stretch it faces the tap edge, and an edge butted to the
opposite diffusion is not measured at all. The marker lists below are the
pinned ``NP.5b``/``NP.5di``/``NP.5dii`` expressions' own on the same
geometry, KLayout 0.30.12, except where a test says otherwise.
"""

import klayout.db as kdb
import pytest

from klayout_tools.drc import run_drc
from test_drc_butted_diffusion import PP_ENC_FAR, PPLUS, _boxes
from test_drc_butted_diffusion import _run as _run_pair
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
            (-80, 0),
            (-80, 1000),
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
        assert [(500, 1000), (500, 1080), (1000, 1000), (1000, 1080)] in baseline
    else:
        assert len(baseline) == 4
    right_edge = [(1000, 0), (1000, 1000), (1080, 0), (1080, 1000)]
    assert right_edge in baseline
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


#: A second, passing tap deep inside a well: it uses the band, the first
#: tap does not.
_REMOTE_BANDED_TAP = [
    (COMP, kdb.Box(10000, 0, 11000, 1000)),
    (NPLUS, kdb.Box(9800, -200, 11200, 1200)),
    (NWELL, kdb.Box(9000, -1000, 12000, 2000)),
]


def _butted_sloped_shapes():
    """The sloped tap under an implant that overhangs 100nm everywhere but
    hugs the tap's right edge (x=1000), which a PCOMP east of it shares."""
    implant = kdb.Region(_sloped_tap().sized(100)) & kdb.Region(
        kdb.Box(-1000, -1000, 1000, 2000)
    )
    pcomp = kdb.Box(1000, -500, 2000, 1500)
    return [(COMP, _sloped_tap()), (NPLUS, implant), (COMP, pcomp), (PPLUS, pcomp)]


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
    assert baseline == [[(810, 963), (810, 1120), (820, 960), (846, 1120)]]

    # A well can also cut the same source edge just before the violation.
    # The marker is still wholly strict, even though the tap spans the well.
    before_notch = (NWELL, kdb.Box(-1000, -1000, 701, 2000))
    assert (
        _marker_vertices(tmp_path / "before-notch.gds", [*shapes, before_notch])
        == baseline
    )

    shapes.extend(_REMOTE_BANDED_TAP)
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
        (NPLUS, _sloped_tap().sized(100)),
        (NWELL, kdb.Box(-1000, -1000, 811, 2000)),
    ]
    # At x=811 the original tap edge crosses at y=963 on the 1 nm grid, and
    # the band's sloped marker ends where the strict one starts. The band
    # markers and the two straight strict markers are the pinned statements'
    # own; the pinned NP.5b cuts the implant edge at the well instead, so its
    # sloped marker starts at (780, 973) on the same stretch.
    assert _marker_vertices(tmp_path / "crossing.gds", shapes) == [
        [(382, -100), (382, 0), (811, -100), (811, 0)],
        [(382, 1000), (382, 1100), (700, 1000), (700, 1100)],
        [(700, 1000), (732, 1095), (811, 963), (843, 1058)],
        [(811, -100), (811, 0), (1000, -100), (1000, 0)],
        [(811, 963), (843, 1058), (1000, 900), (1032, 995)],
        [(1000, 0), (1000, 900), (1100, 0), (1100, 900)],
    ]


def test_a_butted_edge_of_a_crossing_sloped_tap_is_not_measured(tmp_path):
    shapes = _butted_sloped_shapes()
    # No well: the right edge has no extension and is butted, the other
    # four have 100nm. All four markers are the pinned NP.5b's.
    assert _marker_vertices(tmp_path / "strict.gds", shapes) == [
        [(-100, 0), (-100, 1000), (0, 0), (0, 1000)],
        [(0, -100), (0, 0), (1000, -100), (1000, 0)],
        [(0, 1000), (0, 1100), (700, 1000), (700, 1100)],
        [(700, 1000), (732, 1095), (968, 911), (1000, 1005)],
    ]

    # The well ends at x=711. The right edge, now wholly strict, stays
    # unmeasured; the sloped edge's strict marker starts at the crossing.
    shapes.append((NWELL, kdb.Box(-1000, -1000, 711, 2000)))
    crossing = [
        [(282, -100), (282, 0), (711, -100), (711, 0)],
        [(282, 1000), (282, 1100), (700, 1000), (700, 1100)],
        [(700, 1000), (711, 996), (734, 1094), (745, 1090)],
        [(711, -100), (711, 0), (1000, -100), (1000, 0)],
        [(711, 996), (743, 1091), (968, 911), (1000, 1005)],
    ]
    assert _marker_vertices(tmp_path / "crossing.gds", shapes) == crossing
    shapes.extend(_REMOTE_BANDED_TAP)
    assert _marker_vertices(tmp_path / "remote.gds", shapes) == crossing


def test_a_butted_edge_in_the_strict_part_of_a_mixed_tap_is_not_measured(tmp_path):
    """The well ends at x=600: the under-extended top edge is in the band up
    to there and strict beyond, and the butted right edge is strict. The top
    edge is reported in both portions; the right edge, whose Nplus is butted
    to PCOMP, is not. Both markers are the pinned statements' own."""
    shapes = [
        (COMP, kdb.Box(0, 0, 1000, 1000)),
        (COMP, kdb.Box(1000, -160, 2000, 1160)),
        (NPLUS, kdb.Box(-160, -160, 1000, 1080)),
        (PPLUS, kdb.Box(1000, -320, 2160, 1320)),
        (NWELL, kdb.Box(-1000, -1000, 600, 3000)),
    ]

    assert _marker_vertices(tmp_path / "crossing.gds", shapes) == [
        [(171, 1000), (171, 1080), (600, 1000), (600, 1080)],
        [(600, 1000), (600, 1080), (1000, 1000), (1000, 1080)],
    ]


def test_the_far_pplus_enclosure_rule_keeps_its_shielding(tmp_path):
    """``PP.5di`` reads the same shielded region check. A second ``Pplus``
    sliver whose far edge is 19nm from the PCOMP's right edge -- behind the
    implant enclosing it -- adds no fifth marker to the four a 10nm
    extension gives."""
    shapes = [(COMP, (0, 0, 1000, 1000)), (PPLUS, (-10, -10, 1010, 1010))]

    baseline = _boxes(_run_pair(tmp_path, shapes, [PP_ENC_FAR]), PP_ENC_FAR)

    assert baseline == [
        (-10, 0, 0, 1000),
        (0, -10, 1000, 0),
        (0, 1000, 1000, 1010),
        (1000, 0, 1010, 1000),
    ]
    shapes.append((PPLUS, (1015, 0, 1019, 1000)))
    with_neighbor = _run_pair(tmp_path, shapes, [PP_ENC_FAR], name="neighbor.gds")
    assert _boxes(with_neighbor, PP_ENC_FAR) == baseline


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
def test_a_notched_implant_corner_is_clean_by_projection(tmp_path, interior_well):
    """The notch corner at (1025, 1158) is 159.97nm from the tap's corner.
    `NP.5b` measures by `projection`, which pairs neither tap edge with
    either notch edge, so nothing is reported -- the pinned statement's own
    result. Under `euclidian` both adjacent tap edges paired with that
    corner, and the band reported it once."""
    shapes = [
        (COMP, kdb.Box(0, 0, 1000, 1000)),
        (NPLUS, kdb.Box(-500, -500, 1500, 1158)),
        (NPLUS, kdb.Box(-500, 1158, 1025, 1500)),
    ]
    if interior_well:
        shapes.append((NWELL, kdb.Box(300, 300, 500, 500)))

    assert _marker_vertices(tmp_path / "notched.gds", shapes) == []
