"""Tests for the sg13cmos5l (IHP-Open-PDK) MOS-only starter deck (issue
#1400, decomposed from #1398; metal stack extended by #1417).

`src/klayout_tools/decks/sg13cmos5l.py`'s DRC width/space/enclosing rules are
already exercised end-to-end (coverage, provenance-populated, curated-engine
agreement) by `tests/test_golden_deck.py`'s `DECK_NAMES`-parametrized tests
-- `sg13cmos5l` was added to that tuple by #1400, so those three tiers
already cover every rule in `sg13cmos5l.DECK` (27 rules as of #1417, up from
6) with no new test code needed here.

This module covers the things `test_golden_deck.py` does *not*, mirroring
`tests/test_sg13g2_deck.py`'s own structure exactly (the deck this one is
independently verified against, not inherited from by analogy -- see
`sg13cmos5l.py`'s own module docstring):

- **`klt drc --deck sg13cmos5l` runs at all**, end to end through the
  CLI-facing `run_drc()` entry point (not just via the golden-pair manifest
  machinery).
- **LVS device-recognition provenance + a golden layout->netlist pair**: a
  minimal drawn NMOS/PMOS extracts with the `l_um`/`w_um` its own geometry
  implies, and `EXTRACTION_DECK.nfet_provenance`/`pfet_provenance` cite the
  real, independently-fetched `mos_extraction.lvs` device-class names.
- **Drawn poly-resistor recognition** (issue #1415): the three
  `res_extraction.lvs` poly flavours (`rsil`/`rppd`/`rhigh`) extract with
  `R = squares * sheet_rho_ohm_sq` and -- the regression this issue is
  actually about -- keep their two contacted heads on *distinct* nets
  instead of shorting them together through the unmodelled `GatPoly` body.
- **The Metal1-TopMetal1 connectivity stack** (issue #1417): a net routed
  straight up through every via level extracts as one connected net (not
  several disconnected ones, the pre-#1417 "outside deck's connectivity
  graph" failure mode), and geometry on the layers cmos5l forbids outright
  just above this stack (`Metal5`/`Via4`/`TopVia2`/`TopMetal2`) stays
  correctly unrecognised.

- **HV (`ThickGateOx`-flavoured) MOS device recognition** (issue #1416): a
  transistor drawn inside `ThickGateOx` (44/0) extracts bound to the real
  `sg13_hv_nmos`/`sg13_hv_pmos` model under `klt extract --pdk`, mirroring
  `tests/test_sg13g2_deck.py`'s own HV golden-pair tests.

See `src/klayout_tools/decks/sg13cmos5l.py`'s module docstring for this
deck's full provenance notes, scope (`width`/`space`/`enclosing` DRC checks
across `Activ`/`GatPoly`/`Metal1`-`TopMetal1`/`Via1`-`Via3`/`TopVia1`; LV *and*
HV MOSFET LVS device class pairs, plus the three poly resistors), and what was
deliberately left un-transcribed and why (metal resistors, diodes, capacitors,
parasitics).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.decks import RuleProvenance, get_deck
from klayout_tools.decks import sg13cmos5l as sg13cmos5l_deck_module
from klayout_tools.decks.sg13cmos5l import EXTRACTION_DECK
from klayout_tools.drc import run_drc
from klayout_tools.extract import run_extract

_DBU_UM = 0.001
_IHP_OPEN_PDK_COMMIT = "d2cc0355f26235c777dfcc6867b390fa1e78083f"


def _box_um(x0: float, y0: float, x1: float, y1: float) -> kdb.Box:
    """A `kdb.Box` from micrometre coordinates at sg13cmos5l's own
    `NOMINAL_DBU_UM` convention (1 nm/unit, matching `sg13cmos5l.lyt`'s own
    `<dbu>0.001</dbu>`)."""
    return kdb.Box(
        round(x0 / _DBU_UM),
        round(y0 / _DBU_UM),
        round(x1 / _DBU_UM),
        round(y1 / _DBU_UM),
    )


def _write_gds(layout: kdb.Layout, path: Path) -> str:
    layout.write(str(path))
    return str(path)


# --------------------------------------------------------------------------- #
# klt drc --deck sg13cmos5l (end to end through run_drc, not just the golden
# manifest machinery)
# --------------------------------------------------------------------------- #


def test_sg13cmos5l_deck_registered_with_six_width_space_rules():
    """`get_deck("sg13cmos5l")` resolves (this issue's registration in
    `decks/__init__.py`'s registries) and is exactly the curated
    Activ->lowest-metal width/space-only starter subset the module
    docstring describes -- 6 rules across 3 layers (`Activ`, `GatPoly`,
    `Metal1`), no other check kind.

    (Name kept from #1400 for git-blame continuity even though #1417 below
    grew the deck well past six rules -- the `Activ`/`GatPoly`/`Metal1`
    width/space-only *subset* this test asserts is still exactly six rules;
    see `test_sg13cmos5l_deck_has_27_rules_after_metal_stack_extension` for
    the full, current rule count.)"""
    deck = get_deck("sg13cmos5l")
    mos_only_rules = [
        r
        for r in deck
        if r.layer in {(1, 0), (5, 0), (8, 0)} and r.check in {"width", "space"}
    ]
    assert len(mos_only_rules) == 6
    assert {rule.check for rule in mos_only_rules} == {"width", "space"}


def test_sg13cmos5l_deck_has_27_rules_after_metal_stack_extension():
    """Issue #1417 extends the curated deck past its #1400 Activ/GatPoly/
    Metal1-only starter to cover the full Metal1-TopMetal1 stack and its
    Via1/Via2/Via3/TopVia1 vias -- 27 rules total across 11 layers, and now
    an `"enclosing"` check kind (the via/metal enclosure rules) alongside
    `"width"`/`"space"`."""
    deck = get_deck("sg13cmos5l")
    assert len(deck) == 27
    assert {rule.check for rule in deck} == {"width", "space", "enclosing"}
    assert {rule.layer for rule in deck} == {
        (1, 0),  # Activ.drawing
        (5, 0),  # GatPoly.drawing
        (8, 0),  # Metal1.drawing
        (10, 0),  # Metal2.drawing
        (19, 0),  # Via1.drawing
        (29, 0),  # Via2.drawing
        (30, 0),  # Metal3.drawing
        (49, 0),  # Via3.drawing
        (50, 0),  # Metal4.drawing
        (125, 0),  # TopVia1.drawing
        (126, 0),  # TopMetal1.drawing
    }


def test_run_drc_sg13cmos5l_metal1_width_violation(tmp_path: Path):
    """A 0.11um `Metal1.drawing` bar (below the 0.16um `M1.a` floor) trips
    `metal1.width.1` under `klt drc --deck sg13cmos5l` end to end."""
    layout = kdb.Layout()
    layout.dbu = _DBU_UM
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(8, 0)).insert(_box_um(0, 0, 0.11, 4))
    path = _write_gds(layout, tmp_path / "m1_violate.gds")

    report = run_drc(path, "sg13cmos5l")
    assert report["status"] == "violations"
    assert report["rule_counts"].get("metal1.width.1", 0) >= 1


def test_run_drc_sg13cmos5l_metal1_clean(tmp_path: Path):
    """A 0.21um `Metal1.drawing` bar (above the 0.16um `M1.a` floor, and
    with no second shape to trip a space check) is fully clean under
    `klt drc --deck sg13cmos5l`."""
    layout = kdb.Layout()
    layout.dbu = _DBU_UM
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(8, 0)).insert(_box_um(0, 0, 0.21, 4))
    path = _write_gds(layout, tmp_path / "m1_clean.gds")

    report = run_drc(path, "sg13cmos5l")
    assert report["status"] == "clean"


def test_run_drc_sg13cmos5l_via1_enclosure_violation(tmp_path: Path):
    """A `Via1.drawing` square barely (0.005um) enclosed by `Metal1.drawing`
    -- below the 0.01um `V1.c` floor -- trips `metal1.enclosing.via1.1`
    under `klt drc --deck sg13cmos5l` end to end, exercising one of the new
    via-level rules #1417 adds (not just the pre-existing Metal1 width/space
    pair `test_run_drc_sg13cmos5l_metal1_width_violation`/`..._metal1_clean`
    above already covered)."""
    layout = kdb.Layout()
    layout.dbu = _DBU_UM
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(19, 0)).insert(_box_um(0, 0, 2, 2))  # Via1
    top.shapes(layout.layer(8, 0)).insert(_box_um(-0.005, -0.005, 2.005, 2.005))
    path = _write_gds(layout, tmp_path / "via1_enclosure_violate.gds")

    report = run_drc(path, "sg13cmos5l")
    assert report["status"] == "violations"
    assert report["rule_counts"].get("metal1.enclosing.via1.1", 0) >= 1


# --------------------------------------------------------------------------- #
# EXTRACTION_DECK's Metal1-TopMetal1 stack (issue #1417)
# --------------------------------------------------------------------------- #


def test_sg13cmos5l_deck_declares_full_metal_stack():
    """`EXTRACTION_DECK` now declares the full Metal1-TopMetal1 stack with a
    Via1/Via2/Via3/TopVia1 chain between them (index-aligned, `len(metals) -
    1` vias) and a `.pin` (datatype 2) label layer per metal level -- the
    deck-data half of #1417, mirroring `test_extract.py`'s
    `test_sky130_deck_declares_full_metal_stack`/
    `test_gf180mcu_deck_declares_full_metal_stack`. No `Metal5`/`Via4`/
    `TopVia2`/`TopMetal2`: cmos5l's own stack tops at `TopMetal1` (all four
    are on cmos5l's own LVS/DRC forbidden-layer lists -- see
    `sg13cmos5l.py`'s module docstring)."""
    assert EXTRACTION_DECK.metals == (
        (8, 0),  # Metal1.drawing
        (10, 0),  # Metal2.drawing
        (30, 0),  # Metal3.drawing
        (50, 0),  # Metal4.drawing
        (126, 0),  # TopMetal1.drawing
    )
    assert EXTRACTION_DECK.vias == (
        (19, 0),  # Via1.drawing
        (29, 0),  # Via2.drawing
        (49, 0),  # Via3.drawing
        (125, 0),  # TopVia1.drawing
    )
    assert EXTRACTION_DECK.metal_labels == (
        (8, 2),  # Metal1.pin
        (10, 2),  # Metal2.pin
        (30, 2),  # Metal3.pin
        (50, 2),  # Metal4.pin
        (126, 2),  # TopMetal1.pin
    )
    assert len(EXTRACTION_DECK.vias) == len(EXTRACTION_DECK.metals) - 1


def _make_nfet_layout_routed_through_full_metal_stack() -> kdb.Layout:
    """The same NMOS `_make_nfet_layout` draws, except the drain terminal is
    routed straight up through *every* level of `EXTRACTION_DECK.metals`/
    `.vias` (Metal1 -> Via1 -> Metal2 -> Via2 -> Metal3 -> Via3 -> Metal4 ->
    TopVia1 -> TopMetal1) instead of being labelled directly at Metal1 --
    mirrors `tests/test_sg13g2_deck.py`'s own
    `_make_nmos_layout_routed_through_full_metal_stack` (issue #1243), the
    template this issue's own curated body names. The drain net's own `"D"`
    label is drawn only at the very top (`TopMetal1.pin`, 126/2); if any
    level of the stack were disconnected, `run_extract` would instead report
    an unlabelled/synthesized net for the drain terminal, not `"D"`."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(1, 0, _box_um(0, 0, 2, 1))  # Activ.drawing, W=1um
    draw(5, 0, _box_um(0.8, -0.2, 1.2, 1.2))  # GatPoly.drawing gate, L=0.4um

    draw(6, 0, _box_um(0.1, 0.3, 0.3, 0.7))  # Cont (source side)
    draw(8, 0, _box_um(0.0, 0.2, 0.4, 0.8))  # Metal1 (source pad)
    label(8, 2, "S", 0.2, 0.5)  # Metal1.pin

    draw(6, 0, _box_um(0.9, 1.0, 1.1, 1.2))  # gate Cont
    draw(8, 0, _box_um(0.85, 0.95, 1.15, 1.25))  # gate Metal1 pad
    label(8, 2, "G", 1.0, 1.1)

    # Drain: Cont lands on Metal1, then every metal/via level of the stack
    # stacks straight up, each level's box wide/tall enough to satisfy that
    # level's own DRC width floor and the via enclosure margin below/above
    # it (see `sg13cmos5l.py`'s `DECK`) -- not load-bearing for this
    # connectivity-only test, but keeping the fixture DRC-clean too avoids a
    # golden layout that would fail `klt drc` on the very stack it
    # demonstrates.
    draw(6, 0, _box_um(1.7, 0.3, 1.9, 0.7))  # Cont (drain side)
    conductor_layers = [
        (8, 0),  # Metal1.drawing
        (10, 0),  # Metal2.drawing
        (30, 0),  # Metal3.drawing
        (50, 0),  # Metal4.drawing
        (126, 0),  # TopMetal1.drawing
    ]
    via_layers = [
        (19, 0),  # Via1.drawing
        (29, 0),  # Via2.drawing
        (49, 0),  # Via3.drawing
        (125, 0),  # TopVia1.drawing
    ]
    for layer, datatype in conductor_layers:
        draw(layer, datatype, _box_um(1.5, 0.0, 4.5, 3.0))
    for layer, datatype in via_layers:
        draw(layer, datatype, _box_um(2.0, 0.5, 3.0, 1.5))
    label(126, 2, "D", 3.0, 1.5)  # TopMetal1.pin -- the top of the stack

    return layout


def test_golden_pair_sg13cmos5l_nfet_drain_routes_through_full_metal_stack(
    tmp_path: Path,
):
    """The device-recognition geometry is unchanged from
    `test_golden_pair_sg13cmos5l_nfet_l_w_matches_drawn_geometry` above (same
    `l_um`/`w_um`), but the drain terminal is only labelled at `TopMetal1`,
    the top of the stack issue #1417 extends `EXTRACTION_DECK.metals`/
    `.vias` to reach. Before #1417 (`metals`/`vias` capped at `Metal1`, no
    vias), a drain routed this far out would resolve to an *isolated*,
    unlabelled/synthesized net -- `deck.metals`/`.vias` had no entries above
    `Metal1` to carry the connection, so the "D" label at `TopMetal1` would
    never reach the transistor's own drain terminal at all -- exactly the
    "outside deck's connectivity graph" failure mode this issue exists to
    close."""
    path = _write_gds(
        _make_nfet_layout_routed_through_full_metal_stack(),
        tmp_path / "nfet_full_stack.gds",
    )
    report = run_extract(
        path, "sg13cmos5l", output=str(tmp_path / "nfet_full_stack.spice")
    )

    assert report["device_counts"] == {"nfet": 1}
    (device,) = report["devices"]
    assert device["class"] == "nfet"
    assert device["params"]["l_um"] == pytest.approx(0.4)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert device["nets"]["s"] == "S"
    assert device["nets"]["d"] == "D"
    assert device["nets"]["g"] == "G"
    assert report["ignored_layers"] == []
    assert not any("connectivity graph" in warning for warning in report["warnings"])


def test_sg13cmos5l_forbidden_layers_above_topmetal1_stay_unrecognised(
    tmp_path: Path,
):
    """Geometry on `Metal5` (67/0), `Via4` (66/0), `TopVia2` (133/0), and
    `TopMetal2` (134/0) -- all four on cmos5l's own LVS/DRC forbidden-layer
    lists, and all four sitting just above this deck's curated
    `TopMetal1`-topped stack -- stays outside `EXTRACTION_DECK`'s
    connectivity graph after #1417, exactly like before: the metal-stack
    extension must not accidentally recognise a level cmos5l itself
    forbids. A drain labelled only on `Metal5` reports as a separate,
    unlabelled net rather than merging into the `Metal1`-rooted source/gate
    nets -- the same "outside deck's connectivity graph" diagnostic a
    genuinely undeclared layer produces."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(1, 0, _box_um(0, 0, 2, 1))  # Activ.drawing, W=1um
    draw(5, 0, _box_um(0.8, -0.2, 1.2, 1.2))  # GatPoly.drawing gate, L=0.4um

    draw(6, 0, _box_um(0.1, 0.3, 0.3, 0.7))  # Cont (source side)
    draw(8, 0, _box_um(0.0, 0.2, 0.4, 0.8))  # Metal1 (source pad)
    label(8, 2, "S", 0.2, 0.5)

    draw(6, 0, _box_um(0.9, 1.0, 1.1, 1.2))  # gate Cont
    draw(8, 0, _box_um(0.85, 0.95, 1.15, 1.25))  # gate Metal1 pad
    label(8, 2, "G", 1.0, 1.1)

    # Drain: Cont lands on Metal1, but the "route" beyond that is drawn on
    # the four forbidden layers above TopMetal1 instead of this deck's real
    # stack -- none of which EXTRACTION_DECK reads.
    draw(6, 0, _box_um(1.7, 0.3, 1.9, 0.7))  # Cont (drain side)
    draw(8, 0, _box_um(1.6, 0.2, 2.0, 0.8))  # Metal1 (drain pad)
    forbidden_layers = [
        (67, 0),  # Metal5.drawing
        (66, 0),  # Via4.drawing
        (133, 0),  # TopVia2.drawing
        (134, 0),  # TopMetal2.drawing
    ]
    for layer, datatype in forbidden_layers:
        draw(layer, datatype, _box_um(1.5, 0.0, 4.5, 3.0))
    label(134, 0, "D", 3.0, 1.5)  # drawn on TopMetal2, not a read label layer

    path = _write_gds(layout, tmp_path / "forbidden_layers.gds")
    report = run_extract(
        path, "sg13cmos5l", output=str(tmp_path / "forbidden_layers.spice")
    )

    ignored = {(e["layer"], e["datatype"]) for e in report["ignored_layers"]}
    for layer, datatype in forbidden_layers:
        assert (layer, datatype) in ignored, report["ignored_layers"]
    # The drain net never reaches a "D" label: the forbidden-layer geometry
    # (including the "D" text itself, drawn on the un-read TopMetal2) is
    # invisible to extraction, so this NMOS's drain resolves to a
    # synthesized/unlabelled net, not "D".
    (device,) = report["devices"]
    assert device["nets"]["d"] != "D"


# --------------------------------------------------------------------------- #
# LVS device-recognition provenance (issue #1400 AC: "every rule cites a
# real, verifiable line in cmos5l's own .drc/.lvs source")
# --------------------------------------------------------------------------- #


def test_sg13cmos5l_mos_provenance_cites_mos_extraction_lvs():
    """`EXTRACTION_DECK.nfet_provenance`/`pfet_provenance` cite the real
    `mos_extraction.lvs` `mos4('sg13_lv_nmos'/'sg13_lv_pmos')`
    `extract_devices` calls -- independently fetched from
    `IHP-GmbH/IHP-Open-PDK` at the exact commit cmos5l's own
    `.github/ihp-sg13g2.ref` pins (not `sg13g2.py`'s own, older `v0.3.0`-tag
    commit -- see `sg13cmos5l.py`'s module docstring for why)."""
    assert EXTRACTION_DECK.nfet_provenance == RuleProvenance(
        source_repo="IHP-GmbH/IHP-Open-PDK",
        source_path=(
            "ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks/mos_extraction.lvs"
        ),
        rule_id="sg13_lv_nmos",
        commit="d2cc0355f26235c777dfcc6867b390fa1e78083f",
    )
    assert EXTRACTION_DECK.pfet_provenance == RuleProvenance(
        source_repo="IHP-GmbH/IHP-Open-PDK",
        source_path=(
            "ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks/mos_extraction.lvs"
        ),
        rule_id="sg13_lv_pmos",
        commit="d2cc0355f26235c777dfcc6867b390fa1e78083f",
    )


# --------------------------------------------------------------------------- #
# Golden layout -> netlist pairs (issue #1400 AC: "Golden layout->netlist
# pair tests ... for the new NMOS/PMOS device recognition, asserting
# extracted l_um/w_um match drawn geometry exactly")
# --------------------------------------------------------------------------- #


def _make_nfet_layout(*, thick_gate_ox: bool = False) -> kdb.Layout:
    """One drawn NMOS on sg13cmos5l's curated MOS-recognition layers: a
    2x1um `Activ.drawing` strip crossed by a 0.4um-wide `GatPoly.drawing`
    gate bar (active outside `NWell.drawing`, so it recognises as NMOS) --
    the exact device `EXTRACTION_DECK.nfet_provenance` cites
    (`sg13_lv_nmos`). cmos5l's `Cont.drawing` (`6/0`) lands directly on
    `Metal1.drawing` (`8/0`), the same single-first-metal-level shape
    `sg13g2.py`'s own deck has. `W` is the active strip's own 1um
    cross-extent; `L` is the poly bar's 0.4um width.

    With `thick_gate_ox=True` the same geometry is additionally covered by
    `ThickGateOx` (44/0), making it cmos5l's thick-oxide ("-HV") flavour
    (`sg13_hv_nmos`, `general_derivations.lvs`'s `ngate_hv_base =
    ngate.and(thickgateox_drw)`) -- the *only* difference between the two
    layouts, so any extracted difference is attributable to the marker
    alone."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(1, 0, _box_um(0, 0, 2, 1))  # Activ.drawing, W=1um
    draw(5, 0, _box_um(0.8, -0.2, 1.2, 1.2))  # GatPoly.drawing gate, L=0.4um
    if thick_gate_ox:
        draw(44, 0, _box_um(-0.3, -0.3, 2.3, 1.3))  # ThickGateOx -> "-HV"

    draw(6, 0, _box_um(0.1, 0.3, 0.3, 0.7))  # Cont (source side)
    draw(6, 0, _box_um(1.7, 0.3, 1.9, 0.7))  # Cont (drain side)
    draw(8, 0, _box_um(0.0, 0.2, 0.4, 0.8))  # Metal1 (source pad)
    draw(8, 0, _box_um(1.6, 0.2, 2.0, 0.8))  # Metal1 (drain pad)
    label(8, 2, "S", 0.2, 0.5)  # Metal1.pin
    label(8, 2, "D", 1.8, 0.5)

    draw(6, 0, _box_um(0.9, 1.0, 1.1, 1.2))  # gate contact
    draw(8, 0, _box_um(0.85, 0.95, 1.15, 1.25))  # gate pad
    label(8, 2, "G", 1.0, 1.1)

    return layout


def test_golden_pair_sg13cmos5l_nfet_l_w_matches_drawn_geometry(tmp_path: Path):
    """A drawn 0.4um-gate / 1um-active-width NMOS extracts with exactly
    those `l_um`/`w_um` -- validating `EXTRACTION_DECK.nfet_provenance`'s
    `sg13_lv_nmos` citation end-to-end: a golden layout, run through
    `run_extract`, reproduces the hand-computed netlist device parameters
    the drawn geometry implies."""
    path = _write_gds(_make_nfet_layout(), tmp_path / "nfet.gds")
    report = run_extract(path, "sg13cmos5l", output=str(tmp_path / "nfet.spice"))

    assert report["device_counts"] == {"nfet": 1}
    (device,) = report["devices"]
    assert device["class"] == "nfet"
    assert device["params"]["l_um"] == pytest.approx(0.4)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert device["nets"]["b"] == EXTRACTION_DECK.substrate_net
    assert EXTRACTION_DECK.nfet_provenance.rule_id == "sg13_lv_nmos"


def _make_pfet_layout(*, thick_gate_ox: bool = False) -> kdb.Layout:
    """The `_make_nfet_layout` geometry with the same active/poly/contact
    stack, wrapped in `NWell.drawing` (active *inside* `NWell` recognises as
    PMOS, the deck's own NMOS/PMOS split) -- the exact device
    `EXTRACTION_DECK.pfet_provenance` cites (`sg13_lv_pmos`). A direct
    `NWell.pin` (`well_label`) text names the body/well net without needing
    a separate tap-contact stack, mirroring `sg13g2.py`'s own `well_label`
    convention.

    With `thick_gate_ox=True`, same `ThickGateOx` (44/0) addition as
    `_make_nfet_layout` -- the PMOS sibling of its thick-oxide flavour
    (`sg13_hv_pmos`)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(31, 0, _box_um(-1, -1, 3, 2))  # NWell.drawing, encloses active -> PMOS
    label(31, 2, "VPB", 2.5, 1.5)  # NWell.pin, body/well net label

    draw(1, 0, _box_um(0, 0, 2, 1))  # Activ.drawing, W=1um
    draw(5, 0, _box_um(0.8, -0.2, 1.2, 1.2))  # GatPoly.drawing gate, L=0.4um
    if thick_gate_ox:
        draw(44, 0, _box_um(-0.3, -0.3, 2.3, 1.3))  # ThickGateOx -> "-HV"

    draw(6, 0, _box_um(0.1, 0.3, 0.3, 0.7))  # Cont (source side)
    draw(6, 0, _box_um(1.7, 0.3, 1.9, 0.7))  # Cont (drain side)
    draw(8, 0, _box_um(0.0, 0.2, 0.4, 0.8))  # Metal1 (source pad)
    draw(8, 0, _box_um(1.6, 0.2, 2.0, 0.8))  # Metal1 (drain pad)
    label(8, 2, "S", 0.2, 0.5)
    label(8, 2, "D", 1.8, 0.5)

    draw(6, 0, _box_um(0.9, 1.0, 1.1, 1.2))  # gate contact
    draw(8, 0, _box_um(0.85, 0.95, 1.15, 1.25))  # gate pad
    label(8, 2, "G", 1.0, 1.1)

    return layout


def test_golden_pair_sg13cmos5l_pfet_l_w_matches_drawn_geometry(tmp_path: Path):
    """A drawn 0.4um-gate / 1um-active-width PMOS (active wrapped in
    `NWell`) extracts with exactly those `l_um`/`w_um` -- validating
    `EXTRACTION_DECK.pfet_provenance`'s `sg13_lv_pmos` citation end-to-end,
    the PMOS sibling of
    `test_golden_pair_sg13cmos5l_nfet_l_w_matches_drawn_geometry` above."""
    path = _write_gds(_make_pfet_layout(), tmp_path / "pfet.gds")
    report = run_extract(path, "sg13cmos5l", output=str(tmp_path / "pfet.spice"))

    assert report["device_counts"] == {"pfet": 1}
    (device,) = report["devices"]
    assert device["class"] == "pfet"
    assert device["params"]["l_um"] == pytest.approx(0.4)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert device["nets"]["b"] == "VPB"
    assert EXTRACTION_DECK.pfet_provenance.rule_id == "sg13_lv_pmos"


# --------------------------------------------------------------------------- #
# Thick-oxide ("-HV") MOS flavour (issue #1416)
# --------------------------------------------------------------------------- #


def _make_pdk_install(tmp_path: Path) -> str:
    """A minimal IHP-Open-PDK-shaped install tree `klt pdk find` resolves as
    variant `ihp-sg13cmos5l` -- mirrors `test_sg13g2_deck.py`'s own
    `_make_pdk_install` (the resolver only probes for the variant
    directory's `libs.tech` marker; the model binding itself is a curated
    table, not a file read)."""
    root = tmp_path / "pdk_install"
    (root / "ihp-sg13cmos5l" / "libs.tech").mkdir(parents=True)
    return str(root)


def _device_cards(report: dict) -> list[str]:
    text = Path(report["netlist_path"]).read_text()
    return [line for line in text.splitlines() if line and line[0] in ("M", "X")]


def test_sg13cmos5l_thick_gate_ox_declares_a_mos_flavour():
    """`ThickGateOx` (44/0) is no longer a diagnostic-only voltage-domain
    marker: `EXTRACTION_DECK` declares one `mos_flavours` entry keyed on it
    (issue #1416), whose `nfet_provenance`/`pfet_provenance` cite
    `mos_extraction.lvs`'s own `sg13_hv_nmos`/`sg13_hv_pmos`
    `extract_devices(mos4(...))` calls."""
    (flavour,) = EXTRACTION_DECK.mos_flavours
    assert flavour.marker == (44, 0)
    assert flavour.flavour == "hv"
    assert flavour.nfet_provenance.rule_id == "sg13_hv_nmos"
    assert flavour.pfet_provenance.rule_id == "sg13_hv_pmos"
    assert flavour.nfet_provenance.source_path == (
        "ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks/mos_extraction.lvs"
    )
    assert flavour.pfet_provenance.commit == _IHP_OPEN_PDK_COMMIT


def test_golden_pair_sg13cmos5l_thick_oxide_nmos_binds_sg13_hv_nmos(tmp_path: Path):
    """The acceptance criterion of issue #1416: a golden layout whose NMOS
    is drawn inside `ThickGateOx` (44/0) extracts bound to the real
    `sg13_hv_nmos` model, not the thin-oxide `sg13_lv_nmos` -- and the
    `ThickGateOx` voltage-domain warning that used to fire for exactly this
    geometry no longer does."""
    path = _write_gds(_make_nfet_layout(thick_gate_ox=True), tmp_path / "hv_nfet.gds")
    report = run_extract(
        path,
        "sg13cmos5l",
        pdk_variant="ihp-sg13cmos5l",
        pdk_root=_make_pdk_install(tmp_path),
        output=str(tmp_path / "hv_nfet.spice"),
    )

    # The structural class label is deliberately unchanged (see
    # `MOSFlavour`'s own docstring) -- only the bound model name
    # distinguishes the flavours.
    assert report["device_counts"] == {"nfet": 1}
    (device,) = report["devices"]
    assert device["params"]["l_um"] == pytest.approx(0.4)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert report["voltage_domain_warnings"] == []

    cards = _device_cards(report)
    assert cards and all(line.startswith("X") for line in cards)
    assert any(" sg13_hv_nmos " in line for line in cards)
    assert not any(" sg13_lv_nmos " in line for line in cards)


def test_golden_pair_sg13cmos5l_thick_oxide_pfet_binds_sg13_hv_pmos(tmp_path: Path):
    """The PMOS sibling of the NMOS golden pair above (`sg13_hv_pmos`)."""
    path = _write_gds(_make_pfet_layout(thick_gate_ox=True), tmp_path / "hv_pfet.gds")
    report = run_extract(
        path,
        "sg13cmos5l",
        pdk_variant="ihp-sg13cmos5l",
        pdk_root=_make_pdk_install(tmp_path),
        output=str(tmp_path / "hv_pfet.spice"),
    )

    assert report["device_counts"] == {"pfet": 1}
    assert report["voltage_domain_warnings"] == []
    cards = _device_cards(report)
    assert any(" sg13_hv_pmos " in line for line in cards)
    assert not any(" sg13_lv_pmos " in line for line in cards)


def test_sg13cmos5l_thin_oxide_mos_still_binds_sg13_lv_nmos(tmp_path: Path):
    """Regression control: the *same* NMOS geometry with no `ThickGateOx`
    drawn over it still binds the deck's default thin-oxide model, so the
    flavour split above is attributable to the marker and nothing else."""
    path = _write_gds(_make_nfet_layout(), tmp_path / "lv_nfet.gds")
    report = run_extract(
        path,
        "sg13cmos5l",
        pdk_variant="ihp-sg13cmos5l",
        pdk_root=_make_pdk_install(tmp_path),
        output=str(tmp_path / "lv_nfet.spice"),
    )

    assert report["device_counts"] == {"nfet": 1}
    assert report["voltage_domain_warnings"] == []
    cards = _device_cards(report)
    assert any(" sg13_lv_nmos " in line for line in cards)
    assert not any(" sg13_hv_nmos " in line for line in cards)


def test_sg13cmos5l_thick_oxide_mos_was_misclassified_before_mos_flavours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Negative control for the fix: with `mos_flavours` stripped back to
    its pre-#1416 empty state (and nothing else changed), the *same*
    thick-oxide golden layout binds `sg13_lv_nmos` -- the silent
    mis-classification issue #1416 reports -- and `ThickGateOx` fires the
    residual-gap `voltage_domain_warnings` diagnostic it fired before the
    fix."""
    monkeypatch.setattr(
        sg13cmos5l_deck_module,
        "EXTRACTION_DECK",
        dataclasses.replace(EXTRACTION_DECK, mos_flavours=()),
    )

    path = _write_gds(
        _make_nfet_layout(thick_gate_ox=True), tmp_path / "hv_nfet_pre_fix.gds"
    )
    report = run_extract(
        path,
        "sg13cmos5l",
        pdk_variant="ihp-sg13cmos5l",
        pdk_root=_make_pdk_install(tmp_path),
        output=str(tmp_path / "hv_nfet_pre_fix.spice"),
    )

    cards = _device_cards(report)
    assert any(" sg13_lv_nmos " in line for line in cards)
    assert not any(" sg13_hv_nmos " in line for line in cards)
    assert [w["marker"] for w in report["voltage_domain_warnings"]] == ["44/0"]


# --------------------------------------------------------------------------- #
# Drawn poly resistors (issue #1415)
# --------------------------------------------------------------------------- #

_RES_SQUARES = 6.0  # 6um marked segment / 1um drawn width


def test_sg13cmos5l_recognises_the_three_poly_resistor_flavours():
    """`EXTRACTION_DECK.resistors` declares exactly the three poly flavours
    cmos5l's own (symlink-resolved) `res_extraction.lvs` extracts through
    `GeneralNTerminalExtractor.new('<name>', 2)`. The metal resistors that
    file also declares (`res_metal1`..`res_topmetal2`) are deliberately
    absent -- see `sg13cmos5l.py`'s own resistor note (two of them sit on
    layers cmos5l forbids outright, and four of the remaining five need the
    Metal2-TopMetal1 stack this starter does not model, issue #1417)."""
    assert {r.name for r in EXTRACTION_DECK.resistors} == {
        "rsil",
        "rppd",
        "rhigh",
    }
    # The generic `"resistor"` device class is now live on this deck (it was
    # absent while `resistors` was empty); no diode/capacitor/bipolar class
    # joins it, so this starter's remaining device-recognition gaps stay
    # visible in the same assertion.
    assert EXTRACTION_DECK.device_classes == ("nfet", "pfet", "resistor")
    assert EXTRACTION_DECK.capacitors == ()
    assert EXTRACTION_DECK.bipolars == ()
    assert EXTRACTION_DECK.diodes == ()


def test_sg13cmos5l_resistor_provenance_cites_res_extraction_lvs():
    """Every curated resistor entry cites the real `res_extraction.lvs`
    `extract_devices(...)` call it was transcribed from, at the commit
    cmos5l's own `.github/ihp-sg13g2.ref` pins (that file is one of the
    rule decks cmos5l symlinks into the sibling `ihp-sg13g2` checkout -- see
    `sg13cmos5l.py`'s module docstring), with the sheet resistance read from
    cmos5l's *own*, non-symlinked `sg13cmos5l_tech.json`."""
    by_name = {r.name: r for r in EXTRACTION_DECK.resistors}
    for name in ("rsil", "rppd", "rhigh"):
        assert by_name[name].provenance == RuleProvenance(
            source_repo="IHP-GmbH/IHP-Open-PDK",
            source_path=(
                "ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks/res_extraction.lvs"
            ),
            rule_id=name,
            commit="d2cc0355f26235c777dfcc6867b390fa1e78083f",
        )
    # `rsilG2_rspec`/`rppdG2_rspec` in cmos5l's own
    # `libs.tech/klayout/python/sg13cmos5l_pycell_lib/sg13cmos5l_tech.json`
    # -- its `techName` is `SG13G2_CMOS5L`, which contains the `SG13G2`
    # substring `rppd_code.py` tests for, so the `G2` suffix is selected here
    # exactly as it is for sg13g2 itself.
    assert by_name["rsil"].sheet_rho_ohm_sq == pytest.approx(7.0)
    assert by_name["rppd"].sheet_rho_ohm_sq == pytest.approx(260.0)
    # `rhighG2_rspec` (1360.0), corroborated by cmos5l's own (symlinked)
    # `cornerRES.lib` `res_typ` corner -- see `sg13cmos5l.py`'s resistor note
    # for the tie-break against the same file's stale `rhigh_rspec` (1300.0).
    assert by_name["rhigh"].sheet_rho_ohm_sq == pytest.approx(1360.0)


def test_sg13cmos5l_resistor_bodies_are_this_decks_own_poly_layer():
    """All three flavours take their body from `GatPoly.drawing` (5/0) and
    their marker from `PolyRes.drawing` (128/0) -- the GDS numbers read from
    cmos5l's own `sg13cmos5l.lyp`, and the same layer this deck already
    declares as `EXTRACTION_DECK.poly` (a `ResistorDevice.body` must be one
    of the owning deck's own conductor layers, or the body could not be
    subtracted from that layer's connectivity)."""
    for resistor in EXTRACTION_DECK.resistors:
        assert resistor.body == EXTRACTION_DECK.poly == (5, 0)
        assert resistor.marker == (128, 0)
        assert resistor.bulk_to_substrate is True


def test_sg13cmos5l_rhigh_requires_both_implants_disambiguating_it_from_rppd():
    """`rhigh`'s `requires` carries both `pSD`/`nSD` together (upstream:
    `rhigh_res = polyres_mk.and(psd_drw).and(nsd_drw).and(salblock_drw)`),
    which is what keeps it distinct from `rppd` (whose own `excludes`
    subtract `nSD`/`nSD_block`, per `rppd_res = ... .not(nsd_block)
    .not(nsd_drw)`) -- a segment carrying nSD can only ever match `rhigh`,
    and a segment without it can only ever match `rppd`."""
    by_name = {r.name: r for r in EXTRACTION_DECK.resistors}
    rhigh = by_name["rhigh"]
    rppd = by_name["rppd"]
    assert (7, 0) in rhigh.requires  # nSD required
    assert (7, 0) in rppd.excludes  # nSD excluded
    assert (14, 0) in rhigh.requires  # pSD required
    assert (14, 0) in rppd.requires  # pSD required
    assert (28, 0) in rhigh.requires  # SalBlock required
    assert (28, 0) in rppd.requires  # SalBlock required


def test_sg13cmos5l_resistors_exclude_activ_so_a_marked_gate_is_not_a_resistor():
    """`res_derivations.lvs`'s `polyres_exclude` leads with `activ`, so a
    `polyres`-marked strip over diffusion is never a resistor -- without
    that term a marked *gate* would be misclassified. Every flavour carries
    it (the same guard `sg13g2.py`'s own entries document)."""
    for resistor in EXTRACTION_DECK.resistors:
        assert (1, 0) in resistor.excludes  # Activ
        assert (44, 0) in resistor.excludes  # ThickGateOx


def _make_poly_resistor_layout(
    extra_layers: tuple[tuple[int, int], ...],
) -> kdb.Layout:
    """A 12x1um `GatPoly` bar with a 6um-long `PolyRes`-marked segment
    (`L=6um`/`W=1um`, 6.0 squares), contacted up to a labelled `Metal1` pad
    at each end, plus `extra_layers` drawn over that same segment to narrow
    it to one specific cmos5l poly-resistor flavour.

    This is the exact shape the issue describes: "a drawn `rppd` body is a
    `GatPoly` strip contacted at two ends". Mirrors
    `tests/test_sg13g2_deck.py`'s own `_make_poly_resistor_layout`, with
    cmos5l's own `Metal1.pin` (8/2) label layer -- this deck's
    `metal_labels` -- rather than sg13g2's `Metal1.text`."""
    layout = kdb.Layout()
    layout.dbu = _DBU_UM
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(5, 0, _box_um(0, 0, 12, 1))  # GatPoly.drawing bar, W=1um
    draw(128, 0, _box_um(3, 0, 9, 1))  # PolyRes.drawing marker -> L=6um
    for layer, datatype in extra_layers:
        draw(layer, datatype, _box_um(2.8, -0.2, 9.2, 1.2))

    draw(6, 0, _box_um(0.1, 0.3, 0.3, 0.7))  # Cont.drawing (head A)
    draw(6, 0, _box_um(11.7, 0.3, 11.9, 0.7))  # Cont.drawing (head B)
    draw(8, 0, _box_um(0.0, 0.2, 0.4, 0.8))  # Metal1.drawing (head A pad)
    draw(8, 0, _box_um(11.6, 0.2, 12.0, 0.8))  # Metal1.drawing (head B pad)
    label(8, 2, "RA", 0.2, 0.5)  # Metal1.pin
    label(8, 2, "RB", 11.8, 0.5)

    return layout


#: `(flavour name, marker layers drawn over the segment)` -- each entry is a
#: direct transcription of that flavour's own `res_derivations.lvs` recipe.
_POLY_RESISTOR_FLAVOURS = [
    # rsil: polyres & EXTBlock & RES, with pSD/nSD/SalBlock all absent
    # (`rsil_res = polyres_mk.and(res_drw).not(rsil_exc)`).
    ("rsil", ((111, 0), (24, 0))),
    # rppd: polyres & EXTBlock & pSD & SalBlock, nSD absent
    # (`rppd_res = polyres_mk.and(psd_drw).and(salblock_drw)...`).
    ("rppd", ((111, 0), (14, 0), (28, 0))),
    # rhigh: polyres & EXTBlock & pSD & nSD & SalBlock -- both implants
    # present together (`rhigh_res = polyres_mk.and(psd_drw).and(nsd_drw)
    # .and(salblock_drw)`).
    ("rhigh", ((111, 0), (14, 0), (7, 0), (28, 0))),
]


@pytest.mark.parametrize(("name", "extra_layers"), _POLY_RESISTOR_FLAVOURS)
def test_golden_pair_sg13cmos5l_poly_resistor_r_ohm_matches_provenance_coefficient(
    tmp_path: Path, name: str, extra_layers: tuple[tuple[int, int], ...]
):
    """A drawn 6-square `GatPoly` bar marked `PolyRes` and narrowed to one
    flavour extracts as that device class with `R = squares *
    sheet_rho_ohm_sq`, computed from the deck's own provenance-cited
    coefficient -- the golden layout->netlist pair for each new entry."""
    resistor = next(r for r in EXTRACTION_DECK.resistors if r.name == name)

    path = _write_gds(
        _make_poly_resistor_layout(extra_layers), tmp_path / f"{name}.gds"
    )
    report = run_extract(path, "sg13cmos5l", output=str(tmp_path / f"{name}.spice"))

    assert report["device_counts"] == {name: 1}
    (device,) = report["devices"]
    assert device["class"] == name
    assert device["params"]["l_um"] == pytest.approx(6.0)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert device["params"]["r_ohm"] == pytest.approx(
        _RES_SQUARES * resistor.sheet_rho_ohm_sq
    )


@pytest.mark.parametrize(("name", "extra_layers"), _POLY_RESISTOR_FLAVOURS)
def test_sg13cmos5l_poly_resistor_heads_stay_distinct_nets(
    tmp_path: Path, name: str, extra_layers: tuple[tuple[int, int], ...]
):
    """The regression this issue is actually about (#1415): before this
    deck recognised any poly resistor, the marked `GatPoly` body was
    absorbed into ordinary interconnect, shorting the two contacted heads
    onto one net -- which merged two schematic nets and cascaded into
    `net.unmatched`/`device.unmatched` LVS failures for every other device
    in the block.

    With the flavour recognised, `RA`/`RB` stay two distinct nets, the
    "resistor-body signature ... absorbed into ordinary interconnect as an
    unintended short" warning is gone, and no `merges N distinct labels`
    net-merge warning is emitted."""
    path = _write_gds(
        _make_poly_resistor_layout(extra_layers), tmp_path / f"{name}.gds"
    )
    report = run_extract(path, "sg13cmos5l", output=str(tmp_path / f"{name}.spice"))

    (device,) = report["devices"]
    assert {device["nets"]["a"], device["nets"]["b"]} == {"RA", "RB"}

    assert report["unmodelled_poly"] == []
    joined = " ".join(report["warnings"])
    assert "resistor-body signature" not in joined
    assert "merges" not in joined


def test_sg13cmos5l_unmarked_poly_bar_is_not_a_resistor(tmp_path: Path):
    """A `GatPoly` bar carrying the `PolyRes` marker but *none* of the
    flavour-selecting layers stays ordinary interconnect: a segment this
    deck cannot positively identify keeps today's short (and the diagnostic
    warning that names it) rather than extracting with a guessed sheet
    resistance -- `ResistorDevice.excludes`' own "known-unmodelled beats
    silently wrong" discipline."""
    path = _write_gds(_make_poly_resistor_layout(()), tmp_path / "bare.gds")
    report = run_extract(path, "sg13cmos5l", output=str(tmp_path / "bare.spice"))

    assert report["device_counts"] == {}
    assert [entry["reason"] for entry in report["unmodelled_poly"]] == [
        "marked_unrecognised"
    ]
