"""Per-hit boxes, provenance flags and the ASAP7 deck oracle (issue #7405)."""

import re

import pytest

from klayout_tools.decks.asap7_rules import DECK_PATH
from klayout_tools.drc import run_drc_klayout_engine
from klayout_tools.place_and_route_via_tip import _MARKERS_PER_RULE
from test_place_and_route_via_tip import (
    M1,
    _all_four_rules,
    _Builder,
    _needs_klayout,
    _pad_tip_near_wire_side,
)


def test_marker_box_spans_the_pad_tip_to_wire_side_gap():
    """The manual check of the curation: ``rules[0]["markers"]``'s box sits
    between the pad tip (``y`` = 20 nm) and the wire side (``y`` = 40 nm),
    spanning exactly the 20 nm gap, with the pad's attribution flag set."""
    result = _pad_tip_near_wire_side(M1, 20).run()
    entry = result["rules"][0]
    assert entry["markers_truncated"] is False
    assert len(entry["markers"]) == 1
    marker = entry["markers"][0]
    assert marker["box_um"] == [-0.009, 0.02, 0.009, 0.04]
    assert marker["via_pad"] is True and marker["router_fill"] is False


def test_marker_boxes_are_microns_independent_of_the_layout_dbu():
    """``box_um`` is a micron figure in top-cell coordinates, not a database
    unit: the same geometry built at the deck's own 0.25 nm dbu and at a
    1 nm dbu yields the same boxes (and the same flags)."""
    fine = _pad_tip_near_wire_side(M1, 20, dbu=0.00025).run()
    coarse = _pad_tip_near_wire_side(M1, 20, dbu=0.001).run()
    assert coarse["rules"][0]["markers"] == fine["rules"][0]["markers"]
    box = fine["rules"][0]["markers"][0]["box_um"]
    assert box[3] - box[1] == 0.02  # the 20 nm gap, in microns


def test_marker_flags_and_length_match_the_subset_counts():
    """Not truncated: one marker per hit, the true ``via_pad`` flags number
    exactly ``via_pads`` (likewise ``router_fill``), and the markers are
    sorted by box so the list is deterministic."""
    result = _all_four_rules().run()
    for entry in result["rules"]:
        assert entry["markers_truncated"] is False
        assert len(entry["markers"]) == entry["violations"]
        assert sum(m["via_pad"] for m in entry["markers"]) == entry["via_pads"]
        assert sum(m["router_fill"] for m in entry["markers"]) == entry["router_fill"]
        boxes = [m["box_um"] for m in entry["markers"]]
        assert boxes == sorted(boxes)


def _many_isolated_tip_hits(count: int) -> _Builder:
    """``count`` copies of the ``S.2`` pad-tip-vs-wire-side case, 1 um apart
    so no slot can see another's geometry: one hit per slot, every one of
    them attributed to a landing pad."""
    b = _Builder()
    for i in range(count):
        x = i * 1000.0
        b.pad(M1, x - 9, -20, x + 9, 20)
        b.wire(M1, x - 9, -200, x + 9, -20)
        b.wire(M1, x - 200, 40, x + 200, 58)
    return b


@pytest.mark.parametrize(
    "hits", [_MARKERS_PER_RULE, _MARKERS_PER_RULE + 1], ids=["at_cap", "over_cap"]
)
def test_markers_are_capped_and_truncation_is_flagged(hits):
    """At the cap the list is whole and untruncated; one hit over it, the
    list is cut at the cap, ``markers_truncated`` says so, and the counts
    stay the full, uncapped figures."""
    result = _many_isolated_tip_hits(hits).run()
    entry = next(e for e in result["rules"] if e["rule"] == "M1.S.2")
    assert entry["violations"] == hits and entry["via_pads"] == hits
    assert len(entry["markers"]) == _MARKERS_PER_RULE
    assert entry["markers_truncated"] is (hits > _MARKERS_PER_RULE)
    boxes = [m["box_um"] for m in entry["markers"]]
    assert boxes == sorted(boxes)


@_needs_klayout
def test_marker_boxes_match_the_real_deck(tmp_path):
    """The box half of the oracle (issue #7405): the deck's own violation
    marker boxes -- the integer-dbu ``bbox`` of each ``M[123].S.[2345]``
    record -- equal, as sets per rule, the ``markers`` boxes converted back
    to database units. What the reporter measured by rebuilding the checks
    with ``klayout.db`` is now the pass's own output."""
    b = _all_four_rules()
    path = tmp_path / "tips.gds"
    b.layout.write(str(path))

    deck_boxes: dict[str, set[tuple[int, int, int, int]]] = {}
    for violation in run_drc_klayout_engine(str(path), str(DECK_PATH))["violations"]:
        if re.fullmatch(r"M[123]\.S\.[2345]", violation["rule"]):
            bbox = violation["bbox"]
            deck_boxes.setdefault(violation["rule"], set()).add(
                (bbox["left"], bbox["bottom"], bbox["right"], bbox["top"])
            )
    dbu = b.layout.dbu
    marker_boxes = {
        entry["rule"]: {
            tuple(int(round(um / dbu)) for um in marker["box_um"])
            for marker in entry["markers"]
        }
        for entry in b.run()["rules"]
    }
    assert marker_boxes == deck_boxes
