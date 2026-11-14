"""Tests for the ASAP7 ``M1``-``M3`` tip-spacing flag (issue #4495).

Three tiers, mirroring ``tests/test_drc_asap7_deck.py``'s own split:

- unit checks on generated geometry (no binary): which rule each pad/wire
  configuration trips, how violations are attributed to ``VIA_*`` landing
  pads, and that the pass writes nothing;
- a **static parity** check that every threshold and edge-length class this
  pass hard-codes is the one ``decks/asap7.drc`` states -- ASAP7 has no
  curated rule table to resolve them from (``get_deck("asap7")`` raises), so
  this test is what keeps the two in step;
- a **differential oracle** gated on the ``klayout`` binary: the real deck is
  run over the same generated layout and must report exactly the per-rule
  counts this pass reports. This is what makes the pass's numbers
  deck-equivalent rather than merely plausible.
"""

from __future__ import annotations

import collections
import re
import shutil

import klayout.db as kdb
import pytest

from klayout_tools.decks.asap7_rules import DECK_PATH
from klayout_tools.drc import run_drc_klayout_engine
from klayout_tools.place_and_route_via_tip import (
    _NARROW_MAX_NM,
    _SHORT_MAX_NM,
    _TIP_RULE_LEF_LAYERS,
    _TIP_RULES,
    check_via_pad_tips,
)

#: The deck's own nominal dbu (``asap7.lyt``): 0.25 nm, the only resolution
#: that expresses its 36.25 nm edge-length bound exactly.
DBU = 0.00025

M1 = (19, 0)
M2 = (20, 0)
M3 = (30, 0)
V1 = (21, 0)
LEF_TO_GDS = {"M1": M1, "M2": M2, "M3": M3, "V1": V1}

_needs_klayout = pytest.mark.skipif(
    shutil.which("klayout") is None,
    reason="klayout binary is not installed on this machine",
)


def _nm(value: float) -> int:
    """Nanometres at the DRM's 1x drawing scale -> database units."""
    return int(round(value / 1000.0 / DBU))


class _Builder:
    """A layout with one top cell, where ``wire`` draws plain routed metal and
    ``pad`` draws a ``VIA_*`` cell's landing pad (the router-inserted geometry
    the flag attributes violations to)."""

    def __init__(self) -> None:
        self.layout = kdb.Layout()
        self.layout.dbu = DBU
        self.top = self.layout.create_cell("top")
        self._vias = 0

    def _index(self, layer: tuple[int, int]) -> int:
        return self.layout.layer(*layer)

    def wire(self, layer, x0, y0, x1, y1) -> None:
        self.top.shapes(self._index(layer)).insert(
            kdb.Box(_nm(x0), _nm(y0), _nm(x1), _nm(y1))
        )

    def pad(self, layer, x0, y0, x1, y1, cut: bool = True) -> None:
        self._vias += 1
        via = self.layout.create_cell(f"VIA_VIA12_{self._vias}")
        w, h = (x1 - x0) / 2.0, (y1 - y0) / 2.0
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        via.shapes(self._index(layer)).insert(kdb.Box(_nm(-w), _nm(-h), _nm(w), _nm(h)))
        if cut:
            side = min(w, h)
            via.shapes(self._index(V1)).insert(
                kdb.Box(_nm(-side), _nm(-side), _nm(side), _nm(side))
            )
        self.top.insert(
            kdb.CellInstArray(via.cell_index(), kdb.Trans(_nm(cx), _nm(cy)))
        )

    def run(self, variant: str = "asap7") -> dict:
        return check_via_pad_tips(
            kdb,
            layout=self.layout,
            top_cell=self.top,
            variant=variant,
            lef_to_gds=LEF_TO_GDS,
        )

    def merged(self, layer) -> kdb.Region:
        index = self.layout.find_layer(*layer)
        return kdb.Region(self.top.begin_shapes_rec(index)).merged()


def _by_rule(report: dict) -> dict[str, int]:
    return {entry["rule"]: entry["violations"] for entry in report["rules"]}


def _attributed(report: dict) -> dict[str, int]:
    return {entry["rule"]: entry["via_pads"] for entry in report["rules"]}


def _pad_tip_near_wire_side(layer, gap: float, x: float = 0.0) -> _Builder:
    """A landing pad whose 18 nm (narrow) tip edge faces the side of a long
    wire ``gap`` nm away -- the ``S.2`` tip-to-side case issue #4495 reports,
    with the pad fed from below so it is connected metal, not an island."""
    b = _Builder()
    b.pad(layer, x - 9, -20, x + 9, 20)
    b.wire(layer, x - 9, -200, x + 9, -20)
    b.wire(layer, x - 200, 20 + gap, x + 200, 20 + gap + 18)
    return b


def test_pad_tip_inside_the_tip_to_side_spacing_is_reported():
    report = _pad_tip_near_wire_side(M1, 20)
    result = report.run()
    assert result["status"] == "violations"
    assert _by_rule(result) == {"M1.S.2": 1}
    assert _attributed(result) == {"M1.S.2": 1}
    assert result["violations"] == 1 and result["via_pad_violations"] == 1


def test_pad_tip_at_the_tip_to_side_threshold_is_clean():
    result = _pad_tip_near_wire_side(M1, 25).run()
    assert result["status"] == "clean"
    assert result["rules"] == [] and result["violations"] == 0


@pytest.mark.parametrize(
    ("layer", "rule"),
    [(M1, "M1.S.2"), (M2, "M2.S.2"), (M3, "M3.S.2")],
    ids=["M1", "M2", "M3"],
)
def test_every_tip_rule_layer_is_checked_not_only_m1(layer, rule):
    """Issue #4495's follow-up report: the router-vs-deck disagreement is not
    specific to the V1/M1 landing pad, and the deck states the same
    length-conditioned family for M2 and M3 ("rules stated for M1 apply to M2
    and M3 as well"). The identical geometry on each of the three layers must
    therefore be reported against that layer's own rule id."""
    result = _pad_tip_near_wire_side(layer, 20).run()
    assert _by_rule(result) == {rule: 1}
    assert _attributed(result) == {rule: 1}


def test_wire_only_violation_is_counted_but_not_attributed_to_via_pads():
    """A tip violation between two routed wires is still a deck violation --
    it is reported, with ``via_pads`` 0, so a caller can tell the router's
    landing geometry apart from the design's own routing."""
    b = _Builder()
    b.wire(M1, -9, -200, 9, 0)
    b.wire(M1, -200, 20, 200, 38)
    result = b.run()
    assert _by_rule(result) == {"M1.S.2": 1}
    assert _attributed(result) == {"M1.S.2": 0}
    assert result["violations"] == 1 and result["via_pad_violations"] == 0


def test_pass_writes_no_geometry():
    """The flag must never change the merged GDS: the issue's own risk note
    is that touching landing geometry can break connectivity or a via
    enclosure, and a report cannot."""
    b = _pad_tip_near_wire_side(M1, 20)
    before = b.merged(M1)
    cells_before = b.layout.cells()
    assert b.run()["violations"] == 1
    after = b.merged(M1)
    assert (before ^ after).is_empty() and before.count() == after.count()
    assert b.layout.cells() == cells_before


def test_pad_nested_inside_another_cell_is_still_attributed():
    """The sibling *repair* pass deliberately refuses a via cell nested in
    another cell (it cannot enumerate its placements safely), but a report
    has no such constraint: nested landing geometry is exactly the kind this
    flag exists to surface, so it must not fall out of the attribution."""
    b = _Builder()
    pad_via = b.layout.create_cell("VIA_VIA12_nested")
    index = b.layout.layer(*M1)
    pad_via.shapes(index).insert(kdb.Box(_nm(-9), _nm(-20), _nm(9), _nm(20)))
    middle = b.layout.create_cell("middle")
    middle.insert(kdb.CellInstArray(pad_via.cell_index(), kdb.Trans(0, 0)))
    b.top.insert(kdb.CellInstArray(middle.cell_index(), kdb.Trans(_nm(100), 0)))
    b.wire(M1, -100, 40, 300, 58)
    result = b.run()
    assert _by_rule(result) == {"M1.S.2": 1}
    assert _attributed(result) == {"M1.S.2": 1}


def test_non_asap7_is_a_named_skip():
    result = _pad_tip_near_wire_side(M1, 20).run(variant="sky130A")
    assert result["status"] == "skipped"
    assert result["reason"] == "PDK family of 'sky130A' is not ASAP7; no-op"
    assert result["violations"] == 0 and result["rules"] == []


def test_no_tip_rule_layer_in_the_map_is_a_named_skip():
    b = _Builder()
    b.wire(M1, 0, 0, 100, 18)
    result = check_via_pad_tips(
        kdb,
        layout=b.layout,
        top_cell=b.top,
        variant="asap7",
        lef_to_gds={"V1": V1},
    )
    assert result["status"] == "skipped"
    assert "no M1/M2/M3 routed-net layer" in result["reason"]


def test_absent_layer_in_the_layout_is_clean_not_a_skip():
    """``M2``/``M3`` are in the map but undrawn: the pass ran, so the answer
    is "clean", never the "skipped" an unmeasured layer gets."""
    b = _Builder()
    b.wire(M1, 0, 0, 1000, 18)
    result = b.run()
    assert result["status"] == "clean" and result["reason"] is None


# --------------------------------------------------------------------------
# Static parity with the packaged deck
# --------------------------------------------------------------------------

#: ``decks/asap7.drc``'s Table 3.12.1 block, which states the tip family.
_TABLE_3_12_1 = re.compile(r"# Table 3\.12\.1 -- M1-M3\n(?P<body>.*?)\n# -{10}", re.S)
#: ``{ "M1" => m1, "M2" => m2, "M3" => m3 }.each do |name, m|``
_FAMILY_LAYERS = re.compile(r'\{\s*((?:"M\d"\s*=>\s*m\d,?\s*)+)\}\.each')
_FAMILY_LAYER = re.compile(r'"(M\d)"')
#: ``m_long = m.edges.with_length(36.25.nm, nil)``
_CLASS_DEF = re.compile(
    r"^\s*(m_\w+)\s*=\s*m\.edges\.with_length\(([\w.]+), ([\w.]+)\)", re.M
)
#: ``m_short.separation(m_long, 25.nm, projection)`` / ``m_tip.space(27.nm,
#: projection)``, whose ``.output("#{name}.S.N", ...)`` follows on the next
#: line.
_CHECK = re.compile(
    r"^\s*(m_\w+)\.(space|separation)\(([^)]*)\)\s*\n"
    r'\s*\.output\("#\{name\}\.(S\.\d)"',
    re.M,
)
_DECK_CLASS_NAMES = {
    "m_long": "long",
    "m_short": "short",
    "m_tip": "tip",
    "m_narrow_tip": "narrow",
}


def _deck_table() -> str:
    body = _TABLE_3_12_1.search(DECK_PATH.read_text()) if DECK_PATH else None
    assert body is not None, "deck no longer carries a 'Table 3.12.1 -- M1-M3' block"
    return body.group("body")


def _nm_literal(token: str) -> float | None:
    return None if token == "nil" else float(token.removesuffix(".nm"))


def test_tip_rule_layers_match_the_deck():
    layers = _FAMILY_LAYERS.search(_deck_table())
    assert layers is not None
    assert tuple(_FAMILY_LAYER.findall(layers.group(1))) == _TIP_RULE_LEF_LAYERS


def test_edge_length_classes_match_the_deck():
    classes = {
        name: (_nm_literal(low), _nm_literal(high))
        for name, low, high in _CLASS_DEF.findall(_deck_table())
    }
    assert classes == {
        "m_long": (_SHORT_MAX_NM, None),
        "m_short": (None, _SHORT_MAX_NM),
        "m_tip": (_NARROW_MAX_NM, _SHORT_MAX_NM),
        "m_narrow_tip": (None, _NARROW_MAX_NM),
    }


def test_tip_rule_thresholds_and_primitives_match_the_deck():
    """Every ``S.2``-``S.5`` check this pass runs must match the deck's own
    operand classes, threshold **and** primitive. ``space`` (one collection)
    and ``separation`` (two) do not count the same edge pairs, so the
    primitive is part of the contract, not an implementation detail: this
    pass encodes it as "first class == second class"."""
    deck = set()
    for first, primitive, args, rule in _CHECK.findall(_deck_table()):
        parts = [part.strip() for part in args.split(",")]
        assert parts[-1] == "projection", f"{rule} is not a projection check"
        if primitive == "space":
            second, threshold = first, parts[0]
        else:
            second, threshold = parts[0], parts[1]
        deck.add(
            (
                rule,
                _DECK_CLASS_NAMES[first],
                _DECK_CLASS_NAMES[second],
                _nm_literal(threshold),
            )
        )
    tip_family = {entry for entry in deck if entry[0] in {"S.2", "S.3", "S.4", "S.5"}}
    assert tip_family == set(_TIP_RULES)
    # The same block also states `S.1` (side to side) and `S.6` (corner to
    # corner). Both are deliberately out of this pass's scope -- it reports
    # the tip family only -- so their presence here is the check that the
    # exclusion is still a choice and not an oversight.
    assert ("S.1", "long", "long", 18.0) in deck
    assert not any(entry[0] == "S.1" for entry in _TIP_RULES)


# --------------------------------------------------------------------------
# Differential oracle: the real deck over the same geometry
# --------------------------------------------------------------------------


def _all_four_rules() -> _Builder:
    """One layout tripping each of ``M1.S.2``-``M1.S.5`` once, in slots far
    enough apart (1 um) that no case can see another's geometry.

    Edge-length classes, from Table 3.12.1: an 18 nm edge is a *narrow* tip
    (< 24 nm), a 30 nm edge a *tip* (24..36 nm) and a 400 nm edge a *side*
    (> 36 nm).
    """
    b = _Builder()
    # S.2 (25 nm): narrow pad tip facing a long wire side, 20 nm away.
    b.pad(M1, -9, -20, 9, 20)
    b.wire(M1, -9, -200, 9, -20)
    b.wire(M1, -200, 40, 200, 58)
    # S.4 (31 nm): two narrow pad tips facing each other 30 nm apart.
    b.pad(M1, 991, -20, 1009, 20)
    b.pad(M1, 991, 50, 1009, 90)
    # S.3 (27 nm): two 30 nm wire tips facing each other 26 nm apart.
    b.wire(M1, 1985, -20, 2015, 20)
    b.wire(M1, 1985, 46, 2015, 86)
    # S.5 (31 nm): a 30 nm wire tip facing a narrow pad tip 30 nm away.
    b.wire(M1, 2985, -20, 3015, 20)
    b.pad(M1, 2991, 50, 3009, 90)
    return b


def test_generated_oracle_layout_trips_all_four_tip_rules():
    """Guard for the oracle below: if this geometry stops exercising all four
    rules, a deck/pass agreement on the remainder would be vacuous."""
    assert set(_by_rule(_all_four_rules().run())) == {
        "M1.S.2",
        "M1.S.3",
        "M1.S.4",
        "M1.S.5",
    }


@_needs_klayout
def test_counts_match_the_real_deck(tmp_path):
    """The differential oracle (issue #4495): ``klt drc``'s own ASAP7 deck,
    run through the ``klayout`` binary over the same layout, must report
    exactly the per-rule tip counts this in-process pass reports."""
    b = _all_four_rules()
    path = tmp_path / "tips.gds"
    b.layout.write(str(path))

    deck = collections.Counter(
        violation["rule"]
        for violation in run_drc_klayout_engine(str(path), str(DECK_PATH))["violations"]
        if re.fullmatch(r"M[123]\.S\.[2345]", violation["rule"])
    )
    assert _by_rule(b.run()) == dict(deck)
