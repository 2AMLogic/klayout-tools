"""Tests for the ASAP7 post-merge M4 track-grid repair (issue #7817).

Three tiers, mirroring ``tests/test_place_and_route_power_stack.py``'s split:

- unit checks on generated geometry (no binary): the reporter's measured
  shapes (issue #7817: a 24 nm M4 wire at y 53,988-54,012, an 84 nm column
  whose pad merge makes a 96 nm section, on-track neighbours) and exactly
  what the repair draws for each, that a foreign conductor in the patch
  ring blocks the patch, that an off-grid wire carrying its own V3 is left
  (the ``V3.M4.AUX.2`` protection), and that a non-ASAP7 PDK is a
  byte-identical no-op;
- a **static parity** check pinning the thresholds this pass hard-codes to
  the ones ``decks/asap7.drc`` states;
- a **differential oracle** gated on the ``klayout`` binary: the real deck
  over the same generated layout must flag the three families before the
  repair and clear them -- raising nothing else -- after it, and a target
  the pass reports in ``left`` must stay exactly what the deck flags.
"""

from __future__ import annotations

import re
import shutil

import klayout.db as kdb
import pytest

from klayout_tools.decks.asap7_rules import DECK_PATH
from klayout_tools.drc import run_drc_klayout_engine
from klayout_tools.place_and_route_m4_grid import (
    _ACROSS_SPACE_NM,
    _ALONG_SPACE_NM,
    _GRID_NM,
    _MAX_WIDTH_NM,
    _MIN_ALONG_WIDTH_NM,
    repair_m4_grid,
)

#: The deck's own nominal dbu (``asap7.lyt``): 0.25 nm.
DBU = 0.00025

M4 = (40, 0)
V3 = (35, 0)
LEF_TO_GDS = {"M4": M4, "V3": V3}

_needs_klayout = pytest.mark.skipif(
    shutil.which("klayout") is None,
    reason="klayout binary is not installed on this machine",
)


def _nm(value: float) -> int:
    """Nanometres at the DRM's 1x drawing scale -> database units."""
    return int(round(value / 1000.0 / DBU))


class _Builder:
    """M4-only geometry at the reporter's measured coordinates (issue
    #7817): a 24 nm stub spanning y 53,988-54,012 -- off the 24 nm grid on
    both edges and off the 48 nm track grid, so ``M4.AUX.1`` and
    ``M4.AUX.2`` both flag it -- beside an on-track wire at y
    54,000-54,024, plus an ~84 nm x 900 nm column whose via-pad merge makes
    one section 96 nm wide (``M4.W.3``'s even-multiple class) and leaves
    the 84 nm edge off grid (``M4.AUX.1``)."""

    def __init__(self) -> None:
        self.layout = kdb.Layout()
        self.layout.dbu = DBU
        self.top = self.layout.create_cell("top")

    def box(self, layer, x0, y0, x1, y1) -> None:
        self.top.shapes(self.layout.layer(*layer)).insert(
            kdb.Box(_nm(x0), _nm(y0), _nm(x1), _nm(y1))
        )

    def reporter_shapes(self, *, blocker: bool = False, own_v3: bool = False) -> None:
        # The off-grid 24 nm stub (M4.AUX.1 + M4.AUX.2).
        self.box(M4, 0, 53988, 300, 54012)
        # The on-track control wire: 24 nm on the 48 nm track grid, clean.
        self.box(M4, 600, 54000, 900, 54024)
        # The 84 nm column and its pad merge: a 96 nm (M4.W.3) section
        # over x 2000-2088, an off-grid 84 nm edge (M4.AUX.1) beyond it.
        self.box(M4, 2000, 0, 2900, 84)
        self.box(M4, 2000, 84, 2088, 96)
        if blocker:
            # 20 nm from the stub's end: inside the repair ring's 40 nm
            # along margin (and itself an M4.S.2 violation), so no patch
            # may draw toward it.
            self.box(M4, 320, 54000, 500, 54024)
        if own_v3:
            # A V3 whose horizontal edges coincide with the stub's: the
            # only V3.M4.AUX.2-clean configuration. Growing the stub past
            # the V3 edges would newly flag that rule, so the stub is left.
            self.box(V3, 100, 53988, 118, 54012)

    def run(self, variant: str = "asap7A") -> dict:
        return repair_m4_grid(
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


def _deck_counts(path) -> dict[str, int]:
    """Violations per rule the packaged ASAP7 deck reports on a GDS."""
    counts: dict[str, int] = {}
    for violation in run_drc_klayout_engine(str(path), str(DECK_PATH))["violations"]:
        counts[violation["rule"]] = counts.get(violation["rule"], 0) + 1
    return counts


def test_reporter_shapes_are_repaired_with_documented_geometry():
    """Every new edge lands on the 24 nm grid, every repaired band's height
    is an odd multiple of it (so ``M4.W.3``'s even-multiple classes and
    ``W.2``'s maximum are both escaped), the x extent never moves, and the
    on-track control wire is untouched."""
    b = _Builder()
    b.reporter_shapes()
    report = b.run()
    assert report["status"] == "repaired"
    assert report["targets"] == 2
    assert report["repaired"] == 2
    assert report["left"] == 0
    assert report["patches"] == 4  # two bands x (grow-below + grow-above)
    assert b.merged(M4) == [
        # The stub: the 72 nm (3 x 24) band y 53,952-54,024.
        kdb.Box(0, _nm(53952), _nm(300), _nm(54024)),
        # The control wire, byte-for-byte as drawn.
        kdb.Box(_nm(600), _nm(54000), _nm(900), _nm(54024)),
        # The column + pad merge: the 120 nm (5 x 24) band y -24..96,
        # a perfect rectangle (the notch and the bend go with it).
        kdb.Box(_nm(2000), _nm(-24), _nm(2900), _nm(96)),
    ]


def test_a_pure_w3_rectangle_is_a_target_and_is_repaired():
    """A 96 nm on-grid rectangle carries no AUX marker of its own -- only
    the even-multiple width -- and is still repaired."""
    b = _Builder()
    b.box(M4, 0, 0, 300, 96)
    report = b.run()
    assert report == {
        "status": "repaired",
        "reason": None,
        "patches": 1,
        "targets": 1,
        "repaired": 1,
        "left": 0,
    }
    # The 120 nm (5 x 24) band starting one grid step below: the most
    # balanced of the two candidates (the [0, 120] one grows 24 nm up
    # instead, and sorts after by lowest-bottom).
    assert b.merged(M4) == [kdb.Box(0, _nm(-24), _nm(300), _nm(96))]


def test_foreign_conductor_in_the_spacing_ring_blocks_the_repair():
    """An on-track wire 20 nm from the stub's end sits inside the band's
    M4.S.2 ring, so no patch may draw: the target is counted in ``left``
    and its geometry is untouched."""
    b = _Builder()
    b.reporter_shapes(blocker=True)
    report = b.run()
    assert report["status"] == "violations"
    assert report["targets"] == 2
    assert report["repaired"] == 1  # the column/pad target is still free
    assert report["left"] == 1
    stubs = [box for box in b.merged(M4) if box.left == 0]
    assert stubs == [kdb.Box(0, _nm(53988), _nm(300), _nm(54012))]


def test_off_grid_wire_carrying_its_own_v3_is_left():
    """Growing the stub past its V3's coincident horizontal edges would
    newly flag ``V3.M4.AUX.2`` (V3 must be exactly as wide as the M4 it
    lands on), so the wire is left for via geometry this pass does not
    touch -- the enclosure around the V3 is unchanged."""
    b = _Builder()
    b.reporter_shapes(own_v3=True)
    report = b.run()
    assert report["targets"] == 2
    assert report["left"] == 1
    assert b.merged(M4) == [
        kdb.Box(0, _nm(53988), _nm(300), _nm(54012)),
        kdb.Box(_nm(600), _nm(54000), _nm(900), _nm(54024)),
        kdb.Box(_nm(2000), _nm(-24), _nm(2900), _nm(96)),
    ]
    assert b.merged(V3) == [kdb.Box(_nm(100), _nm(53988), _nm(118), _nm(54012))]


def test_wire_merged_with_an_on_track_neighbour_becomes_one_clean_band():
    """The reporter's cluster: the off-grid stub already overlaps the
    on-track wire, so the merged polygon mixes off- and on-grid sections.
    The repair bands the whole conductor -- one rectangle, every edge on
    the grid, no bend (``M4.AUX.3`` clears as a side effect)."""
    b = _Builder()
    b.box(M4, 0, 53988, 300, 54012)
    b.box(M4, 250, 54000, 700, 54024)
    report = b.run()
    assert report == {
        "status": "repaired",
        "reason": None,
        "patches": 2,
        "targets": 1,
        "repaired": 1,
        "left": 0,
    }
    assert b.merged(M4) == [kdb.Box(0, _nm(53976), _nm(700), _nm(54048))]


def test_clean_layout_reports_clean_and_draws_nothing():
    b = _Builder()
    b.box(M4, 0, 54000, 300, 54024)
    b.box(M4, 600, 0, 900, 72)  # odd multiple, on-grid
    report = b.run()
    assert report["status"] == "clean"
    assert report["patches"] == 0
    assert b.merged(M4) == [
        kdb.Box(0, _nm(54000), _nm(300), _nm(54024)),
        kdb.Box(_nm(600), 0, _nm(900), _nm(72)),
    ]


def test_absent_m4_layer_in_the_layout_reports_clean():
    b = _Builder()
    b.box(V3, 0, 0, 18, 24)
    report = b.run()
    assert report["status"] == "clean"
    assert report["patches"] == 0


def test_non_asap7_pdk_is_a_byte_identical_no_op(tmp_path):
    b = _Builder()
    b.reporter_shapes()
    before = tmp_path / "before.gds"
    b.layout.write(str(before))
    report = b.run(variant="sky130A")
    assert report["status"] == "skipped"
    assert "not ASAP7" in report["reason"]
    after = tmp_path / "after.gds"
    b.layout.write(str(after))
    assert before.read_bytes() == after.read_bytes()


def test_missing_m4_layer_in_the_map_reports_a_named_skip():
    b = _Builder()
    b.reporter_shapes()
    report = repair_m4_grid(
        kdb,
        layout=b.layout,
        top_cell=b.top,
        variant="asap7A",
        lef_to_gds={k: v for k, v in LEF_TO_GDS.items() if k != "M4"},
    )
    assert report["status"] == "skipped"
    assert report["reason"] == "no M4 layer in the KLayout layer map"


def test_rerun_is_deterministic(tmp_path):
    """Two fresh builds of the reporter's layout repair to the same GDS,
    bytes identical (the pass sorted its targets and candidates)."""
    outs = []
    for i in range(2):
        b = _Builder()
        b.reporter_shapes()
        assert b.run()["status"] == "repaired"
        path = tmp_path / f"run{i}.gds"
        b.layout.write(str(path))
        outs.append(path.read_bytes())
    assert outs[0] == outs[1]


def test_thresholds_match_the_deck():
    """Static parity: the numbers this pass hard-codes are the ones
    ``decks/asap7.drc`` states (ASAP7 has no curated rule table to resolve
    them from, so this test keeps the two in step)."""
    text = DECK_PATH.read_text(encoding="utf-8")
    # The M4-M7 loop's own per-layer table: M4's grid 24 nm and W.2
    # maximum 480 nm.
    assert re.search(r'\{ "M4" => \[m4, :h, 24, 480\]', text)
    assert _GRID_NM == 24.0
    assert _MAX_WIDTH_NM == 480.0
    # W.5's along minimum and S.2's along spacing, shared by the family.
    assert "along_width(m, dir, 44.nm)" in text
    assert _MIN_ALONG_WIDTH_NM == 44.0
    assert "along_space(m, dir, 40.nm)" in text
    assert _ALONG_SPACE_NM == 40.0
    # S.1's across spacing is the layer grid itself (24 nm for M4).
    assert "across_space(m, dir, g.nm)" in text
    assert _ACROSS_SPACE_NM == _GRID_NM
    # W.3's even-multiple loop, from the first even multiple up to wmax.
    assert "(2 * g).step(wmax, 2 * g).map do |w|" in text
    # AUX.1's 24 nm grid (offset 0) and AUX.2's 48 nm track grid.
    assert "across_ongrid(m, dir, g.nm)" in text
    assert "across_ongrid(stretched, dir, (2 * g).nm)" in text
    # The V3 exact-width rule whose mirror protects a V3-carrying wire.
    assert "(horz(v3.overlapping(m4)) - horz(m4))" in text


@_needs_klayout
def test_counts_match_the_real_deck(tmp_path):
    """The differential oracle (issue #7817): the packaged ASAP7 deck
    flags the three reported families on the generated layout -- the
    reporter's measured shapes -- the repair clears them, and no other
    rule family's count rises."""
    b = _Builder()
    b.reporter_shapes()
    before = tmp_path / "reporter.gds"
    b.layout.write(str(before))

    before_counts = _deck_counts(before)
    assert before_counts.get("M4.AUX.1", 0) > 0
    assert before_counts.get("M4.AUX.2", 0) > 0
    assert before_counts.get("M4.W.3", 0) > 0

    assert b.run()["status"] == "repaired"
    after = tmp_path / "reporter-repaired.gds"
    b.layout.write(str(after))
    after_counts = _deck_counts(after)
    for rule in ("M4.AUX.1", "M4.AUX.2", "M4.W.3"):
        assert after_counts.get(rule, 0) == 0, (rule, after_counts)
    for rule, count in after_counts.items():
        assert count <= before_counts.get(rule, 0), (rule, before_counts, after_counts)


@_needs_klayout
def test_wire_merged_with_an_on_track_neighbour_clears_the_deck(tmp_path):
    b = _Builder()
    b.box(M4, 0, 53988, 300, 54012)
    b.box(M4, 250, 54000, 700, 54024)
    gds = tmp_path / "merged.gds"
    b.layout.write(str(gds))
    assert _deck_counts(gds).get("M4.AUX.1", 0) > 0
    assert b.run()["status"] == "repaired"
    gds = tmp_path / "merged-repaired.gds"
    b.layout.write(str(gds))
    assert _deck_counts(gds) == {}


@_needs_klayout
def test_blocked_target_stays_what_the_deck_flags(tmp_path):
    """A target counted in ``left`` is not forced: with the column/pad
    target absent, the blocked stub is the only target, nothing is drawn,
    and the deck's counts are identical before and after the run."""
    b = _Builder()
    b.box(M4, 0, 53988, 300, 54012)
    b.box(M4, 320, 54000, 500, 54024)
    before = tmp_path / "blocked.gds"
    b.layout.write(str(before))
    before_counts = _deck_counts(before)
    assert before_counts.get("M4.AUX.1", 0) > 0
    assert before_counts.get("M4.AUX.2", 0) > 0

    report = b.run()
    assert report == {
        "status": "violations",
        "reason": None,
        "patches": 0,
        "targets": 1,
        "repaired": 0,
        "left": 1,
    }
    after = tmp_path / "blocked-repaired.gds"
    b.layout.write(str(after))
    assert _deck_counts(after) == before_counts


@_needs_klayout
def test_v3_carrying_target_is_left_without_raising_v3_m4_aux2(tmp_path):
    """The refusal the V3 mirror guards: leaving the wire keeps the deck's
    V3.M4.AUX.2 count at zero while the M4 families it carries stay
    flagged (they are the honest ``left``)."""
    b = _Builder()
    b.reporter_shapes(own_v3=True)
    gds = tmp_path / "v3.gds"
    b.layout.write(str(gds))
    assert _deck_counts(gds).get("V3.M4.AUX.2", 0) == 0

    report = b.run()
    assert report["left"] == 1
    gds = tmp_path / "v3-repaired.gds"
    b.layout.write(str(gds))
    counts = _deck_counts(gds)
    assert counts.get("V3.M4.AUX.2", 0) == 0
    assert counts.get("M4.AUX.1", 0) > 0


@pytest.mark.parametrize("contact", ["V3", "V4", "M4.pin"])
def test_repair_preserves_foreign_contacts(contact, tmp_path):
    from klayout_tools.extract import run_extract

    b = _Builder()
    b.box(M4, 0, 108, 300, 132)
    child = b.layout.create_cell("NEIGHBOUR")
    b.top.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans()))
    b.top.shapes(b.layout.layer(40, 251)).insert(kdb.Text("SIGNAL", _nm(240), _nm(120)))
    if contact == "M4.pin":
        child.shapes(b.layout.layer(40, 251)).insert(
            kdb.Text("OTHER", _nm(108), _nm(100))
        )
    else:
        via, other = (45, 50) if contact == "V4" else (35, 30)
        child.shapes(b.layout.layer(via, 0)).insert(
            kdb.Box(_nm(96), _nm(80), _nm(120), _nm(104))
        )
        b.box((other, 0), 96, 0, 120, 250)
        child.shapes(b.layout.layer(other, 251)).insert(
            kdb.Text("OTHER", _nm(108), _nm(40))
        )
    before = b.merged(M4)
    assert b.run()["left"] == 1
    assert b.merged(M4) == before
    if contact == "V4":
        # Flatten this tiny fixture so the extractor exposes both top pins.
        b.top.flatten(-1, False)
        path, netlist = tmp_path / "untouched.gds", tmp_path / "untouched.spi"
        b.layout.write(str(path))
        result = run_extract(str(path), "asap7", top="top", output=str(netlist))
        assert result["warnings"] == []
        pins = next(
            line.split()[2:]
            for line in netlist.read_text().splitlines()
            if line.upper().startswith(".SUBCKT TOP ")
        )
        assert set(pins) == {"SIGNAL", "OTHER"}


def test_repair_uses_a_band_clear_of_a_foreign_cut():
    b = _Builder()
    b.box(M4, 0, 108, 300, 132)
    b.box((45, 0), 96, 68, 120, 92)
    # The first candidate (72..144 nm) would reach that V4. The next
    # band remains clear, and a different V4 already on M4 stays connected.
    b.box((45, 0), 192, 108, 216, 132)
    assert b.run()["repaired"] == 1
    assert b.merged(M4) == [kdb.Box(_nm(0), _nm(96), _nm(300), _nm(168))]
