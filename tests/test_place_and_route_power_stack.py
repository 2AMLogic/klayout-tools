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
    _S1_NM,
    _S2_NM,
    _S3_NM,
    _S4_S5_NM,
    _S6_NM,
    _SHORT_EDGE_MAX_NM,
    _STUB_TARGET_NM,
    _TIP_MIN_NM,
    _m3_spacing_violations,
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


def _deck_counts(path) -> dict[str, int]:
    """Violations per rule the packaged ASAP7 deck reports on a GDS."""
    counts: dict[str, int] = {}
    for violation in run_drc_klayout_engine(str(path), str(DECK_PATH))["violations"]:
        counts[violation["rule"]] = counts.get(violation["rule"], 0) + 1
    return counts


def _m3_s_total(counts: dict[str, int]) -> int:
    return sum(n for rule, n in counts.items() if rule.startswith("M3.S."))


def _with_neighbours(*neighbours, signal_m3: bool = True) -> _Builder:
    """The shipped stack plus extra M3 boxes (nm) that are legal around it."""
    b = _Builder()
    b.stack(signal_m3=signal_m3)
    for box in neighbours:
        b.box(M3, *box)
    return b


def _stub_pair() -> _Builder:
    """Two stack stubs on one M3 track, 50 nm apart (y 100-134 and 184-218),
    with M3 under the lower one that blocks its downward extension -- so the
    lower stub must go up, toward the upper one."""
    b = _Builder()
    b.box(M5, 0, 90, 216, 250)
    for y in (105, 189):
        b.box(V4, 0, y, 216, y + 24)
        b.box(M4, 0, y, 216, y + 24)
        b.box(V3, 99, y, 117, y + 24)
        b.box(M3, 99, y - 5, 117, y + 29)
    b.box(M3, 99, 40, 117, 62)
    return b


def _mirror_count(b: _Builder) -> int:
    region = kdb.Region(b.top.begin_shapes_rec(b.layout.find_layer(*M3))).merged()
    return _m3_spacing_violations(kdb, region, DBU).count()


def _stub(y0: float, y1: float) -> kdb.Box:
    return kdb.Box(_nm(99), _nm(y0), _nm(117), _nm(y1))


#: Each layout is legal as drawn around the lower stub (99-117 x 187-221 nm)
#: except for the stub's own M3.S.2 against the 18 nm-away signal wire, and
#: each is a configuration a downward-only stub extension turns into a *new*
#: M3 spacing violation or leaves unrepaired.
_SCENARIOS = {
    # 16 nm below the stub, 18 nm to its left: the extended stub's new bottom
    # corner is 19 nm (Euclidean) from this one -- S.6 wants 20.
    "diagonal_corner": lambda: _with_neighbours((63, 100, 81, 171)),
    # Same, but the neighbour's top (178 nm) is 1 nm inside the strip, so its
    # short 34 nm side edge starts facing the now-long stub edge -- S.2 wants 25.
    "short_edge_enters_strip": lambda: _with_neighbours((63, 144, 81, 178)),
    # Diagonal neighbours both below and above: no safe direction.
    "no_safe_direction": lambda: _with_neighbours(
        (63, 100, 81, 171), (135, 238, 153, 300), signal_m3=False
    ),
    # The control: 28 nm of clearance, the downward strip is fine.
    "clear_neighbour": lambda: _with_neighbours((63, 100, 81, 160)),
    "stub_pair": _stub_pair,
}


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


@pytest.mark.parametrize(
    ("scenario", "lower_stub"),
    [
        # Downward would leave the left neighbour's corner 19 nm from the new
        # bottom corner (S.6): the pass takes the upward direction instead.
        ("diagonal_corner", _stub(187, 231)),
        # Downward would put the short neighbour's side edge 18 nm from the
        # now-long stub edge (S.2, 25 nm): upward again.
        ("short_edge_enters_strip", _stub(187, 231)),
        # The control: clear enough, so the usual downward strip.
        ("clear_neighbour", _stub(177, 221)),
    ],
)
def test_m3_extension_takes_the_direction_that_adds_no_violation(scenario, lower_stub):
    b = _SCENARIOS[scenario]()
    report = b.run()
    assert report["m3_stub_spacing"] == {"targets": 2, "repaired": 2, "left": 0}
    stubs = b.merged(M3)
    assert lower_stub in stubs
    assert _stub(753, 797) in stubs


def test_m3_stub_with_no_safe_direction_is_left_as_drawn():
    """Diagonal neighbours below and above the lower stub: neither strip is
    safe, so the stub is counted in ``left`` and not touched."""
    b = _SCENARIOS["no_safe_direction"]()
    report = b.run()
    assert report["m3_stub_spacing"] == {"targets": 2, "repaired": 1, "left": 1}
    stubs = b.merged(M3)
    assert _stub(187, 221) in stubs
    assert _stub(753, 797) in stubs


def test_a_drawn_strip_is_seen_by_the_next_stub():
    """The lower stub is forced upward to 144 nm; the upper stub's downward
    strip would then end 30 nm above it (S.4 wants 31). Checking each stub
    only against the layout as first read would draw it."""
    b = _SCENARIOS["stub_pair"]()
    report = b.run()
    assert report["m3_stub_spacing"] == {"targets": 2, "repaired": 2, "left": 0}
    assert b.merged(M3) == [_stub(40, 62), _stub(100, 144), _stub(184, 228)]


def test_m5_cut_with_a_blocked_short_side_is_left_not_repaired():
    """A cut short on both its top and bottom needs both strips: patching one
    side leaves V4.M5.EN.2 failing, so the cut is ``left``."""
    b = _Builder()
    b.box(M5, 0, 192, 216, 216)
    b.box(V4, 0, 192, 216, 216)
    b.box(M4, 0, 192, 216, 216)
    b.box(M5, 0, 240, 216, 280)  # inside the top strip's 40 nm along ring
    report = b.run()
    assert report["status"] == "violations"
    assert report["patches"] > 0
    assert report["v4_m5_enclosure"] == {"targets": 1, "repaired": 0, "left": 1}
    assert b.merged(M5) == [
        kdb.Box(0, _nm(180), _nm(216), _nm(216)),
        kdb.Box(0, _nm(240), _nm(216), _nm(280)),
    ]


def test_power_stack_reports_violations_when_all_targets_are_blocked():
    b = _Builder()
    for layer in (M4, M5, V4):
        b.box(layer, 0, 192, 216, 216)
    b.box(M4, -36, 192, -20, 216)
    b.box(M4, 236, 192, 252, 216)
    b.box(M5, 0, 240, 216, 280)
    b.box(M5, 0, 132, 216, 172)
    report = b.run()
    assert report["status"] == "violations"
    assert report["patches"] == 0
    assert report["v4_m4_enclosure"] == {"targets": 1, "repaired": 0, "left": 1}
    assert report["v4_m5_enclosure"] == {"targets": 1, "repaired": 0, "left": 1}


def test_power_stack_with_no_targets_is_clean():
    report = _Builder().run()
    assert report["status"] == "clean"
    assert report["patches"] == 0


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
    # The whole S.1-S.6 family the stub extension is re-checked against
    # (:func:`_m3_spacing_violations` mirrors it rule for rule).
    for rule in (
        f"m_long.space({_S1_NM:g}.nm, projection)",
        f"m_short.separation(m_long, {_S2_NM:g}.nm, projection)",
        f"m_tip.space({_S3_NM:g}.nm, projection)",
        f"m_narrow_tip.space({_S4_S5_NM:g}.nm, projection)",
        f"m_tip.separation(m_narrow_tip, {_S4_S5_NM:g}.nm, projection)",
        f"m.space({_S6_NM:g}.nm, euclidian, projection_limits(nil, 0.25.nm))",
        f"m_tip = m.edges.with_length({_TIP_MIN_NM:g}.nm, {_SHORT_EDGE_MAX_NM:g}.nm)",
        f"m_narrow_tip = m.edges.with_length(nil, {_TIP_MIN_NM:g}.nm)",
    ):
        assert rule in text, rule


@_needs_klayout
def test_counts_match_the_real_deck(tmp_path):
    """The differential oracle (issue #6909): the packaged ASAP7 deck flags
    the three families on the generated stack, the repair clears all three,
    and no other rule family's count rises."""
    b = _Builder()
    b.stack()
    before = tmp_path / "stack.gds"
    b.layout.write(str(before))

    before_counts = _deck_counts(before)
    assert before_counts.get("V4.M4.EN.1") == 2
    assert before_counts.get("V4.M5.EN.2") == 2
    assert before_counts.get("M3.S.2") == 2

    assert b.run()["status"] == "repaired"
    after = tmp_path / "stack-repaired.gds"
    b.layout.write(str(after))
    after_counts = _deck_counts(after)
    assert after_counts.get("V4.M4.EN.1", 0) == 0
    assert after_counts.get("V4.M5.EN.2", 0) == 0
    assert after_counts.get("M3.S.2", 0) == 0
    for rule, count in after_counts.items():
        if rule in ("V4.M4.EN.1", "V4.M5.EN.2", "M3.S.2"):
            continue
        assert count <= before_counts.get(rule, 0), (rule, before_counts, after_counts)


@_needs_klayout
@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_stub_extension_leaves_no_m3_spacing_violation_in_the_real_deck(
    scenario, tmp_path
):
    """Differential oracle for the M3 stub strip (issue #6909 review): every
    layout starts legal apart from the stub's own M3.S.2, so after the repair
    the deck must report no M3 spacing violation at all -- the per-rule
    "count did not rise" comparison cannot see a new violation traded for a
    fixed one inside the same family. The in-module S.1-S.6 mirror must agree
    with the deck's count before and after, since the check rests on it."""
    b = _SCENARIOS[scenario]()
    gds = tmp_path / "before.gds"
    b.layout.write(str(gds))
    before = _deck_counts(gds)
    assert _mirror_count(b) == _m3_s_total(before)

    b.run()
    gds = tmp_path / "after.gds"
    b.layout.write(str(gds))
    after = _deck_counts(gds)
    assert _m3_s_total(after) == 0, after
    assert _mirror_count(b) == 0
    for rule in ("V4.M4.EN.1", "V4.M5.EN.2"):
        assert after.get(rule, 0) <= before.get(rule, 0)
