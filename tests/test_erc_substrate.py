"""Substrate-continuity post-pass for ``erc.supply_short`` (issue #5342).

Design: ``docs/design/erc-substrate-continuity.md`` sections 3 and 9.
Fixtures 1-4 plus the monotonicity pin. gf180mcu's deck declares
``substrate_isolation`` (DNWELL 12/0), so ``deck="gf180mcu"`` enables the pass.
"""

from __future__ import annotations

import klayout.db as kdb
import pytest

from helpers.erc_substrate_fixtures import (
    DBU,
    _box,
    _ptie,
    _rail,
    _run,
    _shorts,
    _spec,
    _two_rails,
    _um,
)
from klayout_tools.erc_substrate import _FAMILY_MISSING


def test_fixture1_substrate_only_short_is_reported(tmp_path):
    layout, _ = _two_rails()
    report = _run(tmp_path, layout, _spec([_ptie("p1", "VSS"), _ptie("p2", "VSS2")]))
    shorts = _shorts(report)
    assert len(shorts) == 1
    assert shorts[0]["via"] == "substrate"
    assert shorts[0]["body"]["kind"] == "substrate"
    assert shorts[0]["body"]["id"] == "vsubs"
    assert shorts[0]["body"]["ties"] == ["p1", "p2"]
    checked = report["erc_coverage"]["checked"]
    assert "erc.supply_short.substrate" in checked
    assert not any(
        e["id"] == "erc.supply_short" for e in report["erc_coverage"]["inapplicable"]
    )
    assert report["status"] != "clean"


def test_fixture2_dnwell_island_isolates_the_second_tap(tmp_path):
    layout, top = _two_rails()
    _box(top, layout, (12, 0), -1, 4, 21, 7)  # DNWELL around the VSS2 rail
    report = _run(tmp_path, layout, _spec([_ptie("p1", "VSS"), _ptie("p2", "VSS2")]))
    assert _shorts(report) == []
    bodies = {b["id"]: b for b in report["provenance"]["substrate_bodies"]}
    assert set(bodies) == {"vsubs", "vsubs_iso0"}
    assert bodies["vsubs_iso0"]["ties"] == ["p2"]


def test_fixture3_blanket_well_row_has_no_supply_short(tmp_path):
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")
    _box(top, layout, (10, 0), 0, 8, 20, 12)  # blanket n-well
    _rail(layout, top, 10, "VDD")
    _rail(layout, top, 0, "VSS")
    spec = _spec(
        [
            _ptie("n1", "VDD", well_layer="10/0", well_complement_of=None),
            _ptie("p1", "VSS"),
        ],
        nets=("VDD", "VSS"),
    )
    report = _run(tmp_path, layout, spec)
    assert _shorts(report) == []


def test_fixture4_degenerate_tie_joins_nothing(tmp_path):
    layout, _ = _two_rails()
    # Un-narrowed tap_layer (no tap_requires): degenerate -> no join.
    ties = [
        _ptie("p1", "VSS", tap_requires=[], tap_is_dedicated=False),
        _ptie("p2", "VSS2", tap_requires=[], tap_is_dedicated=False),
    ]
    report = _run(tmp_path, layout, _spec(ties))
    assert _shorts(report) == []


def test_pass_never_alters_gates_or_other_findings(tmp_path):
    layout, _ = _two_rails()
    ties = [_ptie("p1", "VSS"), _ptie("p2", "VSS2")]
    with_pass = _run(tmp_path, layout, _spec(ties), "w")
    without = _run(tmp_path, layout, _spec(ties), "n", deck=None)
    assert with_pass["gates"] == without["gates"]

    def keep(report):
        return [
            f
            for f in report["erc_findings"]
            if f["rule"] != "erc.missing_tie" and f.get("via") != "substrate"
        ]

    assert keep(with_pass) == keep(without)


def test_other_families_keep_the_disclosure(tmp_path):
    layout, _ = _two_rails()
    report = _run(
        tmp_path,
        layout,
        _spec([_ptie("p1", "VSS"), _ptie("p2", "VSS2")]),
        deck=None,
    )
    assert _shorts(report) == []
    entry = next(
        e
        for e in report["erc_coverage"]["inapplicable"]
        if e["id"] == "erc.supply_short"
    )
    assert entry["reason"] == "substrate_isolation_undecidable"
    assert entry["missing"]
    assert "substrate_bodies" not in report["provenance"]


def _ihp_ring(top, layout, gap=False):
    """NWell (31/0) ring around the nBuLay island; `gap` opens its left side."""
    for box in (
        (-2, 3, 22, 4),
        (-2, 7, 22, 8),
        (21, 3, 22, 8),
        (-2, 3, -1, 8),
    ):
        _box(top, layout, (31, 0), *box)
    if gap:
        region = kdb.Region(top.shapes(layout.layer(31, 0)))
        region -= kdb.Region(kdb.Box(_um(-3), _um(4.5), _um(-0.5), _um(6.5)))
        top.shapes(layout.layer(31, 0)).clear()
        top.shapes(layout.layer(31, 0)).insert(region)


# --- Issue #5343 (piece C): isolation_boxes, same_net_as, disclosure --------


def _isolated_spec(boxes, nets=None):
    spec = _spec([_ptie("p1", "VSS"), _ptie("p2", "VSS2", isolation_boxes=boxes)])
    if nets is not None:
        spec["nets"] = nets
    return spec


def _undecidable(report):
    return [
        e
        for e in report["erc_coverage"]["inapplicable"]
        if e["id"] == "erc.supply_short"
    ]


def test_sky130_isolation_boxes_pass_checked_by_assertion(tmp_path):
    layout, _ = _two_rails()
    spec = _isolated_spec([[-1, 4, 21, 7]])
    report = _run(tmp_path, layout, spec, deck="sky130")
    assert _shorts(report) == []
    cov = report["erc_coverage"]
    assert "erc.supply_short.substrate" in cov["checked"]
    assert "erc.supply_short.substrate" in cov["checked_by_assertion"]
    ids = {b["id"] for b in report["provenance"]["substrate_bodies"]}
    assert "vsubs" in ids and any(i.startswith("isolation_box[") for i in ids)
    (echo,) = report["provenance"]["isolation_assertions"]
    assert echo["tie"] == "p2" and len(echo["bodies"]) == 1


def test_sky130_without_assertion_and_no_dnwell_is_evaluated(tmp_path):
    layout, _ = _two_rails()
    report = _run(
        tmp_path,
        layout,
        _spec([_ptie("p1", "VSS"), _ptie("p2", "VSS2")]),
        deck="sky130",
    )
    assert len(_shorts(report)) == 1
    assert (
        "erc.supply_short.substrate"
        not in report["erc_coverage"]["checked_by_assertion"]
    )


def test_asap7_is_disclosed_with_missing(tmp_path):
    layout, _ = _two_rails()
    report = _run(
        tmp_path, layout, _spec([_ptie("p1", "VSS"), _ptie("p2", "VSS2")]), deck="asap7"
    )
    (entry,) = _undecidable(report)
    assert entry["reason"] == "substrate_isolation_undecidable"
    assert any("asap7" in m for m in entry["missing"])


def _same_net_nets():
    return [
        {"name": "VSS", "kind": "supply"},
        {"name": "VSS2", "kind": "supply", "same_net_as": "VSS"},
    ]


def test_same_net_as_suppresses_substrate_short_and_expected_missing(tmp_path):
    layout, _ = _two_rails()
    spec = _spec([_ptie("p1", "VSS"), _ptie("p2", "VSS2")])
    spec["nets"] = _same_net_nets()
    report = _run(tmp_path, layout, spec)
    assert _shorts(report) == []
    assert not [
        f for f in report["erc_findings"] if f["rule"] == "erc.expected_short_missing"
    ]
    assert report["provenance"]["substrate_declared_joins"] == [
        {"net": "VSS", "other_net": "VSS2", "via": "substrate"}
    ]


def test_same_net_as_without_any_short_stays_missing(tmp_path):
    layout, top = _two_rails()
    _box(top, layout, (12, 0), -1, 4, 21, 7)  # DNWELL isolates VSS2: no join
    spec = _spec([_ptie("p1", "VSS"), _ptie("p2", "VSS2")])
    spec["nets"] = _same_net_nets()
    report = _run(tmp_path, layout, spec)
    missing = [
        f for f in report["erc_findings"] if f["rule"] == "erc.expected_short_missing"
    ]
    assert len(missing) == 1
    assert "substrate_declared_joins" not in report["provenance"]


def test_isolation_box_covering_no_tap_has_empty_effect(tmp_path):
    layout, _ = _two_rails()
    report = _run(tmp_path, layout, _isolated_spec([[40, 40, 41, 41]]), deck="sky130")
    assert len(_shorts(report)) == 1  # nothing was isolated
    (echo,) = report["provenance"]["isolation_assertions"]
    assert echo["bodies"] == []


def test_overlapping_isolation_boxes_merge_into_one_body(tmp_path):
    layout, _ = _two_rails()
    report = _run(
        tmp_path,
        layout,
        _isolated_spec([[-1, 4, 10, 7], [5, 4, 21, 7]]),
        deck="sky130",
    )
    assert _shorts(report) == []
    (echo,) = report["provenance"]["isolation_assertions"]
    assert echo["boxes"] == 2 and len(echo["bodies"]) == 1


@pytest.mark.parametrize("deck", ["sg13g2", "sg13cmos5l"])
def test_ihp_nbulay_island_isolates_the_second_tap(tmp_path, deck):
    """Issues #5345/#5359: the IHP decks declare nBuLay (32/0) as
    ``substrate_isolation`` needing a closed NWell (31/0) ring, so an nBuLay
    island closed by a ring around the VSS2 rail is a separate body and there
    is no substrate-only short; without it the two ties share ``vsubs`` and the
    short is reported."""
    ties = [_ptie("p1", "VSS"), _ptie("p2", "VSS2")]
    layout, top = _two_rails()
    _box(top, layout, (32, 0), -1, 4, 21, 7)
    _ihp_ring(top, layout)
    report = _run(tmp_path, layout, _spec(ties), stem="iso", deck=deck)
    assert _shorts(report) == []
    bodies = {b["id"] for b in report["provenance"]["substrate_bodies"]}
    assert bodies == {"vsubs", "vsubs_iso0"}

    layout, _ = _two_rails()
    report = _run(tmp_path, layout, _spec(ties), stem="noiso", deck=deck)
    assert len(_shorts(report)) == 1


@pytest.mark.parametrize("deck", ["sg13g2", "sg13cmos5l"])
@pytest.mark.parametrize("ring", ["gap", "none"])
def test_ihp_unclosed_or_missing_ring_leaves_island_unisolated(tmp_path, deck, ring):
    """#5359: a gap in (or absence of) the NWell ring leaves the nBuLay island
    on shared substrate, so the short is still reported."""
    ties = [_ptie("p1", "VSS"), _ptie("p2", "VSS2")]
    layout, top = _two_rails()
    _box(top, layout, (32, 0), -1, 4, 21, 7)
    if ring == "gap":
        _ihp_ring(top, layout, gap=True)
    report = _run(tmp_path, layout, _spec(ties), stem="unclosed", deck=deck)
    shorts = _shorts(report)
    assert len(shorts) == 1
    assert shorts[0]["body"]["id"] == "vsubs"


# --- Issue #5745: an asserted well_boxes tie capturing another net's well tap


def _foreign_well_repro():
    """Supply A's asserted ``well_boxes`` overlap supply B's drawn well, and
    B's own tap marker sits inside that overlap."""
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")
    _rail(layout, top, 0, "VSS")
    _box(top, layout, (10, 0), 0, 4, 12, 8)  # B's drawn n-well
    _rail(layout, top, 5, "VSS2")  # its own tap marker sits inside the well
    return layout, top


def _foreign_well_spec():
    return _spec(
        [
            _ptie("a1", "VSS", well_complement_of=None, well_boxes=[[0, -1, 12, 9]]),
            _ptie("b1", "VSS2", well_layer="10/0", well_complement_of=None),
        ]
    )


def test_foreign_well_tap_not_attributed_to_asserted_substrate_body(tmp_path):
    """Issue #5745 repro: ``a1`` asserts substrate over a region that overlaps
    ``b1``'s drawn well, so ``a1``'s (net-agnostic) tap region captures ``b1``'s
    own well tap. That tap taps the *well*, not the substrate, so it must not
    be attributed to ``a1``'s asserted substrate body and the two supplies
    stay unmerged."""
    layout, _ = _foreign_well_repro()
    report = _run(tmp_path, layout, _foreign_well_spec(), stem="repro")
    shorts = _shorts(report)
    assert shorts == []
    bodies = {b["id"]: b for b in report["provenance"]["substrate_bodies"]}
    assert bodies["vsubs"]["ties"] == ["a1"]
    assert bodies["0,4000,12000,8000"]["ties"] == ["b1"]
    assert "erc.supply_short.substrate" in report["erc_coverage"]["checked"]


def test_finding_body_joins_name_the_joining_tap_evidence(tmp_path):
    """A substrate short names, per side, the tap site that joined its net to
    the body: ``body.joins`` carries ``{net, tie, bbox}`` per contributing tap
    polygon (fixture 1's two complement ties sharing ``vsubs``)."""
    layout, _ = _two_rails()
    report = _run(tmp_path, layout, _spec([_ptie("p1", "VSS"), _ptie("p2", "VSS2")]))
    (short,) = _shorts(report)
    assert short["body"]["joins"] == [
        {
            "net": "VSS",
            "tie": "p1",
            "bbox": {"left": 2000, "bottom": 200, "right": 2600, "top": 800},
        },
        {
            "net": "VSS2",
            "tie": "p2",
            "bbox": {"left": 2000, "bottom": 5200, "right": 2600, "top": 5800},
        },
    ]


def test_join_names_the_contributing_well_box_index(tmp_path):
    """When the joining site came from an asserted ``well_boxes`` region, the
    join names the index of the contributing box (0-based, lowest when
    several overlap); a complemented side carries no index."""
    layout, _ = _two_rails()
    spec = _spec(
        [
            _ptie(
                "a1",
                "VSS",
                well_complement_of=None,
                well_boxes=[[0, -1, 3, 3], [0, 3, 20, 7]],
            ),
            _ptie("p2", "VSS2"),
        ]
    )
    report = _run(tmp_path, layout, spec)
    (short,) = _shorts(report)
    assert short["body"]["ties"] == ["a1", "p2"]
    assert short["body"]["joins"] == [
        {
            "net": "VSS",
            "tie": "a1",
            "bbox": {"left": 2000, "bottom": 200, "right": 2600, "top": 800},
            "well_box": 0,
        },
        {
            "net": "VSS2",
            "tie": "p2",
            "bbox": {"left": 2000, "bottom": 5200, "right": 2600, "top": 5800},
        },
    ]


def test_metal_and_substrate_pair_stays_suppressed_by_metal_pairs(tmp_path):
    """A pair already reported through the metal graph is not re-reported
    through the substrate body graph: one short, ``via: "metal"``."""
    layout, top = _two_rails()
    _box(top, layout, (3, 0), 5, 0, 6, 6)  # li1 strap joining both rails
    report = _run(tmp_path, layout, _spec([_ptie("p1", "VSS"), _ptie("p2", "VSS2")]))
    shorts = _shorts(report)
    assert len(shorts) == 1
    assert shorts[0]["via"] == "metal"
    assert "body" not in shorts[0]


@pytest.mark.parametrize("family", sorted(_FAMILY_MISSING))
def test_family_missing_has_no_deck_declared_family(family):
    """#5336: a ``_FAMILY_MISSING`` entry goes dead the moment that family's
    built-in extraction deck declares ``substrate_isolation`` --
    :func:`isolation_missing` returns ``[]`` from the deck before it ever
    reaches the table, so the string survives only to state the opposite of
    what the deck does. That is what the sg13g2/sg13cmos5l entries did between
    #5345/#5359 and #5336."""
    from klayout_tools.decks import UnknownExtractionDeckError, get_extraction_deck

    try:
        deck = get_extraction_deck(family)
    except UnknownExtractionDeckError:
        return  # no built-in deck for this family: the entry stays reachable
    assert deck.substrate_isolation is None, (
        f"_FAMILY_MISSING['{family}'] says the deck declares no isolation layer, "
        f"but the built-in {family} deck declares "
        f"substrate_isolation={deck.substrate_isolation}; drop the entry"
    )
