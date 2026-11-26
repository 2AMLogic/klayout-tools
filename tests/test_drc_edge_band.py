"""gf180mcu ``nplus.enclosing.comp.1`` selects the foundry deck's near/far
limit per edge (issue #7955).

The foundry deck (``rule_decks/nplus.drc``) measures an NCOMP tap's Nplus
extension two ways, picked per *edge* by its distance from the boundary of a
pure Nwell: ``NP.5di`` needs 0.16 um within 0.429 um of the boundary,
``NP.5dii`` needs 0.02 um deeper inside. The curated rule used to apply the
flat 0.16 um, so an 80 nm extension deep inside a large Nwell -- legal --
was reported on all four edges. The fixtures here are the issue's: a 1 um
``Comp`` square at the origin under ``Nplus``, inside an ``Nwell`` whose
margins from the square select the class of each edge. Counts were fixed by
running the pinned foundry expressions on the same geometry.

Also pins the ``DrcRule.band_spec`` contract itself (descriptor validation,
``--list-rules`` JSON) and that the ``PCOMP`` mirror is untouched.
"""

from __future__ import annotations

import pytest

from golden_deck.manifest import write_layout
from klayout_tools.decks import DrcRule, get_deck
from klayout_tools.drc import DrcError, list_deck_rules, run_drc
from klayout_tools.drc_edge_band import EdgeBandSpec

RULE = "nplus.enclosing.comp.1"
COMP, NWELL, DNWELL, PPLUS, NPLUS = (22, 0), (21, 0), (12, 0), (31, 0), (32, 0)
PCOMP_RULES = ["pplus.enclosing.comp.1", "pplus.enclosing.comp.far.1"]

#: The issue's four rows: Nplus margin, Nwell margin, markers. The middle
#: two are positive controls -- the relaxation must not swallow them.
ROWS = [
    (80, 1000, 0),  # deep inside a pure Nwell: NP.5dii's 0.02 um is met
    (80, 250, 4),  # within 0.429 um of the boundary: NP.5di's 0.16 um
    (10, 1000, 4),  # deep inside, but under NP.5dii's own 0.02 um
    (200, 1000, 0),
]


def _tap_shapes(nplus_margin, nwell_margins, implant=NPLUS):
    """A 1 um ``Comp`` square at the origin under ``implant``, which extends
    ``nplus_margin`` beyond it on every side, inside an ``Nwell`` whose own
    margin from the square is ``nwell_margins`` = ``(left, bottom, right,
    top)`` (a single value is used for all four sides; ``None`` draws no
    well)."""
    m = nplus_margin
    shapes = [
        {"layer": list(COMP), "box": [0, 0, 1000, 1000]},
        {"layer": list(implant), "box": [-m, -m, 1000 + m, 1000 + m]},
    ]
    if nwell_margins is not None:
        left, bottom, right, top = (
            (nwell_margins,) * 4 if isinstance(nwell_margins, int) else nwell_margins
        )
        shapes.append(
            {
                "layer": list(NWELL),
                "box": [-left, -bottom, 1000 + right, 1000 + top],
            }
        )
    return shapes


def _run(tmp_path, shapes, rules=(RULE,), name="tap.gds", dbu=0.001):
    if dbu == 0.001:
        path = write_layout(shapes, tmp_path / name)
    else:
        path = _write_at_dbu(shapes, tmp_path / name, dbu)
    return run_drc(path, "gf180mcu", rules=list(rules))


def _write_at_dbu(shapes, path, dbu):
    """``write_layout`` for another database unit: every box is rescaled from
    the 1 nm units the fixtures are authored in."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.dbu = dbu
    top = layout.create_cell("TOP")
    scale = dbu / 0.001
    for shape in shapes:
        x0, y0, x1, y1 = (round(value / scale) for value in shape["box"])
        layer_num, datatype = shape["layer"]
        top.shapes(layout.layer(layer_num, datatype)).insert(kdb.Box(x0, y0, x1, y1))
    layout.write(str(path))
    return str(path)


def _markers(report, rule=RULE):
    return [v for v in report["violations"] if v["rule"] == rule]


def _tap_perimeter_segments(violations):
    """For each marker, the two endpoints of its edge-pair hull that lie on
    the ``Comp`` square's own boundary -- i.e. the stretch of the tap's
    perimeter the marker is about, without the implant-side points."""
    segments = []
    for violation in violations:
        on_perimeter = sorted(tuple(point) for point in _on_square(violation))
        assert len(on_perimeter) == 2, violation["polygon"]
        segments.append(tuple(on_perimeter))
    return sorted(segments)


def _on_square(violation):
    for x, y in violation["polygon"]:
        inside = 0 <= x <= 1000 and 0 <= y <= 1000
        if inside and (x in (0, 1000) or y in (0, 1000)):
            yield (x, y)


def _marker_count(report, rule=RULE):
    return report["rule_counts"].get(rule, 0)


@pytest.mark.parametrize(("nplus_margin", "nwell_margin", "expected"), ROWS)
def test_foundry_near_far_limit_is_selected_per_edge(
    tmp_path, nplus_margin, nwell_margin, expected
):
    """The issue's four rows, on the 1 nm grid the issue specifies."""
    report = _run(tmp_path, _tap_shapes(nplus_margin, nwell_margin))

    assert _marker_count(report) == expected, report["violations"]
    # The rule really was evaluated -- these are not vacuous passes.
    assert RULE in report["coverage"]["rules_checked"]


@pytest.mark.parametrize("dbu", [0.005, 0.01])
@pytest.mark.parametrize(("nplus_margin", "nwell_margin", "expected"), ROWS)
def test_near_far_limit_is_independent_of_the_database_unit(
    tmp_path, nplus_margin, nwell_margin, expected, dbu
):
    """Band width and both thresholds are physical distances: the same
    geometry written on a coarser grid selects the same class per edge."""
    report = _run(
        tmp_path,
        _tap_shapes(nplus_margin, nwell_margin),
        name=f"tap_{dbu}.gds",
        dbu=dbu,
    )

    assert _marker_count(report) == expected, report["violations"]


@pytest.mark.parametrize(
    ("nplus_margin", "nwell_margin", "expected"),
    [
        (20, 1000, 0),  # NP.5dii, exactly at the limit
        (19, 1000, 4),  # just under NP.5dii
        (160, 250, 0),  # NP.5di, exactly at the limit
        (159, 250, 4),  # just under NP.5di
    ],
)
def test_both_limits_pass_at_equality(tmp_path, nplus_margin, nwell_margin, expected):
    report = _run(tmp_path, _tap_shapes(nplus_margin, nwell_margin))

    assert _marker_count(report) == expected, report["violations"]


@pytest.mark.parametrize(("nwell_margin", "expected"), [(428, 4), (429, 4), (430, 0)])
def test_edge_exactly_at_the_band_width_is_in_the_strict_band(
    tmp_path, nwell_margin, expected
):
    """The pinned expressions put an edge exactly 0.429 um from the boundary
    in the outer band (``nwell_n_dn - nwell_n_dn.sized(-0.429.um)`` keeps it,
    ``sized(-0.429.um)`` does not), so it is held to 0.16 um; one grid step
    deeper is interior."""
    report = _run(tmp_path, _tap_shapes(80, nwell_margin))

    assert _marker_count(report) == expected, report["violations"]


def test_asymmetric_tap_reports_only_the_near_band(tmp_path):
    """One tap spanning both classes: 250 nm of Nwell on the left (band) and
    1000 nm on the other three sides (interior). Only the left edge and the
    leftmost 179 nm of the top and bottom edges -- the stretch of them
    inside the band -- are reported, at their positions on the tap; the rest
    of the top and bottom edges and the whole right edge are clean."""
    report = _run(tmp_path, _tap_shapes(80, (250, 1000, 1000, 1000)))

    assert _tap_perimeter_segments(_markers(report)) == [
        ((0, 0), (0, 1000)),  # the full left edge
        ((0, 0), (179, 0)),  # the leftmost 179 nm of the bottom edge
        ((0, 1000), (179, 1000)),  # ... and of the top edge
    ]
    assert _marker_count(report) == 3


@pytest.mark.parametrize("nplus_margin", [80, 160])
def test_no_well_keeps_the_strict_extension(tmp_path, nplus_margin):
    """The relaxation is applied only where the context is positively
    identified: with no ``Nwell`` drawn the flat 0.16 um stands (80 nm
    violates, exactly 160 nm passes). The rule is not skipped, either."""
    report = _run(tmp_path, _tap_shapes(nplus_margin, None))

    assert _marker_count(report) == (4 if nplus_margin == 80 else 0)
    assert RULE in report["coverage"]["rules_checked"]
    assert RULE not in report["coverage"]["rules_skipped"]


@pytest.mark.parametrize(
    "dnwell_box",
    [
        [-500, -500, 1500, 1500],  # covers the tap
        [900, 900, 1500, 1500],  # clips one corner of the tap
        [1500, -1000, 2000, 2000],  # overlaps the Nwell, clear of the tap
    ],
)
def test_nwell_touching_dnwell_is_not_relaxed(tmp_path, dnwell_box):
    """A Nwell that interacts with ``DNWELL`` is not a pure well, whatever
    the ``DNWELL`` is to the tap itself: the tap keeps the strict 0.16 um
    (the ``NP.5c`` DNWELL split is not expressed)."""
    shapes = _tap_shapes(80, 1000)
    shapes.append({"layer": list(DNWELL), "box": dnwell_box})

    report = _run(tmp_path, shapes)

    assert _marker_count(report) == 4, report["violations"]


def test_tap_touching_dnwell_stays_strict_in_a_pure_nwell(tmp_path):
    """The exclusion is of whole checked polygons: a tap that touches a
    ``DNWELL`` nowhere near the well is held to 0.16 um on *every* edge,
    including the ones deep inside the pure Nwell."""
    shapes = [
        {"layer": list(COMP), "box": [0, 0, 2000, 1000]},
        {"layer": list(NPLUS), "box": [-80, -80, 2080, 1080]},
        # Ends 50 nm short of the DNWELL, so the well itself stays pure.
        {"layer": list(NWELL), "box": [-250, -1000, 1800, 2000]},
        {"layer": list(DNWELL), "box": [1850, -1000, 3000, 2000]},
    ]

    report = _run(tmp_path, shapes)

    assert _marker_count(report) == 4, report["violations"]


@pytest.mark.parametrize(
    ("nplus_margin", "nwell_margin", "expected"),
    [(80, 1000, 4), (80, 250, 4), (10, 1000, 4), (200, 1000, 0)],
)
def test_pcomp_checks_are_unchanged(tmp_path, nplus_margin, nwell_margin, expected):
    """The band is declared on the NCOMP rule only. The mirrored ``Pplus``
    fixture is still judged by the ``PCOMP`` pair exactly as before: a PCOMP
    inside an ``Nwell`` is always within reach of the oversized well, so
    ``pplus.enclosing.comp.1`` holds it to 0.16 um however deep it sits --
    which is also the foundry answer there (``PP.5b`` measures against the
    ``Pplus`` edges inside the well; ``PP.5d`` relaxes taps outside it) --
    and the far rule never fires inside a well."""
    report = _run(
        tmp_path,
        _tap_shapes(nplus_margin, nwell_margin, implant=PPLUS),
        rules=PCOMP_RULES,
    )

    counts = report["rule_counts"]
    assert counts.get("pplus.enclosing.comp.1", 0) == expected
    assert "pplus.enclosing.comp.far.1" not in counts


def test_pcomp_without_a_well_is_unchanged(tmp_path):
    """No well at all: the near PCOMP rule is skipped and the far one holds
    the tap to 0.02 um, as since issue #3484."""
    report = _run(tmp_path, _tap_shapes(80, None, implant=PPLUS), rules=PCOMP_RULES)

    assert report["rule_counts"] == {}
    assert "pplus.enclosing.comp.1" in report["coverage"]["rules_skipped"]
    assert "pplus.enclosing.comp.far.1" in report["coverage"]["rules_checked"]


def test_rule_keeps_its_identity_and_declares_the_band():
    """Same rule id, check kind and strict threshold as before -- a caller
    selecting ``--rules nplus.enclosing.comp.1`` gets both limits -- with the
    foundry split declared beside them."""
    rule = next(rule for rule in get_deck("gf180mcu") if rule.id == RULE)

    assert rule.check == "enclosed"
    assert rule.threshold_dbu == 160
    assert rule.band_spec == EdgeBandSpec(
        context_layers=(NWELL,),
        exclude_layers=(DNWELL,),
        band_width_um=0.429,
        inner_threshold_dbu=20,
    )


def test_list_rules_reports_the_band_and_its_inner_threshold():
    listing = list_deck_rules("gf180mcu")
    (listed,) = [entry for entry in listing["rules"] if entry["id"] == RULE]

    assert listed["threshold_um"] == 0.16
    assert listed["edge_band"] == {
        "context_layers": ["21/0"],
        "exclude_layers": ["12/0"],
        "band_width_um": 0.429,
        "inner_threshold_um": 0.02,
    }
    # Additive: no rule without a band grows the key.
    others = [entry for entry in listing["rules"] if entry["id"] != RULE]
    assert others and all("edge_band" not in entry for entry in others)


def test_band_spec_rejects_invalid_descriptors():
    valid = dict(
        context_layers=(NWELL,),
        exclude_layers=(),
        band_width_um=0.429,
        inner_threshold_dbu=20,
    )
    with pytest.raises(ValueError, match="context_layers"):
        EdgeBandSpec(**{**valid, "context_layers": ()})
    with pytest.raises(ValueError, match="band_width_um"):
        EdgeBandSpec(**{**valid, "band_width_um": 0.0})
    with pytest.raises(ValueError, match="inner_threshold_dbu"):
        EdgeBandSpec(**{**valid, "inner_threshold_dbu": -1})
    with pytest.raises(ValueError, match="band_metrics"):
        EdgeBandSpec(**{**valid, "band_metrics": "square"})
    with pytest.raises(ValueError, match="inner_metrics"):
        EdgeBandSpec(**{**valid, "inner_metrics": "euclidean"})


def _banded_rule(tmp_path, monkeypatch, **overrides):
    """Install a one-rule synthetic deck carrying ``overrides`` and run it on
    an empty stream, so the rule is declared badly *and* its layers are
    absent."""
    rule = DrcRule(
        **{
            "id": "s.enclosed.1",
            "description": "synthetic banded enclosure",
            "layer": (10, 0),
            "check": "enclosed",
            "threshold_dbu": 160,
            "other_layer": (20, 0),
            "band_spec": EdgeBandSpec(
                context_layers=((30, 0),),
                exclude_layers=(),
                band_width_um=0.429,
                inner_threshold_dbu=20,
            ),
            **overrides,
        }
    )
    monkeypatch.setattr("klayout_tools.drc.get_deck", lambda name: [rule])
    monkeypatch.setattr("klayout_tools.drc.get_nominal_dbu", lambda name: 0.001)
    monkeypatch.setattr("klayout_tools.drc.get_layer_names", lambda name: {})
    return write_layout([], tmp_path / "empty.gds")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"check": "enclosing"}, "only valid on 'enclosed' checks, not 'enclosing'"),
        ({"other_layer": None}, "requires other_layer"),
        ({"require_containment": True}, "cannot be combined with require_containment"),
        ({"threshold_dbu": 20}, "inner_threshold_dbu must be below threshold_dbu"),
    ],
)
def test_band_spec_misdeclared_on_a_rule_fails_loudly(
    tmp_path, monkeypatch, overrides, message
):
    path = _banded_rule(tmp_path, monkeypatch, **overrides)

    with pytest.raises(DrcError, match=message):
        run_drc(path, "synthetic")


def test_rule_without_a_band_spec_is_unaffected(tmp_path, monkeypatch):
    """``band_spec=None`` is every pre-existing rule: nothing to validate."""
    path = _banded_rule(tmp_path, monkeypatch, band_spec=None)

    report = run_drc(path, "synthetic")

    assert "s.enclosed.1" in report["coverage"]["rules_skipped"]


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


def _region(layout, layer):
    """``layer``'s shapes as a region; a layer the fixture never drew is
    empty."""
    import klayout.db as kdb

    index = layout.find_layer(*layer)
    if index is None:
        return kdb.Region()
    return kdb.Region(layout.top_cell().begin_shapes_rec(index))


def _segment(points):
    return tuple(sorted(points))


def _foundry_np5d_segments(layout):
    """The NCOMP-side stretches ``NP.5di``/``NP.5dii`` report, transcribed
    from the pinned ``rule_decks/nplus.drc`` -- the deck's own selection
    (boolean pure well, ``not_outside``/``outside``, the band as
    ``nwell_n_dn - sized_in``) and the ``Nplus``-side ``enclosing`` checks.
    Unlike the engine's band split this never partitions the tap's edges
    itself, so agreement is not built in. No ``Pplus`` is drawn, so the
    deck's butted-edge exclusion drops nothing."""
    import klayout.db as kdb

    nwell, nplus = _region(layout, NWELL).merged(), _region(layout, NPLUS).merged()
    nwell_n_dn = nwell - _region(layout, DNWELL)
    sized_in = nwell_n_dn.sized(-429)
    background = (nplus - _region(layout, DNWELL)).not_outside(nwell).edges()
    foreground = (
        (_region(layout, COMP) & nplus).not_outside(nwell).outside(kdb.Region()).edges()
    )
    found = set()
    for edges, distance, metrics in (
        (nwell_n_dn - sized_in, 160, kdb.Region.Projection),
        (sized_in, 20, kdb.Region.Euclidian),
    ):
        for pair in background.enclosing_check(
            foreground & edges, distance, metrics=metrics
        ).each():
            found.add(
                _segment(
                    [(pair.second.x1, pair.second.y1), (pair.second.x2, pair.second.y2)]
                )
            )
    return found


def _curated_ncomp_segments(report, layout):
    """Each marker's own NCOMP-side stretch: the two hull points on the NCOMP
    boundary (the other two lie on ``Nplus``, a margin away)."""
    import klayout.db as kdb

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
    import klayout.db as kdb

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
