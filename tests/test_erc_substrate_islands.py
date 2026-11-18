"""Substrate-continuity pass: isolation-island geometry and decks.

The sky130 dnwell-island suite (#5344: an island counts only when an n-well
ring closes it) and the island-closure helper pins, split out of
``tests/test_erc_substrate.py``. Shared fixtures live in
:mod:`helpers.erc_substrate_fixtures`. Design:
``docs/design/erc-substrate-continuity.md`` section 3.
"""

from __future__ import annotations

import klayout.db as kdb

from helpers.erc_substrate_fixtures import (
    DBU,
    _box,
    _ptie,
    _rail,
    _run,
    _shorts,
    _spec,
    _um,
)

# --- sky130: dnwell island counts only when an n-well ring closes it (#5344) --


def _sky130_two_rails():
    """Like ``_two_rails`` but on sky130 layers: n-well is 64/20, dnwell 64/18."""
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")
    _rail(layout, top, 0, "VSS")
    _rail(layout, top, 5, "VSS2")
    _box(top, layout, (64, 20), 0, 20, 20, 30)  # unrelated n-well
    return layout, top


def _sky130_ties():
    return [
        _ptie("p1", "VSS", well_complement_of=["64/20"]),
        _ptie("p2", "VSS2", well_complement_of=["64/20"]),
    ]


def _ring(top, layout, x0, y0, x1, y1, w=1.0):
    for box in (
        (x0, y0, x1, y0 + w),
        (x0, y1 - w, x1, y1),
        (x0, y0, x0 + w, y1),
        (x1 - w, y0, x1, y1),
    ):
        _box(top, layout, (64, 20), *box)


def _sky130_bodies(tmp_path, layout):
    report = _run(
        tmp_path,
        layout,
        _spec(_sky130_ties()),
        deck="sky130",
    )
    return report, _shorts(report)


def test_sky130_closed_nwell_ring_isolates_dnwell_island(tmp_path):
    layout, top = _sky130_two_rails()
    _box(top, layout, (64, 18), -1, 4, 21, 7)
    _ring(top, layout, -2, 3, 22, 8)  # ring enclosing the dnwell
    report, shorts = _sky130_bodies(tmp_path, layout)
    assert shorts == []
    ids = {b["id"] for b in report["provenance"]["substrate_bodies"]}
    assert ids == {"vsubs", "vsubs_iso0"}


def test_sky130_unclosed_ring_leaves_island_unisolated(tmp_path):
    layout, top = _sky130_two_rails()
    _box(top, layout, (64, 18), -1, 4, 21, 7)
    _ring(top, layout, -2, 3, 22, 8)
    # Open a gap in the ring's left side.
    region = kdb.Region(top.shapes(layout.layer(64, 20)))
    region -= kdb.Region(kdb.Box(_um(-3), _um(4.5), _um(-0.5), _um(6.5)))
    top.shapes(layout.layer(64, 20)).clear()
    top.shapes(layout.layer(64, 20)).insert(region)
    report, shorts = _sky130_bodies(tmp_path, layout)
    assert len(shorts) == 1
    assert shorts[0]["body"]["id"] == "vsubs"


def test_sky130_dnwell_without_any_nwell_is_not_isolated(tmp_path):
    layout, top = _sky130_two_rails()
    layout.clear_layer(layout.layer(64, 20))
    _box(top, layout, (64, 20), 0, 20, 20, 30)
    _box(top, layout, (64, 18), -1, 4, 21, 7)
    _, shorts = _sky130_bodies(tmp_path, layout)
    assert len(shorts) == 1


def test_sky130_no_dnwell_is_one_substrate(tmp_path):
    layout, _ = _sky130_two_rails()
    report, shorts = _sky130_bodies(tmp_path, layout)
    assert len(shorts) == 1
    assert {b["id"] for b in report["provenance"]["substrate_bodies"]} == {"vsubs"}


def test_sky130_nested_islands_are_numbered_independently(tmp_path):
    layout, top = _sky130_two_rails()
    _box(top, layout, (64, 18), -1, 4, 21, 7)
    _ring(top, layout, -2, 3, 22, 8)
    _box(top, layout, (64, 18), -1, -1, 21, 2)  # closed island around VSS
    _ring(top, layout, -2, -2, 22, 2.5)
    report, _ = _sky130_bodies(tmp_path, layout)
    ids = {b["id"] for b in report["provenance"]["substrate_bodies"]}
    assert "vsubs_iso0" in ids and "vsubs_iso1" in ids


def test_island_closure_helper_empty_and_abutting():
    from klayout_tools.erc_islands import is_ring_closed, sorted_islands

    island = kdb.Region(kdb.Box(0, 0, 100, 100))
    assert not is_ring_closed(island, kdb.Region())
    assert sorted_islands(kdb.Region(), kdb.Region()) == []
    abutting = kdb.Region(kdb.Box(-20, -20, 120, 120)) - kdb.Region(
        kdb.Box(0, 0, 100, 100)
    )
    assert is_ring_closed(island, abutting)
    assert len(sorted_islands(island, abutting)) == 1
    assert sorted_islands(island, None) != []


def test_sky130_deck_declares_closure_gated_dnwell_isolation():
    """#5344: sky130 declares dnwell (64/18) as ``substrate_isolation`` and
    gates islands on an n-well ring; gf180mcu keeps every island."""
    from klayout_tools.decks.gf180mcu import EXTRACTION_DECK as GF180
    from klayout_tools.decks.sky130 import EXTRACTION_DECK as SKY130

    assert SKY130.substrate_isolation == (64, 18)
    assert SKY130.substrate_isolation_needs_nwell_ring is True
    assert GF180.substrate_isolation_needs_nwell_ring is False
