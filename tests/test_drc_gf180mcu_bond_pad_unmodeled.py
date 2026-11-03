"""gf180mcu discloses DRM 9.2 bond-pad guideline ids as unmodeled (issue #4001)."""

from __future__ import annotations

from klayout_tools.decks import get_unmodeled_rules


def test_gf180mcu_discloses_bond_pad_guidelines_but_not_coded_pad4():
    gf = get_unmodeled_rules("gf180mcu")
    assert gf == sorted(gf)
    assert {"PAD.1", "PAD.2", "PAD.5", "PAD.19a", "PAD.19b", "PAD.20"} <= set(gf)
    assert "PAD.4" not in gf


def test_sky130_has_no_pad_unmodeled_rules():
    assert not any(r.startswith("PAD.") for r in get_unmodeled_rules("sky130"))
