"""``pplus.enclosing.comp.1`` / ``.far.1`` select the ``PCOMP`` edge portions
the foundry ``PP.5b`` / ``PP.5di`` / ``PP.5dii`` expressions select (issue
#7969), through the public ``run_drc``.

The engine-level comparison against the pinned expressions transcribed from
``rule_decks/pplus.drc`` at commit ``10ee7fc7`` is in
``test_drc_implant_extension_oracle.py``.
"""

from __future__ import annotations

import dataclasses
import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.decks import get_deck
from klayout_tools.drc import DrcError, run_drc
from klayout_tools.drc_implant_extension import (
    ImplantExtensionSpec,
    validate_implant_extension,
)

STRICT = "pplus.enclosing.comp.1"
FAR = "pplus.enclosing.comp.far.1"
NWELL, DWELL, COMP, PPLUS, NPLUS, LVPWELL = (
    (21, 0),
    (12, 0),
    (22, 0),
    (31, 0),
    (32, 0),
    (204, 0),
)

_WELL_FROM_LEFT = kdb.Box(-200, -2000, 1000, 3000)
_WELL_FROM_RIGHT = kdb.Box(2200, -2000, 6000, 3000)
_WELL_AROUND = kdb.Box(-1000, -1000, 4000, 2000)
_EVEN, _SHORT_RIGHT = (200, 200, 200, 200), (200, 200, 80, 200)
_BUTTED_NCOMP = [
    (COMP, kdb.Box(2000, 0, 3000, 1000)),
    (NPLUS, kdb.Box(2000, 0, 3400, 1200)),
]

#: The ledger's seven fixtures, each with the foundry expressions' own
#: ``(PP.5b + PP.5dii, PP.5di)`` marker count. Main at ``25da22a11`` reported
#: ``1/0`` on every row but the third and the seventh.
_SEVEN_FIXTURES = [
    pytest.param(
        _SHORT_RIGHT, [(NWELL, _WELL_FROM_LEFT)], (0, 0), id="80nm-well-200nm-from-left"
    ),
    pytest.param(
        _SHORT_RIGHT,
        [(NWELL, _WELL_FROM_RIGHT)],
        (1, 0),
        id="80nm-well-200nm-from-right",
    ),
    pytest.param(
        _SHORT_RIGHT,
        [(NWELL, kdb.Box(-4800, -2000, -800, 3000))],
        (0, 0),
        id="80nm-well-800nm-from-left",
    ),
    pytest.param(
        (200, 200, 10, 200),
        [(NWELL, _WELL_FROM_LEFT)],
        (0, 1),
        id="10nm-well-200nm-from-left",
    ),
    pytest.param(
        (200, 200, 0, 200),
        [(NWELL, _WELL_AROUND), *_BUTTED_NCOMP],
        (0, 0),
        id="inside-nwell-right-edge-butted",
    ),
    pytest.param(
        _SHORT_RIGHT, [(NWELL, _WELL_AROUND)], (1, 0), id="inside-nwell-unbutted-80nm"
    ),
    pytest.param(
        _EVEN, [(NWELL, _WELL_AROUND)], (0, 0), id="inside-nwell-unbutted-200nm"
    ),
]


def _layout(shapes, dbu=0.001):
    """A one-cell layout holding ``[(layer, Box), ...]``, every box in nm."""
    layout = kdb.Layout()
    layout.dbu = dbu
    top = layout.create_cell("TOP")
    for layer, box in shapes:
        scaled = [
            round(nm * 0.001 / dbu) for nm in (box.left, box.bottom, box.right, box.top)
        ]
        top.shapes(layout.layer(*layer)).insert(kdb.Box(*scaled))
    return layout, top


def _pcomp_tap(margins, extra=()):
    """The issue's 2 x 1 um ``PCOMP`` rectangle. ``margins`` are the (left,
    bottom, right, top) nm of ``Pplus`` beyond it; ``extra`` adds context."""
    left, bottom, right, top = margins
    return [
        (COMP, kdb.Box(0, 0, 2000, 1000)),
        (PPLUS, kdb.Box(-left, -bottom, 2000 + right, 1000 + top)),
        *extra,
    ]


def _square_tap(margin, extra=()):
    """A 1 x 4 um ``PCOMP`` with ``Pplus`` ``margin`` nm beyond it."""
    return [
        (COMP, kdb.Box(0, 0, 1000, 4000)),
        (PPLUS, kdb.Box(-margin, -margin, 1000 + margin, 4000 + margin)),
        *extra,
    ]


def _counts(tmp_path, shapes, dbu=0.001):
    """``(strict, far, report)``: the two public rules' marker counts."""
    layout, _ = _layout(shapes, dbu)
    path = tmp_path / "tap.gds"
    layout.write(str(path))
    report = run_drc(str(path), "gf180mcu", rules=[STRICT, FAR])
    counts = report["rule_counts"]
    return counts.get(STRICT, 0), counts.get(FAR, 0), report


def _boxes(report, rule_id):
    return [v["bbox"] for v in report["violations"] if v["rule"] == rule_id]


@pytest.mark.parametrize(("margins", "extra", "expected"), _SEVEN_FIXTURES)
def test_foundry_context_selects_the_limit_per_edge_portion(
    tmp_path, margins, extra, expected
):
    """The ledger's seven fixtures. Only the edge portion facing the nearby
    well is held to 0.16um; the rest of the same tap is the far rule's 0.02um,
    and a ``Pplus`` edge flush with an ``NCOMP`` edge is not measured."""
    assert _counts(tmp_path, _pcomp_tap(margins, extra))[:2] == expected


def test_markers_sit_on_the_edge_portion_they_flag(tmp_path):
    """Rule id and marker location on a mixed tap. A strict marker is the
    flagged edge's own quad, never the whole tap both rules used to report,
    and the far rule's marker is the 10nm edge, not the 200nm ones."""
    right = dict(left=2000, bottom=0, right=2080, top=1000)
    near, _, report = _counts(
        tmp_path, _pcomp_tap(_SHORT_RIGHT, [(NWELL, _WELL_FROM_RIGHT)])
    )
    deep, _, inside = _counts(
        tmp_path, _pcomp_tap(_SHORT_RIGHT, [(NWELL, _WELL_AROUND)])
    )
    assert near == deep == 1
    assert _boxes(report, STRICT) == [right] and _boxes(inside, STRICT) == [right]
    _, far, report = _counts(
        tmp_path, _pcomp_tap((200, 200, 10, 200), [(NWELL, _WELL_FROM_LEFT)])
    )
    (box,) = _boxes(report, FAR)
    assert far == 1 and not _boxes(report, STRICT)
    assert (box["left"], box["right"]) == (2000, 2010)
    assert box["bottom"] <= 0 and box["top"] >= 1000


def test_markers_span_one_edge_each_on_every_side_of_the_tap(tmp_path):
    """A strict marker's ``bbox`` and ``polygon`` span the flagged ``PCOMP``
    edge portion and the ``Pplus`` edge facing it, on all four sides alike:
    a uniformly short tap gives four markers, each the quad of one edge."""
    _, _, report = _counts(
        tmp_path, _pcomp_tap((80, 80, 80, 80), [(NWELL, _WELL_AROUND)])
    )
    quads = [
        (-80, 0, 0, 1000),
        (0, -80, 2000, 0),
        (0, 1000, 2000, 1080),
        (2000, 0, 2080, 1000),
    ]
    assert (
        sorted(
            (b["left"], b["bottom"], b["right"], b["top"])
            for b in _boxes(report, STRICT)
        )
        == quads
    )
    assert sorted(
        tuple(sorted(map(tuple, v["polygon"]))) for v in report["violations"]
    ) == sorted(
        tuple(sorted([(left, bottom), (left, top), (right, bottom), (right, top)]))
        for left, bottom, right, top in quads
    )


def test_partially_butted_edge_reports_only_the_unbutted_portion(tmp_path):
    """An ``NCOMP`` butting the lower 600nm of the right edge: its ``Pplus``
    edge is ``NCOMP``-coincident and dropped, while the upper 400nm has a
    genuine 80nm shortfall.  One marker, on the unbutted portion only -- a
    whole-edge exemption reports nothing, and the ``euclidian`` metric adds a
    second, corner-distance marker on the butted portion."""
    shapes = [
        (PPLUS, kdb.Box(2000, 600, 2080, 1200)),
        (NPLUS, kdb.Box(2000, 0, 3400, 700)),
        (NWELL, _WELL_AROUND),
        *_pcomp_tap((200, 200, 0, 200)),
        (COMP, kdb.Box(2000, 0, 3000, 600)),
    ]
    strict, far, report = _counts(tmp_path, shapes)
    assert (strict, far) == (1, 0)
    assert _boxes(report, STRICT) == [dict(left=2000, bottom=600, right=2080, top=1000)]


def test_nplus_coincident_edge_inside_the_band_is_still_measured_strictly(tmp_path):
    """The in-band portion neither transcribed expression measures: a
    ``PCOMP`` edge that coincides with an ``Nplus`` edge while the tap is
    outside ``Nwell``.  ``PP.5dii`` drops it and ``PP.5b``'s background holds
    no ``Pplus`` inside ``Nwell``, so the pinned deck reports nothing here.
    The rule keeps its previous flat measurement for it rather than leaving
    it unchecked, as the far rule does for the same edges (issue #7970)."""
    strict, far, report = _counts(
        tmp_path,
        _pcomp_tap((200, 200, 0, 200), [(NWELL, _WELL_FROM_RIGHT), *_BUTTED_NCOMP]),
    )
    assert strict >= 1 and far == 0
    assert all(box["left"] == box["right"] == 2000 for box in _boxes(report, STRICT))


@pytest.mark.parametrize(
    ("gap_dbu", "expected"),
    [(428, (3, 3)), (429, (1, 3)), (430, (0, 4)), (431, (0, 4))],
)
def test_edge_exactly_at_the_band_width_is_strict(tmp_path, gap_dbu, expected):
    """A 10nm tap with its ``Nwell`` ``gap`` nm from the right edge. The pinned
    expressions give ``PP.5dii`` / ``PP.5di`` = 3/3, 1/3, 0/4, 0/4."""
    left = 400 + gap_dbu
    shapes = [
        (COMP, kdb.Box(0, 0, 400, 1000)),
        (PPLUS, kdb.Box(-10, -10, 410, 1010)),
        (NWELL, kdb.Box(left, -2000, left + 4000, 3000)),
    ]
    assert _counts(tmp_path, shapes)[:2] == expected


def test_no_well_skips_the_strict_rule_and_leaves_the_far_limit(tmp_path):
    """Well-free coverage is unchanged (issue #2634): the strict rule is
    inapplicable and the tap is held only to the far rule's 0.02um."""
    for margin, far in ((10, 4), (20, 0)):
        strict, far_count, report = _counts(tmp_path, _square_tap(margin))
        assert (strict, far_count) == (0, far)
        assert STRICT in report["coverage"]["rules_skipped"]
        assert FAR in report["coverage"]["rules_checked"]


def test_dnwell_contexts_keep_the_strict_limit(tmp_path):
    """``PP.5b``'s second branch -- inside ``DNWELL`` and outside ``LVPWELL``
    -- keeps 0.16um, and so do the two contexts no transcribed expression
    covers: inside both ``DNWELL`` and ``LVPWELL`` (``PP.5ci``/``PP.5cii``)
    and a ``PCOMP`` straddling a ``DNWELL`` boundary.  A butted edge inside
    ``DNWELL`` passes there exactly as it does inside ``Nwell``."""
    around = kdb.Box(-3000, -3000, 3000, 8000)
    contexts = [
        [(DWELL, around)],
        [(DWELL, around), (LVPWELL, around)],
        [(DWELL, kdb.Box(500, -3000, 3000, 8000))],
    ]
    for extra in contexts:
        assert _counts(tmp_path, _square_tap(80, extra))[:2] == (4, 0)
        assert _counts(tmp_path, _square_tap(160, extra))[:2] == (0, 0)
    butted = _pcomp_tap((200, 200, 0, 200), [(DWELL, around), *_BUTTED_NCOMP])
    assert _counts(tmp_path, butted)[:2] == (0, 0)


@pytest.mark.parametrize("dbu", [0.001, 0.005, 0.01])
def test_results_do_not_depend_on_the_database_unit(tmp_path, dbu):
    """The same three mixed taps, drawn at the same physical size, give the
    same counts at a 1, 5 and 10nm dbu."""
    taps = [
        (_SHORT_RIGHT, [(NWELL, _WELL_FROM_RIGHT)], (1, 0)),
        ((200, 200, 10, 200), [(NWELL, _WELL_FROM_LEFT)], (0, 1)),
        (_SHORT_RIGHT, [(NWELL, _WELL_FROM_LEFT)], (0, 0)),
    ]
    for margins, extra, expected in taps:
        shapes = _pcomp_tap(margins, extra)
        assert _counts(tmp_path, shapes, dbu)[:2] == expected


def test_both_rules_declare_their_edge_selection():
    """Structural pin: ids, check kind, thresholds and provenance are the
    public contract; the split now lives in the rule's spec, not in a
    whole-polygon ``DerivedLayer.context``."""
    deck = {rule.id: rule for rule in get_deck("gf180mcu")}
    strict, far = deck[STRICT], deck[FAR]
    for rule in (strict, far):
        spec = rule.extension_spec
        assert rule.check == "enclosed" and rule.other_layer == PPLUS
        assert rule.derived_layer.context is None
        assert (spec.nwell, spec.dnwell, spec.band_um) == (NWELL, DWELL, 0.429)
    near, far_spec = strict.extension_spec, far.extension_spec
    assert (strict.threshold_dbu, near.selection) == (160, "near")
    assert (near.comp, near.nplus, near.lvpwell) == (COMP, NPLUS, LVPWELL)
    assert (far.threshold_dbu, far_spec.selection) == (20, "far")
    assert (far_spec.comp, far_spec.nplus, far_spec.lvpwell) == (None,) * 3
    assert strict.provenance.rule_id == "PP.5b" and far.provenance.rule_id == "PP.5di"
    assert (
        strict.requires_any_layer == (NWELL, DWELL) and far.requires_any_layer is None
    )


def test_public_cli_selects_the_limit_per_edge(capsys, tmp_path):
    """The ledger's reproducer through the public ``klt drc``: the 80nm edge
    facing the well is a violation and the same edge away from it is not."""
    for well, strict in ((_WELL_FROM_LEFT, 0), (_WELL_FROM_RIGHT, 1)):
        path = tmp_path / "tap.gds"
        _layout(_pcomp_tap(_SHORT_RIGHT, [(NWELL, well)]))[0].write(str(path))
        capsys.readouterr()
        code = main(
            [
                "drc",
                str(path),
                "--deck",
                "gf180mcu",
                "--rules",
                STRICT,
                "--format",
                "json",
            ]
        )
        counts = json.loads(capsys.readouterr().out)["rule_counts"]
        assert counts.get(STRICT, 0) == strict and code == (3 if strict else 0)


def test_extension_spec_rejects_a_rule_it_cannot_apply():
    """Same "fail loudly with the rule id" contract as the engine's other
    spec validators: an unusable declaration is a ``DrcError`` whatever the
    stream holds, and a bad selection fails at construction."""
    strict = next(rule for rule in get_deck("gf180mcu") if rule.id == STRICT)

    def declaration(**changes):
        return dataclasses.replace(strict, **changes)

    for changes, message in (
        ({"check": "enclosing"}, "enclosed"),
        ({"other_layer": None}, "other_layer"),
        ({"require_containment": True}, "require_containment"),
    ):
        with pytest.raises(DrcError, match=message):
            validate_implant_extension(declaration(**changes))
    validate_implant_extension(declaration())
    with pytest.raises(ValueError, match="selection"):
        ImplantExtensionSpec(NWELL, DWELL, 0.429, "middle")
    with pytest.raises(ValueError, match="requires comp, nplus and lvpwell"):
        ImplantExtensionSpec(NWELL, DWELL, 0.429, "near", comp=COMP)
