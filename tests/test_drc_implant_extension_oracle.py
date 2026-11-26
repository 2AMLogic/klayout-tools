"""The engine's ``pplus.enclosing.comp.1`` / ``.far.1`` runner against the
pinned foundry ``PP.5b`` / ``PP.5di`` / ``PP.5dii`` expressions (issue #7969).

The expressions are transcribed verbatim from ``rule_decks/pplus.drc`` at
commit ``10ee7fc7`` (:func:`_foundry_pp5`) and run in-process, so the
comparison is of the ``PCOMP`` edge portions each check flags, not merely of
their number. The public-rule fixtures are in
``test_drc_implant_extension.py``.
"""

from __future__ import annotations

import random
from types import SimpleNamespace

import klayout.db as kdb
import pytest

from klayout_tools.decks import get_deck
from klayout_tools.drc_implant_extension import _one_per_portion, run_implant_extension
from test_drc_implant_extension import (
    _BUTTED_NCOMP,
    _WELL_AROUND,
    _WELL_FROM_LEFT,
    _WELL_FROM_RIGHT,
    COMP,
    DWELL,
    FAR,
    LVPWELL,
    NPLUS,
    NWELL,
    PPLUS,
    STRICT,
    _counts,
    _layout,
    _pcomp_tap,
)

#: Contexts the two 0.16 um expressions cover together with the far one.
_WELLS = {
    "no-well": [],
    "inside-nwell": [(NWELL, _WELL_AROUND)],
    "well-from-left": [(NWELL, _WELL_FROM_LEFT)],
    "well-from-right": [(NWELL, _WELL_FROM_RIGHT)],
    "inside-dnwell": [(DWELL, kdb.Box(-3000, -3000, 6000, 5000))],
}


def _loaded(shapes):
    """``(cell, layout)`` for ``shapes``, in the order the checks take them."""
    layout, top = _layout(shapes)
    return top, layout


def _foundry_pp5(cell, layout):
    """The pinned ``PP.5b`` / ``PP.5di`` / ``PP.5dii`` expressions, verbatim:
    the DRC ``e.and(r)`` is ``e & r`` and ``e.not(r)`` is ``e - r``."""

    def region(layer):
        index = layout.find_layer(*layer)
        return (
            kdb.Region(cell.begin_shapes_rec(index)).merged()
            if index is not None
            else kdb.Region()
        )

    def um(nm):
        return round(nm / 1000 / layout.dbu)

    comp, nplus, pplus = region(COMP), region(NPLUS), region(PPLUS)
    nwell, dnwell, lvpwell = region(NWELL), region(DWELL), region(LVPWELL)
    pcomp = comp & pplus
    pplus_edges, nplus_edges = pplus.edges(), nplus.edges()
    slct = pplus_edges - (pplus_edges & (comp & nplus).edges())
    band = nwell.not_interacting(dnwell).sized(um(429))
    pp5b = pcomp.edges().enclosed_check(
        (slct & nwell) + ((slct - lvpwell) & dnwell),
        um(160),
        False,
        kdb.Edges.Projection,
    )
    pp_5d_pcomp = pcomp.outside(dnwell).edges() - nplus_edges
    background = pplus.outside(dnwell).edges()
    pp5di = (pp_5d_pcomp - band).enclosed_check(
        background, um(20), False, kdb.Edges.Projection
    )
    pp5dii = (pp_5d_pcomp & band).enclosed_check(
        background, um(160), False, kdb.Edges.Projection
    )
    return pp5b, pp5di, pp5dii


def _curated_pp5(cell, layout):
    """The two rules' ``EdgePairs``, straight from the engine's runner."""
    setup = SimpleNamespace(layout=layout, dbu_scale=0.001 / layout.dbu)
    deck = {rule.id: rule for rule in get_deck("gf180mcu")}
    comp = kdb.Region(cell.begin_shapes_rec(layout.find_layer(*COMP)))
    pplus = kdb.Region(cell.begin_shapes_rec(layout.find_layer(*PPLUS))).merged()
    return [
        run_implant_extension(cell, setup, deck[rule_id], comp & pplus, pplus)[0]
        for rule_id in (STRICT, FAR)
    ]


def _portions(*edge_pair_sets):
    """Canonical merged first edges: the ``PCOMP`` edge portions flagged."""
    edges = kdb.Edges()
    for edge_pairs in edge_pair_sets:
        for pair in edge_pairs.each():
            edges.insert(
                kdb.Edge(pair.first.x1, pair.first.y1, pair.first.x2, pair.first.y2)
            )
    return sorted(
        tuple(sorted(((e.x1, e.y1), (e.x2, e.y2)))) for e in edges.merged().each()
    )


def _differential_fixtures():
    """Uniform-margin taps in every context the expressions cover together:
    no well, a well around the tap, a well overlapping its left or its right
    part, a ``DNWELL`` around everything, and a butted right edge."""
    fixtures = [
        pytest.param(_pcomp_tap((margin,) * 4, extra), id=f"{well}-{margin}nm")
        for margin in (0, 10, 20, 80, 160, 200)
        for well, extra in _WELLS.items()
    ]
    fixtures += [
        pytest.param(
            _pcomp_tap((margin, margin, 0, margin), [*_WELLS[well], *_BUTTED_NCOMP]),
            id=f"{well}-butted-{margin}nm",
        )
        for margin in (80, 200)
        for well in ("inside-nwell", "inside-dnwell")
    ]
    return fixtures


@pytest.mark.parametrize("shapes", _differential_fixtures())
def test_flagged_portions_match_the_pinned_foundry_expressions(shapes):
    """Where the expressions cover a tap, the rules flag exactly the ``PCOMP``
    edge portions they flag -- ``PP.5b`` ∪ ``PP.5dii`` for the strict rule and
    ``PP.5di`` for the far one -- not merely the same number of them."""
    cell, layout = _loaded(shapes)
    pp5b, pp5di, pp5dii = _foundry_pp5(cell, layout)
    strict, far = _curated_pp5(cell, layout)
    assert _portions(strict) == _portions(pp5b, pp5dii)
    assert _portions(far) == _portions(pp5di)


def test_far_rule_keeps_the_two_gaps_owned_by_issue_7970(tmp_path):
    """Deliberately unchanged here.  The far rule still keeps ``PCOMP`` edges
    that coincide with an ``Nplus`` edge (``PP.5di`` drops them) and still
    measures with the engine's default ``euclidian`` metric (``PP.5di`` is
    ``projection``): a flush butted edge is reported, and a corner distance
    stretches a 10nm finding 17nm into the 20nm portion beside it."""
    flush = _pcomp_tap((200, 200, 0, 200), _BUTTED_NCOMP)
    stepped = [
        (COMP, kdb.Box(0, 0, 2000, 1000)),
        (PPLUS, kdb.Box(-200, -200, 2020, 600)),
        (PPLUS, kdb.Box(-200, 600, 2010, 1200)),
    ]
    for shapes, curated, foundry in (
        (flush, [((2000, 0), (2000, 1000))], []),
        (stepped, [((2000, 583), (2000, 1000))], [((2000, 600), (2000, 1000))]),
    ):
        assert _counts(tmp_path, shapes)[:2] == (0, 1)
        cell, layout = _loaded(shapes)
        assert _portions(_curated_pp5(cell, layout)[1]) == curated
        assert _portions(_foundry_pp5(cell, layout)[1]) == foundry


def _canon(edge):
    """An orientation-free key for an edge: its sorted endpoints."""
    return tuple(sorted(((edge.x1, edge.y1), (edge.x2, edge.y2))))


def test_portion_fusion_agrees_with_klayout_on_random_edge_sets():
    """The line sweep behind "one marker per edge portion" is hand-rolled
    integer geometry, so it is checked against KLayout's own
    ``Edges.merged()`` on random edge sets: the same fused portions, and on
    each the second edge of its longest contributing pair.  A zero-length
    first edge, which ``Edges.enclosed_check`` reports at some corners,
    follows ``Edges.merged()`` too: one on a portion's line joins it, any other
    stands alone."""
    rng = random.Random(7969)
    for _ in range(400):
        pairs = kdb.EdgePairs()
        for tag in range(rng.randrange(1, 12)):
            at, lo = rng.choice((0, 97, 203)), rng.randrange(-300, 300)
            hi = lo + rng.randrange(0, 400)
            first, second = rng.choice(
                (
                    (kdb.Edge(lo, at, hi, at), kdb.Edge(lo, 900 + tag, hi, 900 + tag)),
                    (kdb.Edge(at, lo, at, hi), kdb.Edge(900 + tag, lo, 900 + tag, hi)),
                    (
                        kdb.Edge(lo, lo + at, hi, hi + at),
                        kdb.Edge(lo, lo + 900 + tag, hi, hi + 900 + tag),
                    ),
                )
            )
            pairs.insert(kdb.EdgePair(first, second))
        contributing = [
            (
                kdb.Edge(p.first.x1, p.first.y1, p.first.x2, p.first.y2),
                kdb.Edge(p.second.x1, p.second.y1, p.second.x2, p.second.y2),
            )
            for p in pairs.each()
        ]
        fused = list(_one_per_portion([pairs]).each())
        expected = sorted(
            _canon(e) for e in kdb.Edges([f for f, _ in contributing]).merged().each()
        )
        assert sorted(_canon(pair.first) for pair in fused) == expected
        for pair in fused:
            inside = [
                (f, s)
                for f, s in contributing
                if kdb.Edges([f, pair.first]).merged().count() == 1
            ]
            longest = max(f.length() for f, _ in inside)
            if sum(f.length() == longest for f, _ in inside) == 1:
                tag = next(s for f, s in inside if f.length() == longest)
                assert _canon(pair.second) == _canon(tag)


def test_zero_length_first_edges_follow_klayout_merging():
    """``Edges.enclosed_check`` reports a zero-length first edge at some
    corners.  One on a portion's line joins that portion; one elsewhere, even
    repeated, is a single portion of its own -- ``Edges.merged()``'s reading."""
    pairs = kdb.EdgePairs()
    tag = kdb.Edge(0, 80, 1000, 80)
    firsts = (
        kdb.Edge(0, 0, 1000, 0),
        kdb.Edge(400, 0, 400, 0),
        kdb.Edge(700, 700, 700, 700),
        kdb.Edge(700, 700, 700, 700),
    )
    for first in firsts:
        pairs.insert(kdb.EdgePair(first, tag))
    fused = _one_per_portion([pairs])
    assert sorted(_canon(pair.first) for pair in fused.each()) == [
        ((0, 0), (1000, 0)),
        ((700, 700), (700, 700)),
    ]


def _exact(edge):
    """An edge with its direction kept, unlike :func:`_canon`."""
    return (edge.x1, edge.y1, edge.x2, edge.y2)


def test_fused_portion_is_oriented_as_klayout_orients_its_own_pair():
    """KLayout reports an enclosure pair with its two edges running the same
    way, and every ``enclosed`` marker in the engine is rendered from a pair
    of that shape.  A portion is therefore reported exactly as the expression
    that flags it reports its own pair, on every side of the tap alike; a
    portion fused from several pairs takes the orientation of the pair whose
    second edge it keeps, whichever way round that pair runs."""
    cell, layout = _loaded(_pcomp_tap((80, 80, 80, 80), [(NWELL, _WELL_AROUND)]))
    pp5b, _, pp5dii = _foundry_pp5(cell, layout)
    own = {
        (_exact(pair.first), _exact(pair.second))
        for edge_pairs in (pp5b, pp5dii)
        for pair in edge_pairs.each()
    }
    strict, _far = _curated_pp5(cell, layout)
    fused = [(_exact(pair.first), _exact(pair.second)) for pair in strict.each()]
    assert len(fused) == 4, fused
    stray = [pair for pair in fused if pair not in own]
    assert not stray, stray

    for span in (
        (((0, 1000, 1500, 1000), 1080), ((900, 1000, 2000, 1000), 1180)),
        (((1500, 1000, 0, 1000), 1080), ((2000, 1000, 900, 1000), 1180)),
        (((2000, 1000, 400, 1000), 1180), ((0, 1000, 1500, 1000), 1080)),
    ):
        raw = kdb.EdgePairs()
        for (x1, y1, x2, y2), facing_y in span:
            raw.insert(
                kdb.EdgePair(
                    kdb.Edge(x1, y1, x2, y2), kdb.Edge(x1, facing_y, x2, facing_y)
                )
            )
        (pair,) = list(_one_per_portion([raw]).each())
        (x1, y1, x2, y2), facing_y = max(
            span, key=lambda run: abs(run[0][2] - run[0][0])
        )
        assert _exact(pair.first) == (
            (0, 1000, 2000, 1000) if x2 > x1 else (2000, 1000, 0, 1000)
        )
        assert _exact(pair.second) == (x1, facing_y, x2, facing_y)
