"""Tests for the gf180mcu deck's ESD implant (`ESD.*`) and guard-ring marker
(`GR.*`) rules and the disclosure of the upstream rules not transcribed
(issue #3792).

The gap these close: `klt drc --deck gf180mcu` reported `clean` for layouts
the PDK's own `esd.rb` / `guard_ring.rb` fail, because the curated deck had no
rule for the ESD (24/0) or GUARD_RING_MK (167/5) layers and did not say so.
"""

from __future__ import annotations

import klayout.db as kdb

from klayout_tools.decks import get_deck, get_untranscribed_rules
from klayout_tools.decks.gf180mcu import LAYER_NAMES, UNTRANSCRIBED_RULES
from klayout_tools.drc import SKIP_REASON_NOT_TRANSCRIBED, run_drc

ESD = (24, 0)
COMP = (22, 0)
GUARD_RING_MK = (167, 5)


def _write(path, shapes) -> str:
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    for (layer, datatype), items in shapes.items():
        index = layout.layer(layer, datatype)
        layout.set_info(index, kdb.LayerInfo(layer, datatype))
        for item in items:
            top.shapes(index).insert(item)
    layout.write(str(path))
    return str(path)


def test_new_rules_are_registered_with_provenance():
    deck = {rule.id: rule for rule in get_deck("gf180mcu")}
    expected = {
        "esd.width.1": "ESD.1",
        "esd.space.1": "ESD.2",
        "esd.overlap.comp.1": "ESD.4b",
        "esd.area.1": "ESD.5a",
        "esd.holes_area.1": "ESD.5b",
        "esd.separation.nplus.1": "ESD.8",
        "esd.separation.pplus.1": "ESD.8",
        "comp.separation.guard_ring_mk.1": "GR.2",
        "metaltop.separation.guard_ring_mk.1": "GR.2",
    }
    for rule_id, upstream in expected.items():
        provenance = deck[rule_id].provenance
        assert provenance is not None, rule_id
        assert provenance.rule_id == upstream
        assert provenance.source_path.endswith(("/esd.rb", "/guard_ring.rb")), rule_id
    assert LAYER_NAMES[ESD] == "ESD"
    assert LAYER_NAMES[GUARD_RING_MK] == "GUARD_RING_MK"


def test_esd_width_and_space_violations_are_flagged(tmp_path):
    # 0.5um-wide implant (< 0.6); two 1um-wide implants 0.5um apart (< 0.6).
    narrow = _write(tmp_path / "narrow.gds", {ESD: [kdb.Box(0, 0, 500, 2000)]})
    close = _write(
        tmp_path / "close.gds",
        {ESD: [kdb.Box(0, 0, 1000, 1000), kdb.Box(1500, 0, 2500, 1000)]},
    )
    assert run_drc(narrow, "gf180mcu")["rule_counts"]["esd.width.1"] >= 1
    assert run_drc(close, "gf180mcu")["rule_counts"]["esd.space.1"] >= 1


def test_comp_too_close_to_guard_ring_marker_is_flagged(tmp_path):
    near = _write(
        tmp_path / "near.gds",
        {
            COMP: [kdb.Box(0, 0, 1000, 1000)],
            GUARD_RING_MK: [kdb.Box(6000, 0, 20000, 20000)],  # 5um away
        },
    )
    far = _write(
        tmp_path / "far.gds",
        {
            COMP: [kdb.Box(0, 0, 1000, 1000)],
            GUARD_RING_MK: [kdb.Box(11_000, 0, 30_000, 20_000)],  # 10um away
        },
    )
    assert (
        run_drc(near, "gf180mcu")["rule_counts"]["comp.separation.guard_ring_mk.1"] >= 1
    )
    conforming = run_drc(far, "gf180mcu")
    assert "comp.separation.guard_ring_mk.1" not in conforming["rule_counts"]
    assert "comp.separation.guard_ring_mk.1" in conforming["coverage"]["rules_checked"]


def test_conforming_esd_is_clean_of_esd_rules(tmp_path):
    ok = _write(tmp_path / "ok.gds", {ESD: [kdb.Box(0, 0, 1000, 1000)]})
    report = run_drc(ok, "gf180mcu")
    assert not [r for r in report["rule_counts"] if r.startswith("esd.")]
    assert "esd.width.1" in report["coverage"]["rules_checked"]


def test_untranscribed_rules_are_disclosed_even_with_no_esd_shapes(tmp_path):
    path = _write(tmp_path / "plain.gds", {COMP: [kdb.Box(0, 0, 1000, 1000)]})
    report = run_drc(path, "gf180mcu")
    skipped = {r["id"]: r["reason"] for r in report["coverage"]["skipped"]}
    for rule_id in UNTRANSCRIBED_RULES:
        assert skipped[rule_id] == SKIP_REASON_NOT_TRANSCRIBED
    assert set(get_untranscribed_rules("gf180mcu")) == set(UNTRANSCRIBED_RULES)
    # A clean stream is therefore a qualified one, never an unconditional clean.
    assert report["status"] != "clean"


def test_other_decks_declare_no_untranscribed_rules():
    assert get_untranscribed_rules("sky130") == {}
    assert get_untranscribed_rules("nonexistent") == {}
