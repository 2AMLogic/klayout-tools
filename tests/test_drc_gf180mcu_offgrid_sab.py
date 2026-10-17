"""Tests for the gf180mcu deck's manufacturing-grid (`OFFGRID`) rules and
salicide-block (`SB.*`) rules (issue #3001).

The gap these close: `klt drc --deck gf180mcu` reported `clean` for cells the
PDK's own runset fails -- `contact_OFFGRID` on a guard-ring contact and
`SB.4`/`SB.10` on a SAB-blocked poly resistor -- because the curated deck
transcribed none of `geom.drc`'s `OFFGRID` group nor `sab.drc`'s `SB.*` group.
The gf180mcu twin of the sky130 report #2642 (`tests/test_drc_offgrid.py`).
"""

from __future__ import annotations

import klayout.db as kdb

from klayout_tools.decks import get_deck
from klayout_tools.decks.gf180mcu import (
    _OFFGRID_ROWS,
    LAYER_NAMES,
    OFFGRID_GRID_UM,
    OFFGRID_UNCHECKED_LAYERS,
)
from klayout_tools.drc import run_drc

SAB = (49, 0)
CONTACT = (33, 0)
POLY2 = (30, 0)


def _write(path, shapes, dbu: float = 0.001) -> str:
    layout = kdb.Layout()
    layout.dbu = dbu
    top = layout.create_cell("TOP")
    for (layer, datatype), items in shapes.items():
        index = layout.layer(layer, datatype)
        layout.set_info(index, kdb.LayerInfo(layer, datatype))
        for item in items:
            top.shapes(index).insert(item)
    layout.write(str(path))
    return str(path)


def test_offgrid_contact_vertex_is_flagged(tmp_path):
    """The reported reproducer: a contact whose right edge is 2 dbu off the
    0.005um grid is reported under `contact.ongrid.1` (one violation per
    off-grid vertex); the same contact on-grid is clean."""
    off = _write(tmp_path / "off.gds", {CONTACT: [kdb.Box(0, 0, 222, 220)]})
    on = _write(tmp_path / "on.gds", {CONTACT: [kdb.Box(0, 0, 220, 220)]})

    report = run_drc(off, "gf180mcu")
    assert report["rule_counts"]["contact.ongrid.1"] == 2
    flagged = [v for v in report["violations"] if v["rule"] == "contact.ongrid.1"]
    assert {v["check"] for v in flagged} == {"ongrid"}
    assert {v["layer"] for v in flagged} == {"Contact"}

    clean = run_drc(on, "gf180mcu")
    # Issue #3792: the deck discloses its untranscribed ESD.*/GR.* rules as
    # skipped, so a violation-free run is `clean_partial`, never `clean`.
    assert clean["status"] == "clean_partial"
    assert "contact.ongrid.1" in clean["coverage"]["rules_checked"]


def test_offgrid_vertex_is_flagged_on_every_transcribed_layer(tmp_path):
    """Every `_OFFGRID_ROWS` entry is wired end to end: an off-grid rectangle
    on each row's layer trips that row's own rule (catches a mistranscribed
    `(layer, datatype)`, which would silently check a layer nobody draws)."""
    for stem, layer, _runset_name in _OFFGRID_ROWS:
        path = _write(
            tmp_path / f"offgrid_{stem}.gds",
            {layer: [kdb.Box(0, 0, 1503, 1000)]},
        )
        report = run_drc(path, "gf180mcu")
        assert report["rule_counts"].get(f"{stem}.ongrid.1") == 2, stem


def test_offgrid_table_matches_deck_and_layer_names():
    """Rule table, deck and layer-name map agree; the grid is the runset's."""
    deck = {rule.id: rule for rule in get_deck("gf180mcu")}
    assert OFFGRID_GRID_UM == 0.005
    for stem, layer, _runset_name in _OFFGRID_ROWS:
        rule = deck[f"{stem}.ongrid.1"]
        assert rule.check == "ongrid"
        assert rule.layer == layer
        assert rule.grid_um == OFFGRID_GRID_UM
        assert layer in LAYER_NAMES
    # Unchecked layers are named explicitly, not inferred.
    assert "lvpwell" in OFFGRID_UNCHECKED_LAYERS


def test_grid_rule_without_angle_half():
    """gf180mcu's runset has no `with_angle` call; no angle rule is emitted."""
    assert not [r for r in get_deck("gf180mcu") if r.check == "angle"]


def _sb_report(tmp_path, name, shapes):
    return run_drc(_write(tmp_path / f"{name}.gds", shapes), "gf180mcu")


def test_sab_to_contact_spacing_sb4(tmp_path):
    """SAB 0.10um from a contact reports SB.4; 0.20um is clean."""
    sab = {SAB: [kdb.Box(0, 0, 1000, 1000)]}
    near = _sb_report(
        tmp_path, "near", {**sab, CONTACT: [kdb.Box(1100, 400, 1320, 620)]}
    )
    far = _sb_report(tmp_path, "far", {**sab, CONTACT: [kdb.Box(1200, 400, 1420, 620)]})

    assert near["rule_counts"] == {"sab.separation.contact.1": 1}
    assert far["status"] == "clean_partial"
    assert "sab.separation.contact.1" in far["coverage"]["rules_checked"]


def test_poly_extension_beyond_sab_sb10(tmp_path):
    """Poly2 extending 0.2um past the SAB edge reports SB.10; 0.3um is clean.

    The SAB is deliberately taller than the poly (it overhangs sideways, as
    on a real SAB-blocked resistor): that overhang must not be reported.
    """
    sab = {SAB: [kdb.Box(0, 0, 1000, 1000)]}
    short = _sb_report(
        tmp_path, "short", {**sab, POLY2: [kdb.Box(-200, 300, 1200, 700)]}
    )
    ok = _sb_report(tmp_path, "ok", {**sab, POLY2: [kdb.Box(-300, 300, 1300, 700)]})

    assert short["rule_counts"] == {"poly2.enclosing.sab.1": 2}
    assert ok["status"] == "clean_partial"


def test_sab_width_and_space(tmp_path):
    narrow = _sb_report(tmp_path, "narrow", {SAB: [kdb.Box(0, 0, 300, 1000)]})
    close = _sb_report(
        tmp_path,
        "close",
        {SAB: [kdb.Box(0, 0, 1000, 1000), kdb.Box(1300, 0, 2300, 1000)]},
    )

    assert narrow["rule_counts"] == {"sab.width.1": 1}
    assert close["rule_counts"] == {"sab.space.1": 1}


def test_stream_without_sab_is_clean_with_sb_rules_inapplicable(tmp_path):
    report = _sb_report(tmp_path, "nosab", {POLY2: [kdb.Box(0, 0, 1400, 400)]})

    assert report["status"] == "clean_partial"
    assert "poly2.enclosing.sab.1" in report["coverage"]["rules_skipped"]
    assert "sab.width.1" in report["coverage"]["rules_skipped"]


def test_grid_incommensurate_dbu_is_a_reported_skip(tmp_path):
    """A 6nm dbu cannot express the 5nm grid: the rule is skipped and recorded
    (`grid_not_representable`), never mis-flagged."""
    path = _write(tmp_path / "odd.gds", {CONTACT: [kdb.Box(0, 0, 37, 37)]}, dbu=0.006)

    report = run_drc(path, "gf180mcu")

    assert {"id": "contact.ongrid.1", "reason": "grid_not_representable"} in report[
        "coverage"
    ]["skipped"]
    assert report["rule_counts"].get("contact.ongrid.1") is None
