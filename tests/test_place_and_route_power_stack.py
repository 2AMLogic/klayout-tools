"""Tests for the ASAP7 power-stack enclosure repair (issue #6909).

Three tiers, mirroring ``tests/test_place_and_route_via_tip.py``'s split:

- unit checks on generated geometry (no binary): which pdngen M2 -> M5 stack
  configuration each family of the repair acts on, exactly what it draws,
  that a foreign conductor in the patch zone blocks the patch, and that a
  non-ASAP7 PDK is a byte-identical no-op;
- a **static parity** check pinning the thresholds this pass hard-codes to
  the ones ``decks/asap7.drc`` states;
- a **differential oracle** gated on the ``klayout`` binary: the real deck
  over the same generated layout must flag the three families before the
  repair and clear them -- raising nothing else -- after it.
"""

from __future__ import annotations

import re
import shutil

import klayout.db as kdb
import pytest

from klayout_tools.decks.asap7_rules import DECK_PATH
from klayout_tools.drc import run_drc_klayout_engine
from klayout_tools.place_and_route_power_stack import (
    _ENCLOSURE_NM,
    _SHORT_EDGE_MAX_NM,
    _STUB_TARGET_NM,
    repair_power_stack,
)

#: The deck's own nominal dbu (``asap7.lyt``): 0.25 nm, the only resolution
#: that expresses its 36.25 nm edge-length bound exactly.
DBU = 0.00025

M3 = (30, 0)
M4 = (40, 0)
M5 = (50, 0)
V3 = (35, 0)
V4 = (45, 0)
LEF_TO_GDS = {"M3": M3, "M4": M4, "M5": M5, "V3": V3, "V4": V4}

_needs_klayout = pytest.mark.skipif(
    shutil.which("klayout") is None,
    reason="klayout binary is not installed on this machine",
)


def _nm(value: float) -> int:
    """Nanometres at the DRM's 1x drawing scale -> database units."""
    return int(round(value / 1000.0 / DBU))


class _Builder:
    """One pdngen ``M2 -> M5`` stack column at the ``asap7sc7p5t`` preset's
    own geometry: a 216 nm vertical M5 strap whose endmost wide-V4 cut rows
    sit flush with its ends, zero-enclosure 216 x 24 nm M4 landings (the
    tech LEF's ``M5_M4widePWR0p864``), and 34 nm M3 stack stubs beside a
    long signal M3 wire on the adjacent 36 nm track."""

    def __init__(self) -> None:
        self.layout = kdb.Layout()
        self.layout.dbu = DBU
        self.top = self.layout.create_cell("top")

    def box(self, layer, x0, y0, x1, y1) -> None:
        self.top.shapes(self.layout.layer(*layer)).insert(
            kdb.Box(_nm(x0), _nm(y0), _nm(x1), _nm(y1))
        )

    def stack(self, *, signal_m3: bool = True) -> None:
        # The strap and both endmost cut rows, ends flush (V4.M5.EN.2).
        self.box(M5, 0, 192, 216, 792)
        self.box(V4, 0, 192, 216, 216)
        self.box(V4, 0, 768, 216, 792)
        # Zero-enclosure M4 landings, exactly the cut rects (V4.M4.EN.1).
        self.box(M4, 0, 192, 216, 216)
        self.box(M4, 0, 768, 216, 792)
        # 34 nm stack stubs on the 108 nm M3 track, carrying their V3 cuts.
        self.box(V3, 99, 192, 117, 216)
        self.box(V3, 99, 768, 117, 792)
        self.box(M3, 99, 187, 117, 221)
        self.box(M3, 99, 763, 117, 797)
        # A signal M3 wire on the adjacent 144 nm track, 18 nm away: the
        # stub's 34 nm side is a short edge, so M3.S.2 (25 nm) applies.
        if signal_m3:
            self.box(M3, 135, 0, 153, 1000)

    def run(self, variant: str = "asap7A") -> dict:
        return repair_power_stack(
            kdb,
            layout=self.layout,
            top_cell=self.top,
            variant=variant,
            lef_to_gds=LEF_TO_GDS,
        )

    def merged(self, layer) -> list[kdb.Box]:
        index = self.layout.find_layer(*layer)
        region = kdb.Region(self.top.begin_shapes_rec(index)).merged()
        return sorted(
            (p.bbox() for p in region.each()), key=lambda b: (b.left, b.bottom)
        )


def _report_counts(report: dict) -> str:
    return (
        str(report["v4_m4_enclosure"])
        + str(report["v4_m5_enclosure"])
        + str(report["m3_stub_spacing"])
    )


def test_all_three_families_are_repaired_with_the_documented_geometry():
    b = _Builder()
    b.stack()
    report = b.run()
    assert report["status"] == "repaired"
    assert report["patches"] == 6
    assert report["v4_m4_enclosure"] == {"targets": 2, "repaired": 2, "left": 0}
    assert report["v4_m5_enclosure"] == {"targets": 2, "repaired": 2, "left": 0}
    assert report["m3_stub_spacing"]["targets"] == 2
    assert report["m3_stub_spacing"]["repaired"] == 2
    # M4 landings extended 12 nm left and right: >= 11 nm on the two
    # opposite sides the deck measures, at the same 24 nm track geometry.
    assert b.merged(M4) == [
        kdb.Box(_nm(-12), _nm(192), _nm(228), _nm(216)),
        kdb.Box(_nm(-12), _nm(768), _nm(228), _nm(792)),
    ]
    # The strap extended 12 nm past each endmost cut.
    assert b.merged(M5) == [kdb.Box(0, _nm(180), _nm(216), _nm(804))]
    # The stub lengthened past the deck's 36.25 nm short-edge bound, moving
    # its side edges into the long class where the adjacent-track 18 nm gap
    # is legal side-to-side.
    stubs = b.merged(M3)
    assert kdb.Box(_nm(99), _nm(177), _nm(117), _nm(221)) in stubs
    assert kdb.Box(_nm(99), _nm(753), _nm(117), _nm(797)) in stubs
    assert kdb.Box(_nm(135), 0, _nm(153), _nm(1000)) in stubs


def test_long_stub_and_enclosed_cut_are_left_alone():
    """Only the deck's own classifications trigger work: a stub already in
    the long class keeps its geometry, and a cut with M5 enclosure on the
    top/bottom pair is no M5 target."""
    b = _Builder()
    b.stack()
    # Grow both stubs past 36.25 nm and pull the strap ends 12 nm past the
    # endmost cuts -- now only the M4 family can fire.
    b.box(M3, 99, 173, 117, 221)
    b.box(M3, 99, 749, 117, 797)
    b.box(M5, 0, 180, 216, 804)
    report = b.run()
    assert report["v4_m4_enclosure"] == {"targets": 2, "repaired": 2, "left": 0}
    assert report["v4_m5_enclosure"]["targets"] == 0
    assert report["m3_stub_spacing"]["targets"] == 0


def test_foreign_conductor_in_the_patch_zone_blocks_the_repair():
    """An M4 landing close enough that the 12 nm extension would violate
    M4.S.2's 40 nm along spacing is reported in ``left``, not forced."""
    b = _Builder()
    b.stack()
    # 32 nm from the patch edge the repair would draw (228): inside
    # M4.S.2's 40 nm ring, so the upper landing's extension is blocked.
    b.box(M4, 260, 192, 400, 216)
    report = b.run()
    assert report["v4_m4_enclosure"] == {"targets": 2, "repaired": 1, "left": 1}
    # The blocked target's landing is untouched; the free one is extended.
    assert kdb.Box(0, _nm(192), _nm(216), _nm(216)) in b.merged(M4)
    assert kdb.Box(_nm(-12), _nm(768), _nm(228), _nm(792)) in b.merged(M4)


def test_m3_extension_blocked_below_falls_back_upward():
    b = _Builder()
    b.stack()
    # M3 31 nm below the lower stub's downward extension blocks that
    # direction; the repair must take the upward one instead.
    b.box(M3, 99, 120, 117, 146)
    report = b.run()
    assert report["m3_stub_spacing"]["repaired"] == 2
    assert kdb.Box(_nm(99), _nm(187), _nm(117), _nm(231)) in b.merged(M3)


def test_non_asap7_pdk_is_a_byte_identical_no_op(tmp_path):
    b = _Builder()
    b.stack()
    before = tmp_path / "before.gds"
    b.layout.write(str(before))
    report = b.run(variant="sky130A")
    assert report["status"] == "skipped"
    assert "not ASAP7" in report["reason"]
    after = tmp_path / "after.gds"
    b.layout.write(str(after))
    assert before.read_bytes() == after.read_bytes()


def test_missing_cut_layer_in_the_map_reports_a_named_skip():
    b = _Builder()
    b.stack()
    report = repair_power_stack(
        kdb,
        layout=b.layout,
        top_cell=b.top,
        variant="asap7A",
        lef_to_gds={k: v for k, v in LEF_TO_GDS.items() if k != "V4"},
    )
    assert report["status"] == "skipped"
    assert "V4" in report["reason"]


def test_thresholds_match_the_deck():
    """Static parity: the numbers this pass hard-codes are the ones
    ``decks/asap7.drc`` states (ASAP7 has no curated rule table to resolve
    them from, so this test keeps the two in step)."""
    text = DECK_PATH.read_text(encoding="utf-8")
    assert (
        'output("V4.M4.EN.1", "Table 3.15.1: min enclosure of V4 by M4 '
        'on two opposite sides 11 nm")' in text
    )
    assert _ENCLOSURE_NM == 11.0
    # The M1-M3 edge-length classes: "short" is with_length(nil, 36.25 nm).
    assert re.search(r"m_short = m\.edges\.with_length\(nil, 36\.25\.nm\)", text)
    assert _SHORT_EDGE_MAX_NM == 36.25
    assert _STUB_TARGET_NM > _SHORT_EDGE_MAX_NM
    # M3.S.2's tip-to-side threshold, the rule the stub repair escapes.
    assert re.search(r"m_short\.separation\(m_long, 25\.nm, projection\)", text)


@_needs_klayout
def test_counts_match_the_real_deck(tmp_path):
    """The differential oracle (issue #6909): the packaged ASAP7 deck flags
    the three families on the generated stack, the repair clears all three,
    and no other rule family's count rises."""
    b = _Builder()
    b.stack()
    before = tmp_path / "stack.gds"
    b.layout.write(str(before))

    def rule_counts(path) -> dict[str, int]:
        return {rule: count for rule, count in _deck_counts(path).items() if count}

    def _deck_counts(path) -> dict[str, int]:
        report = run_drc_klayout_engine(str(path), str(DECK_PATH))
        counts: dict[str, int] = {}
        for violation in report["violations"]:
            counts[violation["rule"]] = counts.get(violation["rule"], 0) + 1
        return counts

    before_counts = rule_counts(before)
    assert before_counts.get("V4.M4.EN.1") == 2
    assert before_counts.get("V4.M5.EN.2") == 2
    assert before_counts.get("M3.S.2") == 2

    assert b.run()["status"] == "repaired"
    after = tmp_path / "stack-repaired.gds"
    b.layout.write(str(after))
    after_counts = rule_counts(after)
    assert after_counts.get("V4.M4.EN.1", 0) == 0
    assert after_counts.get("V4.M5.EN.2", 0) == 0
    assert after_counts.get("M3.S.2", 0) == 0
    for rule, count in after_counts.items():
        if rule in ("V4.M4.EN.1", "V4.M5.EN.2", "M3.S.2"):
            continue
        assert count <= before_counts.get(rule, 0), (rule, before_counts, after_counts)
