"""The curated gf180mcu implant rules against the pinned foundry expressions,
side by side (issue #7970).

Each foundry helper transcribes the relevant statements of the pinned
``rule_decks/{nplus,pplus}.drc`` and runs them through the ``klayout.db``
primitives their DRC uses; each curated helper reads the same stretches off
``run_drc``'s markers. Agreement is therefore not built in: the engine
selects its operands its own way (``other_layer_selection``, ``butted_spec``,
``band_spec``) and reports hull polygons, not edge pairs.

No fixture draws ``DNWELL`` or ``LVPWELL``, whose sub-cases the curated deck
does not express.
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
    if rule == NP_ENC:  # NP.5b
        outer = nplus.edges() - (nplus.edges() & pcomp.edges())
        return _sides(ncomp.edges().enclosed_check(outer, 160, **projection))
    if rule == PP_ENC_FAR:  # PP.5di
        inner = pcomp.edges() - (pcomp.edges() & nplus.edges())
        pairs = pplus.edges().enclosing_check(inner, 20, **projection)
        return _sides(pairs, side="second")
    assert rule == PP_SPACE, rule
    # PP.3a, PP.3ci, PP.3cii: the boolean well-depth split of NCOMP.
    measured = pplus.not_interacting(pcomp.interacting(ncomp))
    pure = _region(layout, NWELL).not_interacting(_region(layout, DNWELL))
    inner = pure.sized(-round(0.429 / layout.dbu))
    sides = Counter()
    for checked, distance in (
        (ncomp - pure, 160),  # PP.3a: outside the pure well
        (ncomp & (pure - inner), 160),  # PP.3cii
        (ncomp & inner, 80),  # PP.3ci
    ):
        sides.update(_sides(measured.separation_check(checked, distance), "second"))
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
