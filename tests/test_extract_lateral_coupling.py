"""Lateral (same-layer sidewall) coupling tests for `klt extract
--parasitics --critical-net` (issue #976 / Epic #709 Phase 2a; the gf180mcu
deck table and the `parasitics.lateral_coupling` capability signal are
issue #5742).

Split out of `test_extract.py` (issue #5742) when the new tests pushed
that module past its file-size ratchet ceiling; the shared sky130
fixtures (`_make_lateral_coupling_layout`, `_write_gds`, `_lateral_run`)
are imported from there."""

from __future__ import annotations

import dataclasses

import klayout.db as kdb
import pytest

from klayout_tools.extract import run_extract
from test_extract import _lateral_run, _make_lateral_coupling_layout, _write_gds


def test_lateral_coupling_matches_when_only_one_side_critical(tmp_path):
    """The lateral pass qualifies a pair when *either* side is named --
    naming the non-victim side (`AGR`) finds the identical pair."""
    path = _write_gds(_make_lateral_coupling_layout(), tmp_path / "lateral.gds")
    report = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "lateral.spice"),
        parasitics=True,
        critical_nets=["AGR"],
    )
    para = report["parasitics"]
    assert para["cc_count"] == 1
    assert para["total_coupling_capacitance_ff"] == pytest.approx(0.044)


def _make_gf180mcu_lateral_coupling_layout(top_name: str = "TOP") -> kdb.Layout:
    """Two same-layer (Metal1, gf180mcu ``metals[0]``) nets purpose-built
    for the lateral coupling pass on the gf180mcu deck (issue #5742):

    - Net ``AGR``: a 2x1 um Metal1 bar.
    - Net ``VIC``: a 2x1 um Metal1 bar 0.24 um to the right -- inside
      Metal1's own lookback (``metal_sidewall_lookback_um[0]`` = 2x
      ``metal1.space.1`` = 0.46 um) and DRC-legal against the 0.23 um
      ``metal1.space.1`` rule itself, with a fully-facing 1 um edge.

    No vertical-overlap geometry, so the fixture's coupling is lateral-only
    (the sky130 twin above, retargeted to gf180mcu's Metal1 layer/label
    purpose)."""
    layout = kdb.Layout()
    top = layout.create_cell(top_name)

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    draw(34, 0, kdb.Box(0, 0, 2000, 1000))  # Metal1, net AGR
    label(34, 10, "AGR", 1000, 500)
    draw(34, 0, kdb.Box(2240, 0, 4240, 1000))  # Metal1, net VIC -- 0.24 um gap
    label(34, 10, "VIC", 3240, 500)

    return layout


def test_lateral_coupling_null_without_critical_net_selection(tmp_path):
    """`lateral_coupling` is `null` when no critical-net selection was
    requested (issue #5742): the field signals a capability state for a
    lateral pass, and no lateral pass ran -- neither "computed" nor
    "unmodelled" applies."""
    para = _lateral_run(tmp_path)["parasitics"]
    assert para["lateral_coupling"] is None
    assert para["cc_count"] == 0


def test_lateral_coupling_computed_on_gf180mcu_deck(tmp_path):
    """Issue #5742, deck half: the gf180mcu deck curates
    `metal_sidewalls`, so `--critical-net` on it is no longer an
    unconditional zero -- `lateral_coupling` reads "computed" and the
    AGR<->VIC pair couples at 1 um facing edge x Metal1's 0.040512 fF/um
    coefficient (`defaultsidewall allm1 metal1 40.512`, aF/um / 1000,
    gf180mcuD.tech @ the deck's pinned commit, nominal corner)."""
    path = _write_gds(
        _make_gf180mcu_lateral_coupling_layout(), tmp_path / "lateral.gds"
    )
    report = run_extract(
        path,
        "gf180mcu",
        output=str(tmp_path / "lateral.spice"),
        parasitics=True,
        critical_nets=["VIC"],
    )
    para = report["parasitics"]
    assert para["lateral_coupling"] == "computed"
    assert para["cc_count"] == 1
    assert para["total_coupling_capacitance_ff"] == pytest.approx(0.040512)
    # The deck now curates the coefficient, so the "no lateral coupling
    # coefficient" warning must no longer fire.
    assert not any("PARASITICS.metal_sidewalls" in w for w in report["warnings"])


def test_lateral_coupling_unmodelled_on_deck_without_sidewalls(tmp_path, monkeypatch):
    """Issue #5742, signal half: a deck that curates no `metal_sidewalls`
    reports `lateral_coupling: "unmodelled"` -- machine-readable, no
    string-matching required -- while the existing prose warning stays
    (back-compat). Both read the same `any(metal_sidewalls)` predicate, so
    neither can report a capability the other denies for a resolved
    selection."""
    import klayout_tools.extract as extract_mod
    from klayout_tools.decks import sky130

    empty_deck = dataclasses.replace(
        sky130.PARASITICS, metal_sidewalls=(), metal_sidewall_lookback_um=()
    )
    monkeypatch.setattr(extract_mod, "get_parasitics_deck", lambda name: empty_deck)
    path = _write_gds(_make_lateral_coupling_layout(), tmp_path / "lateral.gds")
    report = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "lateral.spice"),
        parasitics=True,
        critical_nets=["VIC"],
    )
    para = report["parasitics"]
    assert para["lateral_coupling"] == "unmodelled"
    assert para["cc_count"] == 0
    assert para["total_coupling_capacitance_ff"] == 0.0
    assert any("PARASITICS.metal_sidewalls" in w for w in report["warnings"])
