"""The curated gf180mcu implant rules against the pinned foundry expressions,
side by side (issue #7970).

Each foundry helper transcribes the relevant statements of the pinned
``rule_decks/{nplus,pplus}.drc`` and runs them through the ``klayout.db``
primitives their DRC uses; each curated helper reads the same stretches off
``run_drc``'s markers. Agreement is therefore not built in: the engine
selects its operands its own way (``other_layer_selection``, ``butted_spec``,
``band_spec``) and reports hull polygons, not edge pairs.

The coarse-grid and sloped-edge controls record results from the native
GF180 tables at revision 10ee7fc7, run with KLayout 0.30.12.

``nplus.enclosing.comp.1`` is compared against all three of its statements:
``NP.5b`` outside the wells, and ``NP.5di``/``NP.5dii`` inside a pure one,
where both drop the Pplus-coincident NCOMP edges and ``NP.5dii`` the
coincident ``Nplus`` edges too (issue #7987).
"""

from __future__ import annotations

from collections import Counter

import klayout.db as kdb
import pytest

from klayout_tools.drc import run_drc
from test_drc_butted_diffusion import (
    COMP,
    DNWELL,
    FAR,
    LVPWELL,
    NEAR,
    NP_ENC,
    NPLUS,
    NWELL,
    PP_ENC_FAR,
    PP_SPACE,
    PPLUS,
    _write,
)

#: The rules whose checked region is NCOMP rather than PCOMP.
_NCOMP_RULES = (NEAR, FAR, PP_SPACE, NP_ENC)


def _deep_ncomp(gap):
    """A ``Pplus`` bar ``gap`` from the right edge of a 1 um NCOMP deep inside
    a large pure Nwell: 60nm violates PP.3ci, 100nm is inside its 0.08um."""
    return [
        (NWELL, (-2000, -2000, 5000, 5000)),
        (COMP, (0, 0, 1000, 1000)),
        (NPLUS, (-200, -200, 1200, 1200)),
        (PPLUS, (1000 + gap, 0, 2000 + gap, 1000)),
    ]


#: An NCOMP and a PCOMP sharing an edge, each implant butted to the other's
#: diffusion.
_BUTTED_PAIR = [
    (COMP, (0, 0, 2000, 1000)),
    (NPLUS, (-160, -160, 1000, 1160)),
    (PPLUS, (1000, -160, 2160, 1160)),
]

#: One ``Comp`` under both implants, each running past it above and below, so
#: 0.16um stubs of the butted ``Nplus`` edge remain beyond the coincident
#: stretch -- the ordinary butted pair.
_BUTTED_WITH_STUBS = [
    (COMP, (0, 0, 4000, 2000)),
    (NPLUS, (-160, -160, 2000, 2160)),
    (PPLUS, (2000, -160, 4160, 2160)),
]

#: Fixtures per rule, by case name.
_CASES = {
    NP_ENC: [
        ("butted_pair_with_stubs", _BUTTED_WITH_STUBS),
        (
            "butted_and_short_side",
            [
                (COMP, (0, 0, 1000, 1000)),
                (COMP, (1000, -160, 2000, 1160)),
                (NPLUS, (-160, -160, 1000, 1080)),
                (PPLUS, (1000, -320, 2160, 1320)),
            ],
        ),
        (
            # the butted pair and, in the same cell, a tap Nplus under-extends
            "butted_pair_and_under_extended_tap",
            _BUTTED_WITH_STUBS
            + [(COMP, (0, 5000, 1000, 6000)), (NPLUS, (-100, 4900, 1100, 6100))],
        ),
        # Inside a pure Nwell NP.5di/NP.5dii drop the butted NCOMP edge, as
        # NP.5b drops it outside (issue #7987).
        (
            "butted_pair_with_stubs_in_a_well",
            _BUTTED_WITH_STUBS + [(NWELL, (-1000, -1000, 6000, 3000))],
        ),
        (
            # only the enclosing Nplus edge coincides with Pplus: NP.5dii
            # drops it from its operand, NP.5di keeps it and, 200nm from the
            # well boundary, reports the 10nm it falls short
            "enclosing_edge_coincident_deep_in_a_well",
            [
                (COMP, (0, 0, 1000, 1000)),
                (NPLUS, (-160, -160, 1010, 1160)),
                (PPLUS, (1010, -160, 1500, 1160)),
                (NWELL, (-1000, -1000, 6000, 3000)),
            ],
        ),
        (
            "enclosing_edge_coincident_in_the_band",
            [
                (COMP, (0, 0, 1000, 1000)),
                (NPLUS, (-160, -160, 1010, 1160)),
                (PPLUS, (1010, -160, 1500, 1160)),
                (NWELL, (-1000, -1000, 1200, 3000)),
            ],
        ),
        (
            # the Nplus edge coincides with only the middle of the butted edge,
            # in the band: the two remaining stretches are NP.5di's. (Deep
            # inside, NP.5dii's collinear markers collapse to two hull points;
            # those are compared by bounding box in test_drc_edge_band_foundry.py.)
            "partly_coincident_edge_in_the_band",
            [
                (COMP, (0, 0, 4000, 2000)),
                (NPLUS, (-160, -160, 2000, 2160)),
                (PPLUS, (2000, 500, 4160, 1500)),
                (NWELL, (-1000, -1000, 2200, 3000)),
            ],
        ),
    ],
    PP_ENC_FAR: [
        (
            # the Nplus edge coincides with only the middle of the butted edge
            "partly_coincident_edge",
            [
                (COMP, (0, 0, 2000, 1000)),
                (NPLUS, (0, 300, 1000, 700)),
                (PPLUS, (1000, -20, 2160, 1020)),
            ],
        ),
        (
            "butted_and_short_sides",
            [
                (COMP, (0, 0, 2000, 1000)),
                (NPLUS, (0, 0, 1000, 1000)),
                (PPLUS, (1000, -10, 2160, 1010)),
            ],
        ),
    ],
    PP_SPACE: [
        # A butted pair with a Pplus bar 110nm over its NCOMP. Touching the
        # butted Pplus, the bar is one merged polygon with it and leaves with
        # it; 10nm clear of it, the bar is measured on its own.
        ("butted_pair_joined_pplus", _BUTTED_PAIR + [(PPLUS, (0, 1110, 1000, 2110))]),
        ("butted_pair_separate_pplus", _BUTTED_PAIR + [(PPLUS, (0, 1110, 990, 2110))]),
        ("deep_60nm", _deep_ncomp(60)),
        ("deep_100nm", _deep_ncomp(100)),
        (
            "crossing_the_deep_boundary",
            [
                (NWELL, (-200, -2000, 3000, 3000)),
                (COMP, (0, 0, 2000, 1000)),
                (NPLUS, (-160, -160, 2160, 1160)),
                (PPLUS, (1200, 1100, 1700, 1600)),
                (PPLUS, (0, 1100, 200, 1600)),
            ],
        ),
        (
            # one bar 70nm from two NCOMPs, both deep inside the same well
            "two_deep_ncomps",
            [
                (NWELL, (-2000, -2000, 5000, 5000)),
                (COMP, (0, 0, 1000, 1000)),
                (COMP, (3000, 0, 4000, 1000)),
                (NPLUS, (-200, -200, 1200, 1200)),
                (NPLUS, (2800, -200, 4200, 1200)),
                (PPLUS, (1070, 0, 2930, 1000)),
            ],
        ),
    ],
}


def _fixture(tmp_path, shapes):
    """Write ``shapes`` to GDS and read it back, so the foundry helpers see
    exactly the stream the curated engine ran on."""
    path = _write(shapes, tmp_path / "fixture.gds")
    layout = kdb.Layout()
    layout.read(path)
    return path, layout


def _region(layout, layer):
    index = layout.find_layer(*layer)
    if index is None:
        return kdb.Region()
    return kdb.Region(layout.top_cell().begin_shapes_rec(index)).merged()


def _sides(edge_pairs, side="first"):
    """``Counter`` of the stretches (sorted endpoint pairs) of ``side``."""
    sides = Counter()
    for pair in edge_pairs.each():
        edge = pair.first if side == "first" else pair.second
        sides[tuple(sorted(((edge.x1, edge.y1), (edge.x2, edge.y2))))] += 1
    return sides


def _foundry(layout, rule):
    comp = _region(layout, COMP)
    nplus, pplus = _region(layout, NPLUS), _region(layout, PPLUS)
    ncomp, pcomp = comp & nplus, comp & pplus
    projection = {"metrics": kdb.Edges.Projection}
    if rule == NP_ENC:  # NP.5b, NP.5di, NP.5dii
        nwell, dnwell = _region(layout, NWELL), _region(layout, DNWELL)
        pplus_edges = pplus.edges()
        # NP.5b measures the selected Nplus edges outside every well (no
        # LVPWELL is drawn in these fixtures).
        np5b_nplus = nplus.edges() - (nplus.edges() & pcomp.edges())
        sides = _sides(
            ncomp.edges().enclosed_check(np5b_nplus - nwell - dnwell, 160, **projection)
        )
        # NP.5di / NP.5dii drop the Pplus-coincident edges: NP.5di from the
        # checked side alone, NP.5dii from the enclosing Nplus edges too
        # (issue #7987).
        nwell_n_dn = nwell.not_interacting(dnwell)
        sized_in = nwell_n_dn.sized(-429)
        well_nplus = (nplus - dnwell).not_outside(nwell).edges()
        at_well = ncomp.not_outside(nwell).outside(dnwell).edges() - pplus_edges
        for implant, edges, distance, metrics in (
            (well_nplus, nwell_n_dn - sized_in, 160, kdb.Edges.Projection),  # NP.5di
            (well_nplus - pplus_edges, sized_in, 20, kdb.Edges.Euclidian),  # NP.5dii
        ):
            pairs = implant.enclosing_check(at_well & edges, distance, metrics=metrics)
            sides.update(_sides(pairs, "second"))
        return sides
    if rule == PP_ENC_FAR:  # PP.5di
        inner = pcomp.edges() - (pcomp.edges() & nplus.edges())
        pairs = pplus.edges().enclosing_check(inner, 20, **projection)
        return _sides(pairs, side="second")
    assert rule == PP_SPACE, rule
    # PP.3a, PP.3ci, PP.3cii: the boolean well-depth split of NCOMP.
    measured = pplus.not_interacting(pcomp.interacting(ncomp))
    pure = _region(layout, NWELL).not_interacting(_region(layout, DNWELL))
    inner = pure.sized(-429)  # These side-by-side fixtures use a 1 nm grid.
    in_well = measured & pure
    sides = Counter()
    for implant, checked, distance in (
        (measured, (ncomp & _region(layout, LVPWELL)) + (ncomp - pure), 160),  # PP.3a
        (in_well, ncomp & (pure - inner), 160),  # PP.3cii
        (in_well, ncomp & inner, 80),  # PP.3ci
    ):
        sides.update(_sides(implant.separation_check(checked, distance), "second"))
    return sides


def _curated(path, rule, layout):
    """Each marker's own checked-side stretch: the two hull points on the
    checked diffusion's boundary (the other two lie on the implant)."""
    implant = NPLUS if rule in _NCOMP_RULES else PPLUS
    boundary = (_region(layout, implant) & _region(layout, COMP)).edges()
    report = run_drc(path, "gf180mcu", rules=[rule])
    sides = Counter()
    for violation in report["violations"]:
        if violation["rule"] != rule:
            continue
        own = [
            tuple(point)
            for point in violation["polygon"]
            if any(edge.contains(kdb.Point(*point)) for edge in boundary.each())
        ]
        assert len(own) == 2, violation["polygon"]
        sides[tuple(sorted(own))] += 1
    return sides


def _cases(rule):
    return [pytest.param(shapes, id=name) for name, shapes in _CASES[rule]]


@pytest.mark.parametrize("shapes", _cases(NP_ENC))
def test_nplus_enclosure_matches_np5b(tmp_path, shapes):
    path, layout = _fixture(tmp_path, shapes)

    assert _curated(path, NP_ENC, layout) == _foundry(layout, NP_ENC)


@pytest.mark.parametrize("shapes", _cases(PP_ENC_FAR))
def test_pplus_enclosure_matches_pp5di(tmp_path, shapes):
    path, layout = _fixture(tmp_path, shapes)

    assert _curated(path, PP_ENC_FAR, layout) == _foundry(layout, PP_ENC_FAR)


@pytest.mark.parametrize("shapes", _cases(PP_SPACE))
def test_pplus_spacing_matches_pp3(tmp_path, shapes):
    path, layout = _fixture(tmp_path, shapes)

    assert _curated(path, PP_SPACE, layout) == _foundry(layout, PP_SPACE)


@pytest.mark.parametrize("dbu", [0.005, 0.01])
@pytest.mark.parametrize(("gap", "expected"), [(60, 1), (80, 0), (100, 0)])
def test_the_well_depth_split_survives_a_coarser_grid(tmp_path, dbu, gap, expected):
    """The band width and both thresholds are physical distances: the issue's
    fixture written on a 5 nm or a 10 nm grid selects the same limit."""
    path = _write(_deep_ncomp(gap), tmp_path / "coarse.gds", dbu=dbu)

    report = run_drc(path, "gf180mcu", rules=[PP_SPACE])

    assert report["rule_counts"].get(PP_SPACE, 0) == expected, report["violations"]


@pytest.mark.parametrize(("well_right", "expected"), [(1050, 0), (1100, 0), (1200, 1)])
def test_pp3cii_clips_the_implant_to_the_well(tmp_path, well_right, expected):
    shapes = _deep_ncomp(100)
    shapes[0] = (NWELL, (-2000, -2000, well_right, 3000))
    path, layout = _fixture(tmp_path, shapes)
    sides = _foundry(layout, PP_SPACE)
    assert sum(sides.values()) == expected
    assert _curated(path, PP_SPACE, layout) == sides


def test_pp3a_and_pp3cii_keep_adjoining_markers(tmp_path):
    shapes = [
        (COMP, (-400, 0, 400, 1000)),
        (NPLUS, (-600, -200, 600, 1200)),
        (PPLUS, (-300, 1100, 300, 1500)),
        (NWELL, (0, -2000, 2500, 3000)),
    ]
    path, layout = _fixture(tmp_path, shapes)
    sides = Counter([((-400, 1000), (0, 1000)), ((0, 1000), (400, 1000))])
    assert _curated(path, PP_SPACE, layout) == _foundry(layout, PP_SPACE) == sides


def test_overlapping_lvpwell_and_deep_well_keep_both_statements(tmp_path):
    shapes = [*_deep_ncomp(60), (LVPWELL, (-500, -500, 1500, 1500))]
    path, layout = _fixture(tmp_path, shapes)
    sides = Counter({((1000, 0), (1000, 1000)): 2})
    assert _curated(path, PP_SPACE, layout) == _foundry(layout, PP_SPACE) == sides


@pytest.mark.parametrize(("well_left", "expected"), [(-420, 1), (-426, 0), (-432, 0)])
def test_pp3ci_snaps_the_signed_well_inset(tmp_path, well_left, expected):
    shapes = [
        (COMP, (0, 0, 1008, 1008)),
        (NPLUS, (-180, -180, 1188, 1188)),
        (PPLUS, (0, 1110, 600, 1512)),
        (NWELL, (well_left, -1800, 4200, 4200)),
    ]
    path = _write(shapes, tmp_path / "inset.gds", dbu=0.006)
    assert len(run_drc(path, "gf180mcu", rules=[PP_SPACE])["violations"]) == expected


@pytest.mark.parametrize(
    ("rule", "grid", "in_well", "expected"),
    [
        (PP_SPACE, 32, True, 1),
        (PP_SPACE, 64, False, 1),
        (PP_ENC_FAR, 8, False, 4),
        (NP_ENC, 64, False, 4),
    ],
)
@pytest.mark.parametrize("units", [2, 3])
def test_half_grid_limit_is_three_layout_units(
    tmp_path, rule, grid, in_well, expected, units
):
    margin = units * grid
    shapes = [(COMP, (0, 0, 1024, 1024))]
    if rule == PP_SPACE:
        shapes += [
            (NPLUS, (-192, -192, 1216, 1216)),
            (PPLUS, (1024 + margin, 0, 2048 + margin, 1024)),
        ]
        if in_well:
            shapes.append((NWELL, (-2048, -2048, 4096, 4096)))
    else:
        implant = NPLUS if rule == NP_ENC else PPLUS
        shapes.append((implant, (-margin, -margin, 1024 + margin, 1024 + margin)))
    path = _write(shapes, tmp_path / "half-grid.gds", dbu=grid / 1000)
    assert len(run_drc(path, "gf180mcu", rules=[rule])["violations"]) == (
        expected if units == 2 else 0
    )


# The first marker of each case is the foundry's own region, now listed as a
# perimeter ring (#7979): the extra vertex the raw enclosing pair carried
# ((708, 996) / (716, 992)) sits on the straight side between two corners, and
# the ring walked from ``EdgePair.normalized()`` does not repeat it. The
# marker's region is unchanged.
@pytest.mark.parametrize(
    ("rule", "implant", "x", "y", "expected"),
    [
        (
            NP_ENC,
            NPLUS,
            867,
            996,
            [
                [(707, 996), (867, 1095), (867, 996)],
                [(867, 917), (1000, 850), (867, 996), (1066, 996)],
            ],
        ),
        (
            PP_ENC_FAR,
            PPLUS,
            735,
            992,
            [
                [(715, 992), (735, 1005), (735, 992)],
                [(735, 983), (756, 972), (735, 992), (761, 992)],
            ],
        ),
    ],
)
@pytest.mark.parametrize("remote_mask", [False, True])
def test_projection_retains_both_notch_markers(
    tmp_path, rule, implant, x, y, expected, remote_mask
):
    shapes = [
        (COMP, [(0, 0), (0, 1000), (700, 1000), (1000, 850), (1000, 0)]),
        (
            implant,
            [(-400, -400), (-400, 1500), (x, 1500), (x, y), (1600, y), (1600, -400)],
        ),
    ]
    if remote_mask:
        remote = (3000, 0, 3100, 100)
        shapes += [(COMP, remote), (PPLUS if implant == NPLUS else NPLUS, remote)]
    path = _write(shapes, tmp_path / "notch.gds")
    markers = run_drc(path, "gf180mcu", rules=[rule])["violations"]
    assert sorted(sorted(map(tuple, m["polygon"])) for m in markers) == sorted(
        sorted(points) for points in expected
    )
