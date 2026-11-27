"""gf180mcu ``nplus.enclosing.comp.1`` against the pinned ``NP.5d`` statements,
side by side: the well-depth split's markers (issue #7955) and the ``Pplus``
edge mask both well-depth statements apply (issue #7987).

Each foundry helper transcribes ``rule_decks/main.drc`` and ``nplus.drc`` at
revision 10ee7fc7 and runs them through the ``klayout.db`` primitives their
DRC uses; each curated helper reads the same markers off ``run_drc``.
Agreement is therefore not built in: the engine selects its operands its own
way (``band_spec``, ``butted_spec``) and reports hull polygons, not edge
pairs.

With a ``Pplus`` drawn the pinned ``NP.5di``/``NP.5dii`` subtract
``pplus.edges`` from the checked NCOMP edges, and ``NP.5dii`` alone from the
enclosing ``Nplus`` edges. Every expected box with multiplicity is those
statements' own, run on the KLayout 0.30.12 binary.

Split out of ``test_drc_edge_band.py`` to respect its per-file size ratchet.
"""

from __future__ import annotations

from collections import Counter

import klayout.db as kdb
import pytest

from golden_deck.manifest import build_layout, write_layout
from klayout_tools.drc import DrcError, get_deck, list_deck_rules, run_drc
from klayout_tools.drc_edge_band import EdgeBandSpec
from test_drc_edge_band import (
    COMP,
    DNWELL,
    NPLUS,
    NWELL,
    PPLUS,
    RULE,
    _banded_rule,
    _markers,
    _run,
    _tap_shapes,
)


def _region(layout, layer):
    """``layer``'s shapes as a region; a layer the fixture never drew is
    empty."""
    index = layout.find_layer(*layer)
    if index is None:
        return kdb.Region()
    return kdb.Region(layout.top_cell().begin_shapes_rec(index))


def _segment(points):
    return tuple(sorted(points))


def _foundry_np5d_pairs(layout):
    """The edge pairs ``NP.5di``/``NP.5dii`` report, transcribed from the
    pinned ``main.drc`` and ``nplus.drc`` -- the deck's own selection (whole
    wells clear of DNWELL, ``not_outside``/``outside``, the band as
    ``nwell_n_dn - sized_in``) and the ``Nplus``-side ``enclosing`` checks.
    Both statements subtract ``pplus.edges`` from the checked NCOMP edges;
    ``NP.5dii`` alone subtracts them from the enclosing ``Nplus`` edges too.
    Unlike the engine's band split this never partitions the tap's edges
    itself, so agreement is not built in."""
    nwell, nplus = _region(layout, NWELL).merged(), _region(layout, NPLUS).merged()
    pplus_edges = _region(layout, PPLUS).merged().edges()
    dnwell = _region(layout, DNWELL)
    nwell_n_dn = nwell.not_interacting(dnwell)
    sized_in = nwell_n_dn.sized(-429)
    background = (nplus - dnwell).not_outside(nwell).edges()
    foreground = (_region(layout, COMP) & nplus).not_outside(nwell).outside(
        dnwell
    ).edges() - pplus_edges
    pairs = kdb.EdgePairs()
    for implant, edges, distance, metrics in (
        (background, nwell_n_dn - sized_in, 160, kdb.Region.Projection),  # NP.5di
        (background - pplus_edges, sized_in, 20, kdb.Region.Euclidian),  # NP.5dii
    ):
        pairs.insert(
            implant.enclosing_check(foreground & edges, distance, metrics=metrics)
        )
    return pairs


def _foundry_np5d_segments(layout):
    """The NCOMP-side stretches of :func:`_foundry_np5d_pairs`."""
    return {
        _segment([(pair.second.x1, pair.second.y1), (pair.second.x2, pair.second.y2)])
        for pair in _foundry_np5d_pairs(layout).each()
    }


def _foundry_np5d_boxes(layout):
    """Each :func:`_foundry_np5d_pairs` pair's bounding box, with multiplicity."""
    boxes = Counter()
    for pair in _foundry_np5d_pairs(layout).each():
        box = pair.bbox()
        boxes[(box.left, box.bottom, box.right, box.top)] += 1
    return boxes


def _marker_boxes(report, rule=RULE):
    """``Counter`` of each marker's bounding box, with multiplicity."""
    boxes = Counter()
    for violation in _markers(report, rule):
        bbox = violation["bbox"]
        boxes[(bbox["left"], bbox["bottom"], bbox["right"], bbox["top"])] += 1
    return boxes


#: Taps wholly inside a pure Nwell (no DNWELL), by (Comp box, Nplus margin,
#: Nwell box): uniform and lopsided wells, a well only 390 nm deeper than the
#: tap (band corners on the side edges), one narrower than the tap is long,
#: and margins either side of both limits. For these the foundry deck judges
#: every edge by ``NP.5di``/``NP.5dii`` alone.
_INSIDE_PURE_WELL = [
    ((0, 0, 1000, 1000), 80, (-1000, -1000, 2000, 2000)),
    ((0, 0, 1000, 1000), 80, (-250, -250, 1250, 1250)),
    ((0, 0, 1000, 1000), 10, (-1000, -1000, 2000, 2000)),
    ((0, 0, 1000, 1000), 80, (-250, -1000, 2000, 2000)),
    ((0, 0, 1000, 1000), 80, (-400, -390, 1400, 1390)),
    ((0, 0, 1000, 1000), 100, (-2000, -5000, 5000, 5000)),
    ((0, 0, 1000, 1000), 15, (-1000, -1000, 2000, 2000)),
    ((0, 0, 2600, 700), 120, (-130, -1000, 2730, 1700)),
]


def test_foundry_np5d_excludes_the_whole_well_on_distant_dnwell_overlap():
    shapes = _tap_shapes(10, 1000)
    assert len(_foundry_np5d_segments(build_layout(shapes))) == 4
    shapes.append({"layer": list(DNWELL), "box": [1900, -1000, 2500, 2000]})
    assert _foundry_np5d_segments(build_layout(shapes)) == set()


def _curated_ncomp_segments(report, layout):
    """Each marker's own NCOMP-side stretch: the two hull points on the NCOMP
    boundary (the other two lie on ``Nplus``, a margin away)."""
    boundary = list(
        ((_region(layout, COMP) & _region(layout, NPLUS)).merged()).edges().each()
    )
    found = set()
    for violation in _markers(report):
        own = [
            tuple(point)
            for point in violation["polygon"]
            if any(edge.contains(kdb.Point(*point)) for edge in boundary)
        ]
        assert len(own) == 2, violation["polygon"]
        found.add(_segment(own))
    return found


@pytest.mark.parametrize(("comp", "margin", "nwell_box"), _INSIDE_PURE_WELL)
def test_markers_match_the_foundry_np5d_expressions(tmp_path, comp, margin, nwell_box):
    """Differential check against the foundry deck: for a tap wholly inside a
    pure well, the stretches of the tap's perimeter this rule reports are
    exactly ``NP.5di``'s and ``NP.5dii``'s -- not merely the same count."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    for layer, box in (
        (COMP, kdb.Box(*comp)),
        (
            NPLUS,
            kdb.Box(
                comp[0] - margin, comp[1] - margin, comp[2] + margin, comp[3] + margin
            ),
        ),
        (NWELL, kdb.Box(*nwell_box)),
    ):
        top.shapes(layout.layer(*layer)).insert(box)
    path = tmp_path / "pure_well.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu", rules=[RULE])

    assert _curated_ncomp_segments(report, layout) == _foundry_np5d_segments(layout)


# --------------------------------------------------------------------------- #
# The Pplus edge mask (issue #7987)
# --------------------------------------------------------------------------- #

#: The issue's Nwell: every tap below sits deep inside it unless a case
#: replaces it with a narrower well to put the checked edge in the band.
_FILED_NWELL = {"layer": list(NWELL), "box": [-1000, -1000, 6000, 3000]}

#: The issue's reproducer: an NCOMP butted along its whole right edge to a
#: PCOMP, each implant running past the ``Comp`` above and below.
_BUTTED_EDGE = [
    {"layer": list(COMP), "box": [0, 0, 4000, 2000]},
    {"layer": list(NPLUS), "box": [-160, -160, 2000, 2160]},
    {"layer": list(PPLUS), "box": [2000, -160, 4160, 2160]},
]

#: Only the enclosing edge coincides with ``Pplus``: the checked NCOMP edge
#: is 10 nm inside the ``Nplus`` edge that a ``Pplus`` shares.
_ENCLOSING_EDGE = [
    {"layer": list(COMP), "box": [0, 0, 1000, 1000]},
    {"layer": list(NPLUS), "box": [-160, -160, 1010, 1160]},
    {"layer": list(PPLUS), "box": [1010, -160, 1500, 1160]},
]


def _nwell(right):
    return {"layer": list(NWELL), "box": [-1000, -1000, right, 3000]}


def _partly(pplus_box):
    """``Pplus`` over the middle of the coincident stretch only."""
    return {"layer": list(PPLUS), "box": pplus_box}


def _far_pplus():
    """A ``Pplus`` coinciding with no ``Nplus`` edge: it masks nothing."""
    return {"layer": list(PPLUS), "box": [5000, 5000, 6000, 6000]}


#: An NCOMP edge coincident with a ``Pplus`` edge inside an Nwell, by shapes
#: and the marker boxes with multiplicity the pinned statements report. A
#: butted NCOMP edge is dropped in both classes; an NCOMP edge that merely
#: *faces* a butted ``Nplus`` edge survives the band (``NP.5di`` keeps its
#: operand) and is dropped in the interior.
_PPLUS_CASES = [
    pytest.param(_BUTTED_EDGE + [_FILED_NWELL], {}, id="butted_edge_deep"),
    pytest.param(_BUTTED_EDGE + [_nwell(2200)], {}, id="butted_edge_band"),
    pytest.param(_ENCLOSING_EDGE + [_FILED_NWELL], {}, id="enclosing_edge_deep"),
    pytest.param(
        _ENCLOSING_EDGE + [_nwell(1200)],
        Counter({(1000, 0, 1010, 1000): 1}),
        id="enclosing_edge_band",
    ),
    pytest.param(
        _BUTTED_EDGE[:2] + [_partly([2000, 500, 4160, 1500]), _FILED_NWELL],
        Counter({(2000, -20, 2000, 500): 1, (2000, 1500, 2000, 2020): 1}),
        id="partly_coincident_edge_deep",
    ),
    pytest.param(
        _BUTTED_EDGE[:2] + [_partly([2000, 500, 4160, 1500]), _nwell(2200)],
        Counter({(2000, 0, 2000, 500): 1, (2000, 1500, 2000, 2000): 1}),
        id="partly_coincident_edge_band",
    ),
    pytest.param(
        _ENCLOSING_EDGE[:2] + [_partly([1010, 300, 1500, 700]), _FILED_NWELL],
        Counter({(1000, -17, 1010, 317): 1, (1000, 683, 1010, 1017): 1}),
        id="partly_coincident_enclosing_edge_deep",
    ),
    pytest.param(
        _ENCLOSING_EDGE[:2] + [_partly([1010, 300, 1500, 700]), _nwell(1200)],
        Counter({(1000, 0, 1010, 1000): 1}),
        id="partly_coincident_enclosing_edge_band",
    ),
    # A Pplus that coincides with nothing leaves every limit where it was:
    # 10 nm deep inside is under NP.5dii's 0.02 um, 80 nm in the band under
    # NP.5di's 0.16 um.
    pytest.param(
        _tap_shapes(10, 1000) + [_far_pplus()],
        Counter(
            {
                (-10, -10, 0, 1010): 1,
                (-10, -10, 1010, 0): 1,
                (-10, 1000, 1010, 1010): 1,
                (1000, -10, 1010, 1010): 1,
            }
        ),
        id="unrelated_pplus_short_extension_deep",
    ),
    pytest.param(
        _tap_shapes(80, 250) + [_far_pplus()],
        Counter(
            {
                (-80, 0, 0, 1000): 1,
                (0, -80, 1000, 0): 1,
                (0, 1000, 1000, 1080): 1,
                (1000, 0, 1080, 1000): 1,
            }
        ),
        id="unrelated_pplus_short_extension_band",
    ),
    # The no-Nwell control (issue #7970): NP.5b drops the butted Nplus edge.
    pytest.param(_BUTTED_EDGE, {}, id="no_nwell"),
]


@pytest.mark.parametrize(("shapes", "expected"), _PPLUS_CASES)
def test_pplus_coincident_edges_are_not_measured_in_the_well(
    tmp_path, shapes, expected
):
    """The issue's fixtures: an NCOMP edge coincident with a ``Pplus`` edge
    is not measured in either well-depth class, and the rest of the edge
    is, at the pinned statements' own bounds."""
    report = _run(tmp_path, shapes)

    assert _marker_boxes(report) == expected, report["violations"]
    assert RULE in report["coverage"]["rules_checked"]
    assert RULE not in report["coverage"]["rules_skipped"]


@pytest.mark.parametrize(("shapes", "_expected"), _PPLUS_CASES)
def test_pplus_markers_match_the_foundry_np5d_expressions(tmp_path, shapes, _expected):
    """Differential check against the foundry deck with a ``Pplus`` drawn:
    the markers this rule reports have exactly the bounds and multiplicity
    of ``NP.5di``'s and ``NP.5dii``'s edge pairs, partly coincident edges
    included."""
    path = write_layout(shapes, tmp_path / "pplus_well.gds")

    report = run_drc(path, "gf180mcu", rules=[RULE])

    assert _marker_boxes(report) == _foundry_np5d_boxes(build_layout(shapes))


def test_mask_layers_on_a_separation_rule_fails_loudly(tmp_path, monkeypatch):
    """The mask is defined on the ``enclosed`` check's two edge operands. A
    ``separation`` rule declaring one is a deck-authoring mistake and is
    rejected like ``strict_layers`` on an ``enclosed`` rule, layers absent
    or not."""
    path = _banded_rule(
        tmp_path,
        monkeypatch,
        check="separation",
        band_spec=EdgeBandSpec(
            context_layers=((30, 0),),
            exclude_layers=(),
            band_width_um=0.429,
            inner_threshold_dbu=20,
            mask_layers=((40, 0),),
        ),
    )

    with pytest.raises(DrcError, match="mask_layers is only meaningful on 'enclosed'"):
        run_drc(path, "synthetic")


def test_list_rules_adds_mask_layers_only_where_declared():
    """``mask_layers`` is additive like ``strict_layers``: a band that
    declares a mask grows the key, a band without one does not."""
    deck = get_deck("gf180mcu")
    entries = {entry["id"]: entry for entry in list_deck_rules("gf180mcu")["rules"]}
    masked = {rule.id for rule in deck if getattr(rule.band_spec, "mask_layers", ())}
    banded = {rule.id for rule in deck if rule.band_spec is not None}

    assert RULE in masked
    assert banded - masked
    for rule_id in banded:
        assert ("mask_layers" in entries[rule_id]["edge_band"]) == (rule_id in masked)
