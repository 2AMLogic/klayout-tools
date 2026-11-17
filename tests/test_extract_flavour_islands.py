"""Issue #5893: the per-flavour island split and the recognised-islands
diagnostic scanned whole-chip regions once per island (~95% of
``device_extraction`` wall time on routed gf180mcu blocks). The optimized
paths must keep their exact results: a marker-covered island is claimed
whole, a straddling island splits per gate, a gateless island is never
claimed, and the candidate islands for a flavour are exactly those touching
its own marker."""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools.decks import get_extraction_deck
from klayout_tools.extract import _flavour_recognised_islands
from klayout_tools.extract_flavour import _flavour_split_island


@pytest.fixture(scope="module")
def dualgate():
    """The gf180mcu ``Dualgate`` flavour: plain marker claim (no
    ``and_markers``/``not_markers``/``nfet_only``), so the claim test reduces
    to "touches the marker" and the whole-island shortcut may fire."""
    deck = get_extraction_deck("gf180mcu")
    (flavour,) = (f for f in deck.mos_flavours if f.flavour == "06v0")
    return flavour


def test_wholly_inside_marker_island_is_claimed_whole(dualgate):
    island = kdb.Region(kdb.Box(0, 0, 5000, 2000))
    gates_region = island & kdb.Region(kdb.Box(500, 0, 4500, 2000))
    claimed, remaining = _flavour_split_island(
        island, gates_region, dualgate, island, [], [], kdb.Region()
    )
    assert claimed == island
    assert remaining.is_empty()


def test_gateless_island_inside_marker_is_claimed_whole(dualgate):
    island = kdb.Region(kdb.Box(0, 0, 5000, 2000))
    # No gate geometry anywhere near the island: the island-level fallback
    # must reach the same verdict the per-gate walk would have.
    claimed, remaining = _flavour_split_island(
        island, kdb.Region(), dualgate, island, [], [], kdb.Region()
    )
    assert claimed == island
    assert remaining.is_empty()


def test_gateless_island_outside_marker_is_not_claimed(dualgate):
    island = kdb.Region(kdb.Box(0, 0, 5000, 2000))
    claimed, remaining = _flavour_split_island(
        island, kdb.Region(), dualgate, kdb.Region(), [], [], kdb.Region()
    )
    assert claimed.is_empty()
    assert remaining == island


def test_straddling_island_splits_per_gate(dualgate):
    # Two gates on one island; the marker covers only the right gate. The
    # island is not wholly inside the marker, so the per-gate walk claims
    # the marked half's area and the unmarked gate's area stays ordinary.
    island = kdb.Region(kdb.Box(0, 0, 5000, 2000))
    gate_left = kdb.Region(kdb.Box(500, 0, 1500, 2000))
    gate_right = kdb.Region(kdb.Box(3500, 0, 4500, 2000))
    gates_region = gate_left + gate_right
    claimed, remaining = _flavour_split_island(
        island, gates_region, dualgate, gate_right, [], [], kdb.Region()
    )
    # Coverage equality (the booleans may decompose polygons differently).
    assert (claimed ^ (island - gate_left)).is_empty()
    assert (remaining ^ (island - gate_right)).is_empty()


def test_recognised_islands_returns_whole_claimed_islands_only():
    # One island marked MVSD+LDMOS_XTOR with a gate (claimable by the
    # LDNMOS flavour), one island carrying LDMOS_XTOR only (not claimable:
    # the flavour's own marker, MVSD, is what the claim test touches). The
    # diagnostic's recognised region must be exactly the first island --
    # both under the candidate-island restriction and under the old
    # whole-island walk (issue #5893).
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    draw(22, 0, kdb.Box(0, 0, 2000, 1000))  # Comp, island A
    draw(30, 0, kdb.Box(200, 0, 1800, 1000))  # Poly2 gate on A
    draw(210, 0, kdb.Box(0, 0, 2000, 1000))  # MVSD over A
    draw(226, 0, kdb.Box(0, 0, 2000, 1000))  # LDMOS_XTOR over A
    draw(22, 0, kdb.Box(10000, 0, 12000, 1000))  # Comp, island B
    draw(30, 0, kdb.Box(10200, 0, 11800, 1000))  # Poly2 gate on B
    draw(226, 0, kdb.Box(10000, 0, 12000, 1000))  # LDMOS_XTOR over B

    deck = get_extraction_deck("gf180mcu")
    recognised = _flavour_recognised_islands(layout, top, deck, (226, 0))
    assert (recognised ^ kdb.Region(kdb.Box(0, 0, 2000, 1000))).is_empty()
