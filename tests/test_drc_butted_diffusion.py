"""gf180mcu implant rules follow the pinned NP/PP tables' butted-diffusion
exclusions and well-depth limits (issue #7970).

The pinned ``rule_decks/{nplus,pplus}.drc`` drop the implant geometry of a
butted NCOMP/PCOMP pair before measuring -- ``nplus.not_interacting(
ncomp_butted)`` for spacing, the coincident edge segments for ``NP.5b`` and
``PP.5di`` -- and hold an NCOMP deep inside a pure Nwell to 0.08um
(``PP.3ci``). The curated rules kept every edge and the flat limits, so four
spacing/enclosure categories reported 180 markers on five Tiny Tapeout
layouts where the foundry tables report none. Every count and position below
is the pinned expressions' own on the same geometry.
"""

from __future__ import annotations

import dataclasses
import math

import klayout.db as kdb
import pytest

from klayout_tools.decks import DerivedLayer, DrcRule, get_deck
from klayout_tools.drc import DrcError, list_deck_rules, run_drc
from klayout_tools.drc_butted_diffusion import ButtedEdgeSpec
from klayout_tools.drc_edge_band import EdgeBandSpec

COMP, NWELL, DNWELL = (22, 0), (21, 0), (12, 0)
PPLUS, NPLUS, LVPWELL = (31, 0), (32, 0), (204, 0)
NEAR, FAR = "nplus.space.pcomp.1", "nplus.space.pcomp.far.1"
PP_SPACE = "pplus.space.ncomp.1"
NP_ENC, PP_ENC_FAR = "nplus.enclosing.comp.1", "pplus.enclosing.comp.far.1"
PP_ENC = "pplus.enclosing.comp.1"


def _write(shapes, path, dbu=0.001):
    """One ``TOP`` cell holding ``shapes`` -- ``(layer, box)`` pairs or
    ``(layer, [(x, y), ...])`` polygons -- authored on the deck's 1 nm grid
    and written at ``dbu`` micrometres per unit."""
    layout = kdb.Layout()
    layout.dbu = dbu
    scale = dbu / 0.001
    top = layout.create_cell("TOP")
    for layer, geometry in shapes:
        if len(geometry) == 4:
            x0, y0, x1, y1 = geometry
            geometry = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
        points = [
            kdb.Point(*(round(value / scale) for value in point)) for point in geometry
        ]
        top.shapes(layout.layer(*layer)).insert(kdb.Polygon(points))
    layout.write(str(path))
    return str(path)


def _run(tmp_path, shapes, rules, name="fixture.gds"):
    return run_drc(_write(shapes, tmp_path / name), "gf180mcu", rules=list(rules))


def _boxes(report, rule):
    boxes = []
    for violation in report["violations"]:
        if violation["rule"] == rule:
            bbox = violation["bbox"]
            boxes.append((bbox["left"], bbox["bottom"], bbox["right"], bbox["top"]))
    return sorted(boxes)


def _count(report, rule):
    return report["rule_counts"].get(rule, 0)


# --------------------------------------------------------------------------- #
# Spacing: the butted implant is not measured (NP.3a/NP.3cii, PP.3a/PP.3ci)
# --------------------------------------------------------------------------- #

#: (rule, pair inside an Nwell, violating gap, gap at the limit, the violating
#: marker): inside a well the Nplus pair is the near rule (0.16um), with no
#: well the far one (0.08um); implants swapped, the Pplus rule (PP.3a, 0.16um).
_SPACING = [
    (NEAR, True, 110, 160, (2000, 0, 2110, 1000)),
    (FAR, False, 50, 80, (2000, 0, 2050, 1000)),
    (PP_SPACE, False, 110, 160, (0, 1000, 1000, 1110)),
]


def _butted_pair(rule, well, bar_gap=None):
    """One ``Comp`` split between an NCOMP and a PCOMP sharing an edge, each
    implant butted to the other's diffusion; optionally an unrelated bar of
    the measured implant ``bar_gap`` from the checked COMP, clear of
    ``Comp``."""
    shapes = [
        (COMP, (0, 0, 2000, 1000)),
        (NPLUS, (-160, -160, 1000, 1160)),
        (PPLUS, (1000, -160, 2160, 1160)),
    ]
    if well:
        shapes.append((NWELL, (-600, -600, 2600, 1600)))
    if bar_gap is not None:
        if rule == PP_SPACE:
            # 10nm clear of the butted Pplus, which would otherwise merge with it.
            shapes.append((PPLUS, (0, 1000 + bar_gap, 990, 2000 + bar_gap)))
        else:
            shapes.append((NPLUS, (2000 + bar_gap, 0, 3000 + bar_gap, 1000)))
    return shapes


@pytest.mark.parametrize(("rule", "well", "_gap", "_limit", "_box"), _SPACING)
def test_butted_implant_is_silent(tmp_path, rule, well, _gap, _limit, _box):
    """The measured implant touches the opposite COMP at zero distance, but it
    is the implant of the butted pair: the rule is evaluated and silent."""
    report = _run(tmp_path, _butted_pair(rule, well), [rule])

    assert _count(report, rule) == 0, report["violations"]
    assert rule in report["coverage"]["rules_checked"]


@pytest.mark.parametrize(("rule", "well", "gap", "_limit", "box"), _SPACING)
def test_unbutted_implant_with_a_subminimum_gap_still_reports(
    tmp_path, rule, well, gap, _limit, box
):
    """The exclusion is of the butted implant only: in the same cell an
    unrelated implant bar ``gap`` from the checked COMP is reported, at the
    bar and nowhere else."""
    report = _run(tmp_path, _butted_pair(rule, well, gap), [rule])

    assert _boxes(report, rule) == [box], report["violations"]


@pytest.mark.parametrize(("rule", "well", "_gap", "limit", "_box"), _SPACING)
def test_unbutted_implant_at_the_limit_passes(tmp_path, rule, well, _gap, limit, _box):
    report = _run(tmp_path, _butted_pair(rule, well, limit), [rule])

    assert _count(report, rule) == 0, report["violations"]


# --------------------------------------------------------------------------- #
# Pplus spacing to NCOMP: PP.3ci relaxes an NCOMP deep inside a pure Nwell
# --------------------------------------------------------------------------- #


def _curator_fixture(gap):
    """The issue's reproduction: an NCOMP deep inside an Nwell, a ``Pplus``
    bar ``gap`` from its right edge."""
    return [
        (NWELL, (-2000, -2000, 5000, 5000)),
        (COMP, (0, 0, 1000, 1000)),
        (NPLUS, (-200, -200, 1200, 1200)),
        (PPLUS, (1000 + gap, 0, 2000 + gap, 1000)),
    ]


@pytest.mark.parametrize(
    ("gap", "expected"), [(60, 1), (80, 0), (100, 0), (160, 0), (200, 0)]
)
def test_deep_nwell_ncomp_is_held_to_the_pp3ci_limit(tmp_path, gap, expected):
    """The issue's table: 0.08um inside a well that overlaps the NCOMP by at
    least 0.43um, where the flat 0.16um also reported 80nm and 100nm."""
    report = _run(tmp_path, _curator_fixture(gap), [PP_SPACE])

    assert _count(report, PP_SPACE) == expected, report["violations"]
    assert _boxes(report, PP_SPACE) == (
        [(1000, 0, 1000 + gap, 1000)] if expected else []
    )


@pytest.mark.parametrize(
    "context",
    [
        pytest.param([], id="no_well"),
        pytest.param([(NWELL, (-200, -2000, 1200, 3000))], id="edge_within_0.43um"),
        pytest.param(
            [
                (NWELL, (-2000, -2000, 5000, 5000)),
                (DNWELL, (-2500, -2500, -1900, -1900)),
            ],
            id="well_touching_a_dnwell",
        ),
        pytest.param(
            [(NWELL, (-2000, -2000, 5000, 5000)), (LVPWELL, (-500, -500, 1500, 1500))],
            id="ncomp_inside_an_lvpwell",
        ),
    ],
)
def test_a_100nm_gap_still_reports_in_the_160nm_contexts(tmp_path, context):
    """PP.3a covers an NCOMP inside an LVPWELL or outside a pure Nwell and
    PP.3cii one within 0.43um of the pure well's edge: all keep 0.16um."""
    shapes = [
        (COMP, (0, 0, 1000, 1000)),
        (NPLUS, (-200, -200, 1200, 1200)),
        (PPLUS, (1100, 0, 2100, 1000)),
        *context,
    ]

    report = _run(tmp_path, shapes, [PP_SPACE])

    assert _boxes(report, PP_SPACE) == [(1000, 0, 1100, 1000)], report["violations"]


def test_a_shape_crossing_the_deep_boundary_keeps_its_strict_portion(tmp_path):
    """An NCOMP running from within 0.43um of the well edge into the deep
    interior. A bar 100nm over the deep stretch passes; 100nm over the band
    stretch is reported against the whole band part's top edge (to x=229,
    where the interior begins) -- the pinned tables' own boolean selection,
    where a whole-polygon reading would have flagged either both or neither."""
    shapes = [
        (NWELL, (-200, -2000, 3000, 3000)),
        (COMP, (0, 0, 2000, 1000)),
        (NPLUS, (-160, -160, 2160, 1160)),
        (PPLUS, (1200, 1100, 1700, 1600)),
        (PPLUS, (0, 1100, 200, 1600)),
    ]

    report = _run(tmp_path, shapes, [PP_SPACE])

    assert _boxes(report, PP_SPACE) == [(0, 1000, 229, 1100)], report["violations"]


@pytest.mark.parametrize(
    ("context", "gap", "expected"),
    [
        ([(NWELL, (-2000, -2000, 5000, 5000))], 79, 1),  # deep: under PP.3ci
        ([(NWELL, (-2000, -2000, 5000, 5000))], 80, 0),  # deep: at PP.3ci
        ([], 159, 1),  # no well: under the flat 0.16um
        ([], 160, 0),
        ([(NWELL, (-200, -2000, 1200, 3000))], 159, 1),  # band: under PP.3cii
        ([(NWELL, (-200, -2000, 1200, 3000))], 160, 0),
    ],
)
def test_both_spacing_limits_pass_at_equality(tmp_path, context, gap, expected):
    shapes = [
        (COMP, (0, 0, 1000, 1000)),
        (NPLUS, (-200, -200, 1200, 1200)),
        (PPLUS, (1000 + gap, 0, 2000 + gap, 1000)),
        *context,
    ]

    report = _run(tmp_path, shapes, [PP_SPACE])

    assert _count(report, PP_SPACE) == expected, report["violations"]


# --------------------------------------------------------------------------- #
# Enclosure: eligible edge segments, projection metric (NP.5b, PP.5di)
# --------------------------------------------------------------------------- #


def _butted_pair_with_stubs():
    """The ordinary butted pair: one ``Comp`` under both implants, each
    implant running 0.16um past it above and below, so 0.16um stubs of each
    implant's butted edge remain beyond the coincident stretch."""
    return [
        (COMP, (0, 0, 4000, 2000)),
        (NPLUS, (-160, -160, 2000, 2160)),
        (PPLUS, (2000, -160, 4160, 2160)),
    ]


def _ncomp_tap(nplus_top):
    """An NCOMP square with 0.16um of ``Nplus`` on three sides and ``nplus_top
    - 1000`` on top, butted along its whole right edge to a PCOMP that
    extends past it above and below, so no stub of that edge remains."""
    return [
        (COMP, (0, 0, 1000, 1000)),
        (COMP, (1000, -160, 2000, 1160)),
        (NPLUS, (-160, -160, 1000, nplus_top)),
        (PPLUS, (1000, -320, 2160, 1320)),
    ]


def _pcomp_tap(pplus_margin):
    """A PCOMP square butted along its whole left edge to an ``Nplus`` of the
    same height, with ``pplus_margin`` of ``Pplus`` on its other three sides."""
    return [
        (COMP, (0, 0, 2000, 1000)),
        (NPLUS, (0, 0, 1000, 1000)),
        (PPLUS, (1000, -pplus_margin, 2160, 1000 + pplus_margin)),
    ]


def _chamfered(box, cut):
    """``box`` with its top-right corner cut by ``cut`` on both axes."""
    x0, y0, x1, y1 = box
    return [(x0, y0), (x1, y0), (x1, y1 - cut), (x1 - cut, y1), (x0, y1)]


@pytest.mark.parametrize("rule", [NP_ENC, PP_ENC_FAR])
def test_the_butted_side_of_each_diffusion_passes(tmp_path, rule):
    """Each implant butted to the opposite diffusion has zero extension along
    the shared edge. ``NP.5b`` drops that ``Nplus`` edge and ``PP.5di`` that
    PCOMP edge, so neither reports it; the flat rules reported one each."""
    report = _run(tmp_path, _butted_pair_with_stubs(), [rule])

    assert _count(report, rule) == 0, report["violations"]
    assert rule in report["coverage"]["rules_checked"]


@pytest.mark.parametrize(
    ("rule", "shapes", "boxes"),
    [
        (NP_ENC, _ncomp_tap(nplus_top=1080), [(0, 1000, 1000, 1080)]),
        (
            PP_ENC_FAR,
            _pcomp_tap(pplus_margin=10),
            [(1000, -10, 2000, 0), (1000, 1000, 2000, 1010)],
        ),
    ],
)
def test_a_short_unbutted_side_reports_at_that_side(tmp_path, rule, shapes, boxes):
    report = _run(tmp_path, shapes, [rule])

    assert _boxes(report, rule) == boxes, report["violations"]


@pytest.mark.parametrize(
    ("rule", "shapes", "at_limit"),
    [
        (NP_ENC, _ncomp_tap(nplus_top=1159), _ncomp_tap(nplus_top=1160)),
        (PP_ENC_FAR, _pcomp_tap(19), _pcomp_tap(20)),
    ],
)
def test_enclosure_equality(tmp_path, rule, shapes, at_limit):
    """The limit passes and one database unit under it reports."""
    assert _count(_run(tmp_path, at_limit, [rule]), rule) == 0
    assert _count(_run(tmp_path, shapes, [rule]), rule) >= 1


def _square_tap(layer, margin, chamfer):
    """A 1 um COMP square under ``layer``, which clears it by ``margin`` on
    every side and has its top-right corner cut by ``chamfer``."""
    box = (-margin, -margin, 1000 + margin, 1000 + margin)
    implant = _chamfered(box, chamfer) if chamfer else box
    return [(COMP, (0, 0, 1000, 1000)), (layer, implant)]


@pytest.mark.parametrize(
    ("rule", "layer", "limit", "margin", "cut"),
    [
        # The implant clears the COMP by more than the limit on every side,
        # with its corner chamfered: each side keeps the full limit by projection,
        # while the chamfer passes within (2 * margin - cut) / sqrt(2) < limit.
        (NP_ENC, NPLUS, 160, 170, 160),
        (PP_ENC_FAR, PPLUS, 20, 22, 20),
    ],
)
def test_extension_rules_measure_by_projection(
    tmp_path, rule, layer, limit, margin, cut
):
    assert (2 * margin - cut) / math.sqrt(2) < limit

    report = _run(tmp_path, _square_tap(layer, margin, cut), [rule])

    assert _count(report, rule) == 0, report["violations"]
    # The control differs only in 1 nm less implant on every side.
    under = _run(tmp_path, _square_tap(layer, limit - 1, 0), [rule])
    assert _count(under, rule) >= 1, under["violations"]


# --------------------------------------------------------------------------- #
# Declaration: what the rules carry, and what ``--list-rules`` reports
# --------------------------------------------------------------------------- #


def test_rules_declare_the_pinned_selections():
    deck = {rule.id: rule for rule in get_deck("gf180mcu")}

    assert {deck[r].other_layer_selection for r in (NEAR, FAR, PP_SPACE)} == {
        "not_butted"
    }
    assert deck[PP_SPACE].threshold_dbu == 160
    assert deck[PP_SPACE].band_spec == EdgeBandSpec(
        context_layers=(NWELL,),
        exclude_layers=(DNWELL,),
        band_width_um=0.429,
        inner_threshold_dbu=80,
        band_metrics="euclidian",
        strict_layers=(LVPWELL,),
    )
    assert deck[NP_ENC].butted_spec == ButtedEdgeSpec(
        side="other", mask_layers=(PPLUS,), mask_intersect=COMP
    )
    assert deck[PP_ENC_FAR].butted_spec == ButtedEdgeSpec(
        side="checked", mask_layers=(NPLUS,)
    )
    # The near Pplus enclosure rule keeps its butted edges (issue #7969).
    assert deck[PP_ENC].butted_spec is None
    assert deck[PP_ENC].other_layer_selection is None


def test_list_rules_reports_the_selections():
    listing = {entry["id"]: entry for entry in list_deck_rules("gf180mcu")["rules"]}

    assert listing[PP_SPACE]["threshold_um"] == 0.16
    assert listing[PP_SPACE]["edge_band"] == {
        "context_layers": ["21/0"],
        "exclude_layers": ["12/0"],
        "band_width_um": 0.429,
        "inner_threshold_um": 0.08,
        "strict_layers": ["204/0"],
    }
    assert listing[NP_ENC]["butted_edges"] == {
        "side": "other",
        "mask_layers": ["31/0"],
        "mask_intersect": "22/0",
        "metrics": "projection",
    }
    assert listing[PP_ENC_FAR]["butted_edges"] == {
        "side": "checked",
        "mask_layers": ["32/0"],
        "mask_intersect": None,
        "metrics": "projection",
    }
    # Additive: no other rule grows either key.
    rest = set(listing) - {NP_ENC, PP_ENC_FAR}
    assert rest and all("butted_edges" not in listing[key] for key in rest)
    assert "strict_layers" not in listing[NP_ENC]["edge_band"]


def test_butted_edge_spec_rejects_invalid_descriptors():
    valid = dict(side="other", mask_layers=(NPLUS,))
    with pytest.raises(ValueError, match="side"):
        ButtedEdgeSpec(**{**valid, "side": "both"})
    with pytest.raises(ValueError, match="mask_layers"):
        ButtedEdgeSpec(**{**valid, "mask_layers": ()})
    with pytest.raises(ValueError, match="metrics"):
        ButtedEdgeSpec(**{**valid, "metrics": "euclidean"})


def _synthetic(tmp_path, monkeypatch, **overrides):
    rule = DrcRule(
        **{
            "id": "s.rule.1",
            "description": "synthetic",
            "layer": (10, 0),
            "check": "enclosed",
            "threshold_dbu": 160,
            "other_layer": (20, 0),
            "butted_spec": ButtedEdgeSpec(side="other", mask_layers=((30, 0),)),
            **overrides,
        }
    )
    monkeypatch.setattr("klayout_tools.drc.get_deck", lambda name: [rule])
    monkeypatch.setattr("klayout_tools.drc.get_nominal_dbu", lambda name: 0.001)
    monkeypatch.setattr("klayout_tools.drc.get_layer_names", lambda name: {})
    return _write([], tmp_path / "empty.gds")


_NOT_BUTTED = {"butted_spec": None, "other_layer_selection": "not_butted"}
_BAND = EdgeBandSpec(
    context_layers=((30, 0),),
    exclude_layers=(),
    band_width_um=0.429,
    inner_threshold_dbu=20,
)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"check": "enclosing"}, "butted_spec is only valid on 'enclosed' checks"),
        ({"other_layer": None}, "butted_spec requires other_layer"),
        (_NOT_BUTTED, "not_butted requires a derived_layer with intersect_with"),
        (
            {
                **_NOT_BUTTED,
                "derived_layer": DerivedLayer(base=(30, 0), sized_by_um=0.0),
            },
            "not_butted requires a derived_layer with intersect_with",
        ),
        (
            {
                "butted_spec": None,
                "check": "separation",
                "band_spec": _BAND,
                "threshold_dbu": 20,
            },
            "inner_threshold_dbu must be below threshold_dbu",
        ),
        (
            {
                "butted_spec": None,
                "check": "separation",
                "other_layer": None,
                "band_spec": _BAND,
            },
            "band_spec requires other_layer",
        ),
        (
            {"band_spec": dataclasses.replace(_BAND, strict_layers=((40, 0),))},
            "strict_layers is only meaningful on 'separation' checks",
        ),
    ],
)
def test_a_misdeclared_selection_fails_loudly(
    tmp_path, monkeypatch, overrides, message
):
    path = _synthetic(tmp_path, monkeypatch, **overrides)

    with pytest.raises(DrcError, match=message):
        run_drc(path, "synthetic")
