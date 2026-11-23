"""Issue #7419: a through-less tie whose ``connect_to`` role never
interacts with its own tap sites is skipped as ``degenerate_connect_target``.

The reproduction behind the issue (ASAP7, ``klt`` 0.9.2, public inputs: the
``TAPCELL_ASAP7_75t_R`` cell of ``The-OpenROAD-Project/asap7sc7p5t_28``):
a tap reaches M1 only through SDT -> LISD -> V0, so ``connect_to: "M1"``
against a stackup without LISD never overlaps the tap geometry in plan
view. The direct ``connect(tap_sites, target)`` join can then never fire,
every well reports ``erc.missing_tie``, and the run is byte-identical to a
genuinely untied layout. The skip reason separates the two states without
touching the findings, which a genuinely untied block must keep receiving.
"""

import klayout.db as kdb

from helpers.erc_substrate_fixtures import _run

_TIE = "nwell_vdd"
_ID = f'erc.missing_tie:["{_TIE}"]'


def _spec(ties):
    return {
        "stackup": [
            {"name": "poly", "layer": "5/0", "role": "gate"},
            {"name": "met1", "layer": "8/0", "label_layer": "8/25"},
        ],
        "vias": [{"name": "cont", "layer": "6/0", "between": ["poly", "met1"]}],
        "nets": [{"name": "VDD", "kind": "supply"}],
        "ties": ties,
    }


def _tie(**over):
    entry = {
        "name": _TIE,
        "well_layer": "31/0",
        "tap_layer": "1/0",
        "tap_is_dedicated": True,
        "connect_to": "met1",
        "net": "VDD",
    }
    entry.update(over)
    return entry


def _layout(landing_over_tap=True, tap=True):
    """One n-well (31/0) holding one dedicated tap (1/0), as a ``(layout,
    top)`` pair. With ``landing_over_tap`` the met1 landing and its VDD
    label sit directly on the tap (the working, issue-Spec-B shape);
    without, the only met1 on the die sits far from it, so no plan-view
    interaction exists (the zero-overlap, issue-Spec-A shape: the role is
    drawn, just never where the tap is)."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("fixture")

    def box(layer, *coords):
        top.shapes(layout.layer(layer, 0)).insert(kdb.Box(*coords))

    box(31, 0, 0, 3000, 3000)
    if tap:
        box(1, 200, 200, 800, 1800)
    if landing_over_tap:
        box(8, 200, 200, 800, 1800)
        top.shapes(layout.layer(8, 25)).insert(kdb.Text("VDD", 500, 1000))
    else:
        box(8, 5000, 200, 5600, 1800)
        top.shapes(layout.layer(8, 25)).insert(kdb.Text("VDD", 5300, 1000))
    return layout, top


def _skips(report):
    return {
        record["id"]: record["reason"] for record in report["erc_coverage"]["skipped"]
    }


def _missing_ties(report):
    return [
        (f["rule"], f["layer"])
        for f in report["erc_findings"]
        if f["rule"] == "erc.missing_tie"
    ]


def test_zero_overlap_tie_is_skipped_but_still_finds_the_well(tmp_path):
    report = _run(
        tmp_path, _layout(landing_over_tap=False)[0], _spec([_tie()]), deck="sg13g2"
    )

    # Findings unchanged: the well reports an ordinary untied tap ...
    assert _missing_ties(report) == [("erc.missing_tie", _TIE)]
    assert report["erc_status"] == "violations"
    # ... and the envelope now says the join could never have fired.
    assert _skips(report) == {_ID: "degenerate_connect_target"}
    assert _ID not in report["erc_coverage"]["checked"]
    assert report["erc_coverage"]["missing_tie_checked"] is False


def test_an_overlapping_connect_to_neither_skips_nor_finds(tmp_path):
    report = _run(
        tmp_path, _layout(landing_over_tap=True)[0], _spec([_tie()]), deck="sg13g2"
    )

    assert _missing_ties(report) == []
    assert report["erc_coverage"]["skipped"] == []
    assert _ID in report["erc_coverage"]["checked"]
    assert report["erc_status"] == "clean"


def test_partial_overlap_is_an_ordinary_untied_well_not_a_skip(tmp_path):
    layout, top = _layout(landing_over_tap=True)
    # A second well whose tap has no met1 over it: some tap site still
    # interacts with the role, so the declaration stays falsifiable and
    # only the untied well reports.
    top.shapes(layout.layer(31, 0)).insert(kdb.Box(4000, 0, 7000, 3000))
    top.shapes(layout.layer(1, 0)).insert(kdb.Box(4200, 200, 4800, 1800))
    report = _run(tmp_path, layout, _spec([_tie()]), deck="sg13g2")

    assert _skips(report) == {}
    assert _ID in report["erc_coverage"]["checked"]
    assert _missing_ties(report) == [("erc.missing_tie", _TIE)]
    assert report["erc_status"] == "violations"


def test_a_through_tie_is_not_graded_by_this_reason(tmp_path):
    # The contact-qualified join is deliberately out of scope (issue
    # #7419): a through tie with zero qualifying cuts is the legitimate
    # "untied pad" case of `tests/test_erc_tie_through.py` -- ordinary
    # findings, checked work, no skip.
    report = _run(
        tmp_path,
        _layout(landing_over_tap=False)[0],
        _spec([_tie(through="cont")]),
        deck="sg13g2",
    )

    assert _skips(report) == {}
    assert _ID in report["erc_coverage"]["checked"]
    assert _missing_ties(report) == [("erc.missing_tie", _TIE)]


def test_a_tap_degenerate_tie_keeps_its_tap_reason(tmp_path):
    # Bare `tap_layer`: no narrowing, not dedicated -- tap-degenerate *and*
    # target-disjoint. The tap reason wins; it names the nearer defect.
    tie = _tie()
    del tie["tap_is_dedicated"]
    report = _run(
        tmp_path, _layout(landing_over_tap=False)[0], _spec([tie]), deck="sg13g2"
    )

    assert _skips(report) == {_ID: "degenerate_tap_declaration"}


def test_a_well_degenerate_tie_keeps_its_well_reason(tmp_path):
    # A complement substrate whose named layer draws nothing: the asserted
    # region is the whole die -- well-degenerate *and* (with met1 away from
    # the tap) target-disjoint. The well reason wins.
    tie = _tie(well_layer=None, well_complement_of=["99/0"])
    report = _run(
        tmp_path, _layout(landing_over_tap=False)[0], _spec([tie]), deck="sg13g2"
    )

    assert _skips(report) == {_ID: "degenerate_well_assertion"}


def test_an_empty_tap_region_stays_an_honest_no_tap_finding(tmp_path):
    # The new reason requires a non-empty tap: a well with no tap drawn is
    # the honest "no tap drawn" finding, not a degeneracy signal.
    report = _run(
        tmp_path,
        _layout(landing_over_tap=False, tap=False)[0],
        _spec([_tie()]),
        deck="sg13g2",
    )

    assert _skips(report) == {}
    missing = [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"]
    assert len(missing) == 1
    assert "no 'nwell_vdd' tap contact drawn" in missing[0]["description"]


def test_a_merely_touching_landing_counts_as_interaction(tmp_path):
    # met1 sharing only the tap's right edge: `interacting` counts touch,
    # matching what `l2n.connect` joins, so this is overlap -- not a skip.
    layout, top = _layout(landing_over_tap=False)
    top.shapes(layout.layer(8, 0)).insert(kdb.Box(800, 200, 1400, 1800))
    top.shapes(layout.layer(8, 25)).insert(kdb.Text("VDD", 1100, 1000))
    report = _run(tmp_path, layout, _spec([_tie()]), deck="sg13g2")

    assert _skips(report) == {}
    assert _ID in report["erc_coverage"]["checked"]
