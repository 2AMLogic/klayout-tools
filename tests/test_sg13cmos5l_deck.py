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

- **MoM capacitor device recognition** (issue #1466, the corrected
  follow-on to #1463): `cap_cmomi`/`cap_cmomf` extract with `w_um`/`l_um`
  read off their own recognition marker and **no** computed `c_f`, through
  the `MomCapacitorDevice` shape that issue added -- including the stacked
  (`same`-feed) two-metal port case a `CapacitorDevice` entry could not
  express at all.

See `src/klayout_tools/decks/sg13cmos5l.py`'s module docstring for this
deck's full provenance notes, scope (`width`/`space`/`enclosing` DRC checks
across `Activ`/`GatPoly`/`Metal1`-`TopMetal1`/`Via1`-`Via3`/`TopVia1`; LV *and*
HV MOSFET LVS device class pairs, the three poly resistors, and the two MoM
capacitors), plus nominal metal parasitics (issue #2113), and what was
deliberately left un-transcribed and why (metal resistors, diodes, and the
MiM capacitor stack cmos5l's own forbidden-layer rule blocks outright).
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


def test_sg13cmos5l_derives_well_substrate_taps_from_implant_layers():
    """`EXTRACTION_DECK` declares no distinct drawn tap mask (`tap` stays
    `None`), but -- issue #1414, the cmos5l sibling of sg13g2.py's own
    `tap_nplus`/`tap_pplus` fix (#1273, itself mirroring gf180mcu's #1084)
    -- declares `tap_nplus`/`tap_pplus` so `extract.py` can derive an
    equivalent well-/substrate-tie region from the same `nSD`/`pSD` implant
    layers already used for MOS source/drain recognition. Unlike sg13g2,
    cmos5l's `well_label` is not `None` (`NWell.pin`, `31/2`) -- that field
    is unrelated to and unaffected by this fix (see its own docstring
    note)."""
    assert EXTRACTION_DECK.tap is None
    assert EXTRACTION_DECK.tap_nplus == (7, 0)  # nSD.drawing
    assert EXTRACTION_DECK.tap_pplus == (14, 0)  # pSD.drawing
    assert EXTRACTION_DECK.well_label == (31, 2)  # NWell.pin


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


def test_sg13cmos5l_poly_label_is_gatpoly_pin():
    """Issue #1476 (the cmos5l sibling of `sg13g2.py`'s own `poly_label`
    fix): `EXTRACTION_DECK.poly_label` is `GatPoly.pin` (5/2) -- kept
    consistent with this deck's own established `.pin`-purpose convention
    (`well_label`/`metal_labels` above all use datatype-2 `.pin`, not
    `.text`/`.label`), rather than the `.label` (5/1) purpose `sg13g2.py`
    picked for the identical `GatPoly` layer-numbering gap. See
    `EXTRACTION_DECK.poly_label`'s own inline comment for the full
    reasoning."""
    assert EXTRACTION_DECK.poly_label == (5, 2)  # GatPoly.pin


def _make_bare_poly_gate_nfet_layout(gate_label: str) -> kdb.Layout:
    """One NMOS whose gate is a bare `GatPoly` bar with NO gate contact/
    metal -- only a text on `GatPoly.pin` (5/2, `poly_label`) names it.
    Mirrors `sg13g2.py`'s own `_make_bare_poly_gate_nmos_layout` (issue
    #1476's sg13g2 test), adapted to cmos5l's own `poly_label` layer
    choice."""
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
    draw(5, 0, _box_um(0.8, -0.2, 1.2, 1.2))  # GatPoly.drawing gate, L=0.4um, bare

    draw(6, 0, _box_um(0.1, 0.3, 0.3, 0.7))  # Cont (source side)
    draw(6, 0, _box_um(1.7, 0.3, 1.9, 0.7))  # Cont (drain side)
    draw(8, 0, _box_um(0.0, 0.2, 0.4, 0.8))  # Metal1 (source pad)
    draw(8, 0, _box_um(1.6, 0.2, 2.0, 0.8))  # Metal1 (drain pad)
    label(8, 2, "S", 0.2, 0.5)  # Metal1.pin
    label(8, 2, "D", 1.8, 0.5)

    # The gate: labelled ONLY on GatPoly.pin (5/2), no Cont/Metal1 anywhere
    # near it.
    label(5, 2, gate_label, 1.0, 1.1)

    return layout


def test_sg13cmos5l_bare_poly_gate_named_via_poly_label(tmp_path: Path):
    """Issue #1476: a text on `GatPoly.pin` (5/2, `poly_label`) names a
    bare-poly gate that has no metal landing pad, so it survives extraction
    as a NAMED pin instead of an anonymous `$N` net -- and device extraction
    is unaffected (still exactly one nfet)."""
    path = _write_gds(
        _make_bare_poly_gate_nfet_layout("GATEN"), tmp_path / "bare_gate.gds"
    )
    report = run_extract(path, "sg13cmos5l", output=str(tmp_path / "bare_gate.spice"))

    assert report["device_counts"] == {"nfet": 1}
    (device,) = report["devices"]
    assert device["class"] == "nfet"
    assert device["nets"]["g"] == "GATEN"
    pin_names = {n["name"] for n in report["nets"] if n["pin"]}
    assert "GATEN" in pin_names


def test_sg13cmos5l_bare_poly_gate_anonymous_without_poly_label(tmp_path: Path):
    """Control: the same layout with NO poly label leaves the gate an
    anonymous `$N` net (the pre-#1476 friction this issue fixes), while
    device extraction is identical -- proving the `poly_label` text is what
    promotes the gate, not a geometry change."""
    layout = _make_bare_poly_gate_nfet_layout("UNUSED")
    # Drop the GatPoly.pin text, keeping every other shape/label.
    poly_pin = layout.layer(5, 2)
    layout.top_cell().shapes(poly_pin).clear()
    path = _write_gds(layout, tmp_path / "bare_gate_nolabel.gds")

    report = run_extract(path, "sg13cmos5l", output=str(tmp_path / "nolabel.spice"))
    assert report["device_counts"] == {"nfet": 1}
    (device,) = report["devices"]
    assert device["class"] == "nfet"
    # Anonymous, unbiasable -- KLayout's own auto-generated "$n" placeholder,
    # backslash-escaped to match the written netlist's own node spelling
    # (issue #1162).
    assert device["nets"]["g"].startswith("\\$")


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
    # absent while `resistors` was empty), preceded by the two MoM capacitor
    # classes issue #1466 added (`mom_capacitors` entries take the same
    # "device-class role" position `capacitors` entries would -- see
    # `ExtractionDeck.device_classes`). No diode/bipolar class joins them, so
    # this starter's remaining device-recognition gaps stay visible in the
    # same assertion. `capacitors` itself stays empty: cmos5l's only
    # capacitor family is the MoM one (#1463), and its marker-scoped,
    # topologically-matched shape does not fit `CapacitorDevice` -- see
    # `sg13cmos5l.py`'s "MoM capacitors" docstring section.
    assert EXTRACTION_DECK.device_classes == (
        "nfet",
        "pfet",
        "cap_cmomi",
        "cap_cmomf",
        "resistor",
    )
    assert EXTRACTION_DECK.capacitors == ()
    assert {c.name for c in EXTRACTION_DECK.mom_capacitors} == {
        "cap_cmomi",
        "cap_cmomf",
    }
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


# --------------------------------------------------------------------------- #
# MoM capacitors (issue #1466, the corrected follow-on to #1463)
# --------------------------------------------------------------------------- #


def test_sg13cmos5l_mom_capacitor_provenance_cites_cap_extraction_lvs():
    """Both curated MoM-capacitor entries cite the real
    `extract_devices(CapMomExtractor.new(...))` calls they were transcribed
    from, at the sibling-`ihp-sg13g2` commit `.github/ihp-sg13g2.ref` pins
    (`cap_extraction.lvs` is one of the rule decks cmos5l symlinks into that
    checkout -- see `sg13cmos5l.py`'s module docstring). That the devices are
    genuinely *cmos5l's* rather than symlinked-in-but-unused is established
    separately, by cmos5l's own non-symlinked `sg13cmos5l.lvs` `%include`ing
    both `cap_cmom{i,f}_derivations.lvs` -- see the same docstring's "MoM
    capacitors" section."""
    by_name = {c.name: c for c in EXTRACTION_DECK.mom_capacitors}
    assert set(by_name) == {"cap_cmomi", "cap_cmomf"}
    for name in ("cap_cmomi", "cap_cmomf"):
        assert by_name[name].provenance == RuleProvenance(
            source_repo="IHP-GmbH/IHP-Open-PDK",
            source_path=(
                "ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks/cap_extraction.lvs"
            ),
            rule_id=name,
            commit=_IHP_OPEN_PDK_COMMIT,
        )


def test_sg13cmos5l_mom_capacitor_metal_pins_stop_at_metal4():
    """`metal_pins` has one entry per `metals` level and reaches only
    `Metal4.pin` (50/2): cmos5l's stack is M1-M2-M3-M4-TopMetal1, so
    upstream's own shared `cap_extraction.lvs` `m5p` port
    (`Metal5.pin` 67/2) has no `metals` level to attach to here -- `Metal5`
    67/0 is on this deck's forbidden-layer list -- and `TopMetal1` is a level
    that call never wires a pin layer for in either family. `sg13g2.py`'s own
    entries, on the same symlinked rule text, do reach `Metal5.pin`."""
    assert [layer for layer, _dt in EXTRACTION_DECK.metals] == [8, 10, 30, 50, 126]
    for capacitor in EXTRACTION_DECK.mom_capacitors:
        assert len(capacitor.metal_pins) == len(EXTRACTION_DECK.metals)
        assert capacitor.metal_pins == (
            (8, 2),  # Metal1.pin
            (10, 2),  # Metal2.pin
            (30, 2),  # Metal3.pin
            (50, 2),  # Metal4.pin
            None,  # TopMetal1
        )
        assert (67, 2) not in capacitor.metal_pins  # Metal5.pin, unreachable here


def test_sg13cmos5l_mom_capacitor_flavours_are_told_apart_solely_by_marker_layer():
    """`cap_cmomi`/`cap_cmomf` share byte-identical recognition geometry and
    are told apart *only* by which marker layer is drawn -- upstream's own
    `custom_mom_extractor.lvs` comment ("The marker is the only thing that
    tells the two devices apart, so they must never share one")."""
    by_name = {c.name: c for c in EXTRACTION_DECK.mom_capacitors}
    assert by_name["cap_cmomi"].marker == (99, 39)  # Recog.mom
    assert by_name["cap_cmomf"].marker == (99, 40)  # Recog.momf
    assert by_name["cap_cmomi"].metal_pins == by_name["cap_cmomf"].metal_pins


def _make_sg13cmos5l_mom_layout(*, marker: tuple[int, int]) -> kdb.Layout:
    """A 8x3um MoM-capacitor marker with two `Metal1.pin` (8/2) port shapes
    side by side (the `double`/`none` PCell option -- both ports on the same
    metal level), each contacted out to its own labelled `Metal1` routing
    stub. Mirrors upstream's own `cap_cmom{i,f}_derivations.lvs` port
    derivation (`metal1_pin.and(marker)`); `marker` is the only layer that
    tells the two device flavours apart."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: tuple[int, int], box: kdb.Box) -> None:
        top.shapes(layout.layer(*layer)).insert(box)

    def label(layer: tuple[int, int], text: str, x: float, y: float) -> None:
        top.shapes(layout.layer(*layer)).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(marker, _box_um(0, 0, 8, 3))  # device marker, 8um x 3um
    draw((8, 2), _box_um(0, 0, 1, 1))  # Metal1.pin, PLUS
    draw((8, 2), _box_um(7, 2, 8, 3))  # Metal1.pin, MINUS
    draw((8, 0), _box_um(-2, 0, 0, 1))  # Metal1 routing stub off PLUS
    draw((8, 0), _box_um(8, 2, 10, 3))  # Metal1 routing stub off MINUS
    # NB: this deck's `metal_labels` are the `.pin` layers themselves
    # (8/2 etc.), not sg13g2's separate `.text` layers -- a text object is
    # not a polygon, so the same layer carries both a MoM port shape and a
    # net label without either reading the other.
    label((8, 2), "PLUS_NET", -1, 0.5)
    label((8, 2), "MINUS_NET", 9, 2.5)

    return layout


@pytest.mark.parametrize(
    "name, marker", [("cap_cmomi", (99, 39)), ("cap_cmomf", (99, 40))]
)
def test_golden_pair_sg13cmos5l_mom_capacitor_w_l_matches_marker_geometry(
    tmp_path: Path, name: str, marker: tuple[int, int]
):
    """A drawn 8x3um MoM-cap marker with two side-by-side `Metal1.pin` ports
    extracts as `cap_cmomi`/`cap_cmomf` with `w_um`/`l_um` read straight off
    the marker's own bounding box (`l` -> X extent, `w` -> Y extent, the real
    extractor's own axis mapping), both port nets on distinct terminals, and
    **no** `c_f`/`area_um2`/`perimeter_um` key at all -- this device's
    capacitance is supplied by its SPICE/Verilog-A model, not measured by
    `klt extract`. See `docs/json-contract.md`'s "MoM capacitor devices"
    note for that JSON-shape decision.

    `mmin`/`mmax` (issue #2435) are `1`/`1` here: this fixture draws no
    conductor *inside* the marker at all (its two `Metal1` routing stubs
    start at the marker edge and run outwards), so the measurement falls
    back to the two recognised ports' own metal levels -- both `Metal1`.
    That is the single-level `mmin == mmax` edge case, measured rather than
    defaulted."""
    path = _write_gds(_make_sg13cmos5l_mom_layout(marker=marker), tmp_path / "mom.gds")
    report = run_extract(path, "sg13cmos5l", output=str(tmp_path / "mom.spice"))

    assert report["device_counts"] == {name: 1}
    (device,) = report["devices"]
    assert device["class"] == name
    assert device["params"] == {
        "w_um": pytest.approx(3.0),
        "l_um": pytest.approx(8.0),
        "mmin": 1,
        "mmax": 1,
    }
    assert {device["nets"]["a"], device["nets"]["b"]} == {"PLUS_NET", "MINUS_NET"}


@pytest.mark.parametrize(
    "name, marker", [("cap_cmomi", (99, 39)), ("cap_cmomf", (99, 40))]
)
def test_sg13cmos5l_mom_capacitor_card_drops_params_keyword_and_suffixes_geometry(
    tmp_path: Path, name: str, marker: tuple[int, int]
):
    """Issue #2355: `cap_cmomi`/`cap_cmomf` have no curated
    `_CAPACITOR_MODEL_TABLE` entry (see `mom_capacitors`'s own docstring),
    so `write_device`'s unbound-device path owns their card -- and used to
    fall through to KLayout's own default primitive writer, which emits a
    `PARAMS:`-keyword card with bare-micron numbers (e.g. `XD_$1 a b
    cap_cmomi PARAMS: W=40 L=40`) that a SPICE parser under `.option
    scale=1` reads as 40 *metres*, a ~1e6x oversize that silently models a
    near-short capacitor in AC analysis. The fixed card drops `PARAMS:`,
    keeps `cap_cmomi`/`cap_cmomf` as the trailing subcircuit-name token
    (this device's own real upstream `.subckt` name), and formats `W=`/`L=`
    with the same `U`-suffix style this family's unbound resistor cards
    already use (`_format_um`'s default `GEOMETRY_STYLE_UNIT_SUFFIX`) --
    `w_um=3.0`/`l_um=8.0` (the golden pair test above) becomes `W=3U
    L=8U`. `devices[].params` itself is untouched -- this is a
    netlist-text-only formatting fix.

    The trailing `MMIN=1 MMAX=1` is issue #2435's own measured finger-stack
    range (see the golden pair test above for why this fixture measures a
    single level); it carries no unit suffix because a metal index is a
    dimensionless 1-based ordinal, not a length."""
    path = _write_gds(_make_sg13cmos5l_mom_layout(marker=marker), tmp_path / "mom.gds")
    report = run_extract(path, "sg13cmos5l", output=str(tmp_path / "mom.spice"))

    assert report["device_counts"] == {name: 1}
    (device,) = report["devices"]
    assert device["params"] == {
        "w_um": pytest.approx(3.0),
        "l_um": pytest.approx(8.0),
        "mmin": 1,
        "mmax": 1,
    }

    (card,) = [line for line in _device_cards(report) if f" {name} " in line]
    assert "PARAMS:" not in card
    assert card.startswith("X")
    assert card.endswith(f" {name} W=3U L=8U MMIN=1 MMAX=1")


#: The `cap_cmomi`/`cap_cmomf` `.subckt` parameters `klt extract` still does
#: NOT measure and therefore does NOT write onto the card. Verbatim
#: from IHP's own Apache-2.0 model libraries (`IHP-GmbH/ihp-sg13cmos5l` at
#: `607e18d4bd9214a52575c194b4181ef449f9252f`,
#: `libs.tech/ngspice/models/cap_cmomi.lib:60` / `cap_cmomf.lib:53`):
#:
#:   .subckt cap_cmomi PLUS MINUS w=5e-6 l=5e-6 mmin=1 mmax=4 feed=double
#:                                subblock=0 mm_ok=1
#:   .subckt cap_cmomf PLUS MINUS w=5e-6 l=5e-6 mmin=1 mmax=4 subblock=0
#:                                mm_ok=1
#:
#: (each is one line in the source, soft-wrapped here for width; `feed` is
#: declared on `cap_cmomi` only). Each tuple entry is the parameter name as
#: it would be spelled on a written card, matched case-insensitively.
#:
#: `mmin`/`mmax` were on this list until issue #2435 taught the recognition
#: step to measure them from the drawn per-metal finger geometry; they are
#: now written, and pinned by
#: `test_sg13cmos5l_mom_capacitor_card_writes_measured_metal_range` below.
#: The three that remain are the ones #2435 deliberately left out -- see
#: this module's `test_..._card_omits_unmeasured_pdk_subckt_params`
#: docstring for each one's reason.
_MOM_CAPACITOR_UNMEASURED_SUBCKT_PARAMS = ("feed", "subblock", "mm_ok")

#: The parameters a written `cap_cmomi`/`cap_cmomf` card DOES carry, in the
#: order the writer emits them (`_unbound_mom_capacitor_card` in
#: `pdk_models.py`): the two #2355 geometry parameters followed by issue
#: #2435's measured finger-stack metal range.
_MOM_CAPACITOR_WRITTEN_CARD_PARAMS = ["W", "L", "MMIN", "MMAX"]


@pytest.mark.parametrize(
    "name, marker", [("cap_cmomi", (99, 39)), ("cap_cmomf", (99, 40))]
)
def test_sg13cmos5l_mom_capacitor_card_omits_unmeasured_pdk_subckt_params(
    tmp_path: Path, name: str, marker: tuple[int, int]
):
    """Issue #2408's decision pin (option b: document the gap, do not
    fabricate defaults), **narrowed by issue #2435** to the parameters that
    are still genuinely unmeasured: a `--pdk`-bound MoM-capacitor card
    carries `W`/`L` plus the measured `MMIN`/`MMAX`, and never
    `feed`/`subblock`/`mm_ok` -- the remaining parameters of the upstream
    `.subckt` its trailing class-name token binds onto (see
    `_MOM_CAPACITOR_UNMEASURED_SUBCKT_PARAMS` above for the cited source and
    each one's PDK default).

    Those three are omitted deliberately, not accidentally:

    - `feed` (`cap_cmomi` only) is only *partly* recoverable, and guessing
      the rest would be exactly the invisible wrong answer #2408 rejected.
      The PCell's `same` variant is distinguishable -- it stacks its two
      pins on adjacent metals, so the two recognised ports carry different
      metal indices -- but `double` and `none` both place two ports on the
      top metal, told apart only by where those ports sit relative to the
      core, which this recognition step does not know. `none` is upstream's
      own documented *not a standalone 2-terminal device* configuration, so
      writing `feed=double` for it would misreport a device that is not even
      complete. Split out as issue #2445 rather than settled here.
    - `subblock` is a PCell layout switch (substrate isolation block) with no
      counterpart in the recognition geometry at all.
    - `mm_ok` is a documented no-op in this release ("accepted for interface
      parity ... but a NO-OP -- cap_cmom{i,f} has no characterised mismatch
      model", same `.lib` headers), so omitting it costs nothing today.

    So this remains a *pin*, not an aspiration: if a future change starts
    writing any of the three, that must be a deliberate decision with the
    value actually recovered from layout -- the bar `mmin`/`mmax` cleared in
    #2435 -- and this test is the place it gets re-argued. What is and is
    not written is documented for consumers in `docs/cli/extract.md`'s "MoM
    capacitor devices" section.
    """
    path = _write_gds(_make_sg13cmos5l_mom_layout(marker=marker), tmp_path / "mom.gds")
    report = run_extract(
        path,
        "sg13cmos5l",
        pdk_variant="ihp-sg13cmos5l",
        pdk_root=_make_pdk_install(tmp_path),
        output=str(tmp_path / "mom.spice"),
    )

    assert report["device_counts"] == {name: 1}

    (card,) = [line for line in _device_cards(report) if f" {name} " in line]

    lowered = card.lower()
    for param in _MOM_CAPACITOR_UNMEASURED_SUBCKT_PARAMS:
        assert f"{param}=" not in lowered, (
            f"card writes '{param}=', a PDK subckt parameter `klt extract` "
            f"never measures -- see this test's docstring (issue #2408): {card}"
        )

    # Positive half of the pin: exactly the measured parameters are written,
    # so the loop above cannot pass by the card having lost its parameters
    # altogether.
    assert [token.split("=", 1)[0] for token in card.split() if "=" in token] == (
        _MOM_CAPACITOR_WRITTEN_CARD_PARAMS
    )

    # `--pdk` does not change this card: `cap_cmom*` has no curated model
    # binding to resolve (see `mom_capacitors`'s own docstring), so the
    # unbound-device writer owns it either way -- same #2355 shape as the
    # no-`--pdk` test above.
    assert card.endswith(f" {name} W=3U L=8U MMIN=1 MMAX=1")


#: This deck's four MoM-reachable thin-metal levels: 1-based metal index ->
#: (drawn `Metal<n>` layer, `Metal<n>.pin` layer). `TopMetal1` (index 5) is
#: deliberately absent -- `mom_capacitors[].metal_pins` leaves it `None`, so
#: it is neither a port level nor a finger level (see
#: `test_..._ignores_routing_on_a_level_the_device_family_never_reaches`).
_CMOS5L_MOM_METAL_LAYERS = {
    1: ((8, 0), (8, 2)),
    2: ((10, 0), (10, 2)),
    3: ((30, 0), (30, 2)),
    4: ((50, 0), (50, 2)),
}


def _make_sg13cmos5l_mom_finger_stack_layout(
    *,
    marker: tuple[int, int],
    levels: tuple[int, ...],
    port_level: int | None = None,
) -> kdb.Layout:
    """An 8x3um MoM-capacitor marker whose fingers are drawn on exactly the
    metal `levels` given (1-based, e.g. `(2, 3)` for a Metal2..Metal3 stack),
    with its two ports side by side on `port_level` (the topmost of them by
    default, where the real PCell puts them).

    Mirrors the real PCell's shape closely enough for the recovery under
    test: every level in `mmin..mmax` carries the same bar/tooth pattern
    (`cmomi_code.py`'s "Every metal layer mmin..mmax carries the same
    pattern"), all of it enclosed by the recognition marker, and the two
    `MkPin` ports sit on one of those levels.

    Each port's net is carried out to its label by a routing stub on the
    same metal, running from the port's own bar out past the marker edge --
    so `port_level`'s bars merge with a shape that leaves the marker and are
    (correctly) not counted as enclosed finger geometry. That level is still
    measured, via the recognised ports themselves; it is exactly the real
    case where the design routes to the capacitor's top-metal feed pad,
    while the levels below reach it only through via stacks and so stay
    wholly inside the marker."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: tuple[int, int], box: kdb.Box) -> None:
        top.shapes(layout.layer(*layer)).insert(box)

    def label(layer: tuple[int, int], text: str, x: float, y: float) -> None:
        top.shapes(layout.layer(*layer)).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(marker, _box_um(0, 0, 8, 3))  # device marker, 8um x 3um

    for level in levels:
        drawn, _pin = _CMOS5L_MOM_METAL_LAYERS[level]
        # Two opposite-polarity horizontal "bars", each with an interdigitated
        # "tooth" reaching towards (but never touching) the other -- all
        # strictly inside the 8x3um marker, and never bridging the two combs
        # into one net, exactly like the real interdigitated PCell.
        draw(drawn, _box_um(0.5, 0.4, 7.5, 0.8))  # PLUS bar
        draw(drawn, _box_um(2.0, 0.8, 2.4, 1.8))  # PLUS tooth, upwards
        draw(drawn, _box_um(0.5, 2.2, 7.5, 2.6))  # MINUS bar
        draw(drawn, _box_um(5.0, 1.2, 5.4, 2.2))  # MINUS tooth, downwards

    level_for_ports = max(levels) if port_level is None else port_level
    port_drawn, port_pin = _CMOS5L_MOM_METAL_LAYERS[level_for_ports]
    draw(port_pin, _box_um(0.5, 0.4, 1.5, 0.8))  # PLUS port
    draw(port_pin, _box_um(6.5, 2.2, 7.5, 2.6))  # MINUS port
    draw(port_drawn, _box_um(-2, 0.4, 0.5, 0.8))  # routing stub off PLUS
    draw(port_drawn, _box_um(7.5, 2.2, 10, 2.6))  # routing stub off MINUS
    # NB: this deck's `metal_labels` are the `.pin` layers themselves, so a
    # text object and a port polygon share one layer without either reading
    # the other (see `_make_sg13cmos5l_mom_layout` above).
    label(port_pin, "PLUS_NET", -1, 0.6)
    label(port_pin, "MINUS_NET", 9, 2.4)

    return layout


@pytest.mark.parametrize(
    "name, marker", [("cap_cmomi", (99, 39)), ("cap_cmomf", (99, 40))]
)
@pytest.mark.parametrize(
    "levels, mmin, mmax",
    [
        ((1, 2, 3, 4), 1, 4),
        ((2, 3), 2, 3),
        ((3,), 3, 3),
    ],
    ids=["full-stack", "metal2-metal3", "single-level"],
)
def test_golden_pair_sg13cmos5l_mom_capacitor_measures_drawn_finger_stack(
    tmp_path: Path,
    name: str,
    marker: tuple[int, int],
    levels: tuple[int, ...],
    mmin: int,
    mmax: int,
):
    """Issue #2435: `mmin`/`mmax` are the lowest/highest metal index actually
    carrying drawn finger geometry inside the recognition marker -- measured,
    never defaulted to the `.subckt`'s own `mmin=1 mmax=4`.

    Three cases, all on the same fixture generator:

    - **full-stack** `Metal1..Metal4` -- this deck's whole MoM-reachable
      thin-metal stack (its `Metal5` is forbidden and its `TopMetal1` is not
      a level this device family reaches), the case that used to be silently
      *assumed* by the `.subckt` defaults and is now confirmed;
    - **metal2-metal3** -- a non-default stack, the case a defaulted
      `mmin=1 mmax=4` modelled at the wrong layer count `N = mmax - mmin + 1`
      (4 instead of 2, i.e. ~1.09 vs ~0.55 fF/um² of area density on the
      PCell's own coefficient table);
    - **single-level** -- the `mmin == mmax` edge case (`N = 1`).
    """
    path = _write_gds(
        _make_sg13cmos5l_mom_finger_stack_layout(marker=marker, levels=levels),
        tmp_path / "mom.gds",
    )
    report = run_extract(path, "sg13cmos5l", output=str(tmp_path / "mom.spice"))

    assert report["device_counts"] == {name: 1}
    (device,) = report["devices"]
    assert device["class"] == name
    assert device["params"] == {
        "w_um": pytest.approx(3.0),
        "l_um": pytest.approx(8.0),
        "mmin": mmin,
        "mmax": mmax,
    }
    assert {device["nets"]["a"], device["nets"]["b"]} == {"PLUS_NET", "MINUS_NET"}
    assert not [w for w in report["warnings"] if name in w]


def test_sg13cmos5l_mom_capacitor_measures_levels_no_port_sits_on(tmp_path: Path):
    """Issue #2435: the *drawn* finger geometry -- not the two recognised
    ports -- is what sets the range when they disagree.

    Fingers on `Metal1..Metal3` with both ports down on `Metal1`: the ports
    alone would say `mmin = mmax = 1` (`N = 1`), the pre-#2435 `.subckt`
    defaults would say `1..4` (`N = 4`), and only reading `Metal2`/`Metal3`'s
    own enclosed geometry gives the drawn `1..3` (`N = 3`). This is the half
    of the measurement the port union can never supply, so it is pinned
    separately from the PCell-shaped cases above."""
    path = _write_gds(
        _make_sg13cmos5l_mom_finger_stack_layout(
            marker=(99, 39), levels=(1, 2, 3), port_level=1
        ),
        tmp_path / "mom_ports_low.gds",
    )
    report = run_extract(
        path, "sg13cmos5l", output=str(tmp_path / "mom_ports_low.spice")
    )

    assert report["device_counts"] == {"cap_cmomi": 1}
    (device,) = report["devices"]
    assert device["params"]["mmin"] == 1
    assert device["params"]["mmax"] == 3
    assert {device["nets"]["a"], device["nets"]["b"]} == {"PLUS_NET", "MINUS_NET"}


@pytest.mark.parametrize(
    "name, marker", [("cap_cmomi", (99, 39)), ("cap_cmomf", (99, 40))]
)
def test_sg13cmos5l_mom_capacitor_card_writes_measured_metal_range(
    tmp_path: Path, name: str, marker: tuple[int, int]
):
    """Issue #2435, the writer half: a `--pdk`-bound card for a device whose
    fingers were drawn on `Metal2..Metal3` carries `MMIN=2 MMAX=3`, not the
    upstream `.subckt`'s own `mmin=1 mmax=4` defaults it would otherwise
    silently take (and not the `mmin`/`mmax`-less card #2408 pinned).

    Uppercase to match the `W=`/`L=` already on the same card; SPICE
    subcircuit parameter names are case-insensitive, so both bind onto the
    lowercase `w`/`l`/`mmin`/`mmax` the `.subckt` declares."""
    path = _write_gds(
        _make_sg13cmos5l_mom_finger_stack_layout(marker=marker, levels=(2, 3)),
        tmp_path / "mom.gds",
    )
    report = run_extract(
        path,
        "sg13cmos5l",
        pdk_variant="ihp-sg13cmos5l",
        pdk_root=_make_pdk_install(tmp_path),
        output=str(tmp_path / "mom.spice"),
    )

    assert report["device_counts"] == {name: 1}
    (card,) = [line for line in _device_cards(report) if f" {name} " in line]
    assert card.endswith(f" {name} W=3U L=8U MMIN=2 MMAX=3")


@pytest.mark.parametrize(
    "name, marker", [("cap_cmomi", (99, 39)), ("cap_cmomf", (99, 40))]
)
def test_sg13cmos5l_mom_capacitor_ignores_routing_that_crosses_the_marker(
    tmp_path: Path, name: str, marker: tuple[int, int]
):
    """Issue #2435: an unrelated `Metal4` route running *across* a
    `Metal2..Metal3` MoM capacitor must not be counted as a finger level.

    Routing over a capacitor is ordinary layout practice, and the marker is
    painted over the device's full extent, so any crossing shape overlaps it.
    Only conductor polygons lying **entirely inside** the marker count as
    fingers (`extract.py`'s `inside()` narrowing) -- a crossing route leaves
    the footprint on both sides, so it is excluded and `mmax` stays `3`.
    Without that narrowing this device would be reported as a 3-layer
    (`N = 3`) stack instead of the 2-layer one that was drawn."""
    layout = _make_sg13cmos5l_mom_finger_stack_layout(marker=marker, levels=(2, 3))
    top = layout.cell("TOP")
    # Metal4 route crossing the whole 8x3um marker, plus its own net label.
    top.shapes(layout.layer(50, 0)).insert(_box_um(-4, 1.2, 12, 1.6))
    top.shapes(layout.layer(50, 2)).insert(
        kdb.Text("FLYOVER", kdb.Trans(round(11 / _DBU_UM), round(1.4 / _DBU_UM)))
    )

    path = _write_gds(layout, tmp_path / "mom_flyover.gds")
    report = run_extract(path, "sg13cmos5l", output=str(tmp_path / "mom_flyover.spice"))

    assert report["device_counts"] == {name: 1}
    (device,) = report["devices"]
    assert device["params"]["mmin"] == 2
    assert device["params"]["mmax"] == 3


@pytest.mark.parametrize(
    "name, marker", [("cap_cmomi", (99, 39)), ("cap_cmomf", (99, 40))]
)
def test_sg13cmos5l_mom_capacitor_ignores_metal_the_device_family_cannot_reach(
    tmp_path: Path, name: str, marker: tuple[int, int]
):
    """Issue #2435: drawn geometry on a level this entry's `metal_pins`
    leaves `None` -- cmos5l's `TopMetal1`, which no `cap_cmom*` instance ever
    populates -- is never read as a finger level, even when it lies entirely
    inside the marker.

    `metal_pins` is the deck's own statement of which levels this device
    family reaches, so a `TopMetal1` shape inside the marker is by
    construction *not* a finger -- it is a landing pad, a shield, or a
    stub of some other structure. Counting it would report a `Metal2..
    TopMetal1` stack (`N = 4`) for a device drawn on two levels."""
    layout = _make_sg13cmos5l_mom_finger_stack_layout(marker=marker, levels=(2, 3))
    top = layout.cell("TOP")
    # TopMetal1 pad wholly inside the 8x3um marker.
    top.shapes(layout.layer(126, 0)).insert(_box_um(2, 1.0, 6, 1.8))

    path = _write_gds(layout, tmp_path / "mom_topmetal.gds")
    report = run_extract(
        path, "sg13cmos5l", output=str(tmp_path / "mom_topmetal.spice")
    )

    assert report["device_counts"] == {name: 1}
    (device,) = report["devices"]
    assert device["params"]["mmin"] == 2
    assert device["params"]["mmax"] == 3


def test_sg13cmos5l_mom_capacitor_non_contiguous_stack_warns(tmp_path: Path):
    """Issue #2435: a measured range with a *gap* in it is reported through
    `warnings[]` rather than silently accepted.

    A real finger stack is contiguous by construction (the PCell paints every
    level in `mmin..mmax`), so `Metal1` + `Metal3` fingers with nothing on
    `Metal2` means the marker encloses something this measurement cannot tell
    from a finger -- most plausibly a routed shape that both begins and ends
    inside the marker footprint. The range is still measured as the observed
    min/max (`1`..`3`), never regressed to a PDK default; the warning is what
    makes the ambiguity visible."""
    path = _write_gds(
        _make_sg13cmos5l_mom_finger_stack_layout(marker=(99, 39), levels=(1, 3)),
        tmp_path / "mom_gap.gds",
    )
    report = run_extract(path, "sg13cmos5l", output=str(tmp_path / "mom_gap.spice"))

    assert report["device_counts"] == {"cap_cmomi": 1}
    (device,) = report["devices"]
    assert device["params"]["mmin"] == 1
    assert device["params"]["mmax"] == 3
    assert [
        w
        for w in report["warnings"]
        if "cap_cmomi" in w and "Metal2" in w and "contiguous" in w
    ]


def test_sg13cmos5l_mom_capacitor_stacked_ports_stay_on_separate_metal_nets(
    tmp_path: Path,
):
    """The `same`-feed PCell configuration stacks the two ports on *adjacent*
    metal levels at identical (x, y) rather than side by side -- the case a
    two-different-layer `CapacitorDevice` split cannot express at all. Both
    ports must still resolve to their own metal's distinct net. Drawn on
    `Metal3`/`Metal4` here (this deck's top two thin-metal levels, since
    `Metal5` is forbidden), where `sg13g2.py`'s own sibling test uses
    `Metal4`/`Metal5`."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: tuple[int, int], box: kdb.Box) -> None:
        top.shapes(layout.layer(*layer)).insert(box)

    def label(layer: tuple[int, int], text: str, x: float, y: float) -> None:
        top.shapes(layout.layer(*layer)).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw((99, 40), _box_um(0, 0, 5, 5))  # Recog.momf marker
    draw((30, 2), _box_um(1, 1, 2, 2))  # Metal3.pin
    draw((50, 2), _box_um(1, 1, 2, 2))  # Metal4.pin, same (x, y) -- stacked
    draw((30, 0), _box_um(-3, 1, 1, 2))  # Metal3 routing stub
    draw((50, 0), _box_um(2, 1, 5, 2))  # Metal4 routing stub
    label((30, 2), "M3_NET", -2, 1.5)  # this deck labels on the .pin layers
    label((50, 2), "M4_NET", 4, 1.5)

    path = _write_gds(layout, tmp_path / "cap_cmomf_stacked.gds")
    report = run_extract(
        path, "sg13cmos5l", output=str(tmp_path / "cap_cmomf_stacked.spice")
    )

    assert report["device_counts"] == {"cap_cmomf": 1}
    (device,) = report["devices"]
    assert {device["nets"]["a"], device["nets"]["b"]} == {"M3_NET", "M4_NET"}
    # `mmin`/`mmax` (issue #2435) follow the stacked ports' own levels: both
    # routing stubs leave the marker, so no conductor is enclosed by it and
    # the two `Metal3`/`Metal4` ports are the whole measurement.
    assert device["params"] == {
        "w_um": pytest.approx(5.0),
        "l_um": pytest.approx(5.0),
        "mmin": 3,
        "mmax": 4,
    }


def test_sg13cmos5l_is_registered_for_parasitics_extraction(tmp_path: Path):
    """Registry support (#1440) now includes usable full-stack R/C (#2113)."""
    from klayout_tools.decks import get_parasitics_deck, sg13cmos5l

    assert get_parasitics_deck("sg13cmos5l") is sg13cmos5l.PARASITICS

    assert len(sg13cmos5l.PARASITICS.metals) == len(EXTRACTION_DECK.metals) == 5
    assert len(sg13cmos5l.PARASITICS.metal_overlaps) == 4
    path = _write_gds(
        _make_nfet_layout_routed_through_full_metal_stack(), tmp_path / "nfet.gds"
    )
    report = run_extract(
        path, "sg13cmos5l", output=str(tmp_path / "nfet.spice"), parasitics=True
    )

    assert report["status"] == "extracted"
    parasitics = report["parasitics"]
    assert parasitics["r_count"] == 3
    assert parasitics["c_count"] == 3
    assert parasitics["total_resistance_ohm"] > 0
    assert parasitics["total_capacitance_ff"] > 0
    assert parasitics["metals_without_coefficient"] == []
    assert parasitics["overlap_pairs_without_coefficient"] == []
    assert parasitics["cc_count"] == 0  # The via-connected stack is one net.
    assert not any(
        "PARASITICS.metals has no R/C coefficient" in w for w in report["warnings"]
    )


@pytest.mark.parametrize(
    ("layer", "resistance_ohm", "capacitance_ff"),
    [
        (8, 1.10, 3.138072),
        (10, 0.88, 2.290960),
        (30, 0.88, 1.828668),
        (50, 0.88, 1.643160),
        (126, 0.18, 1.788268),
    ],
    ids=["Metal1", "Metal2", "Metal3", "Metal4", "TopMetal1"],
)
def test_sg13cmos5l_parasitics_rectangular_wire(
    tmp_path: Path, layer: int, resistance_ohm: float, capacitance_ff: float
):
    """20x2um: 10 squares, area 40um², perimeter 44um. Expected values are
    hand-computed from the pinned CMOS5L Magic source, not the Python table.
    TopMetal1 must differ from both SG13G2 Metal5 and SG13G2 TopMetal1."""
    layout = kdb.Layout()
    layout.dbu = _DBU_UM
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(layer, 0)).insert(_box_um(0, 0, 20, 2))
    top.shapes(layout.layer(layer, 2)).insert(kdb.Text("WIRE", 1000, 1000))
    path = _write_gds(layout, tmp_path / "wire.gds")
    report = run_extract(
        path, "sg13cmos5l", output=str(tmp_path / "wire.spice"), parasitics=True
    )

    parasitics = report["parasitics"]
    assert parasitics["r_count"] == parasitics["c_count"] == 1
    (wire,) = parasitics["nets"]
    assert wire["net"] == "WIRE"
    assert wire["resistance_ohm"] == pytest.approx(resistance_ohm)
    assert wire["capacitance_ff"] == pytest.approx(capacitance_ff)
    assert parasitics["cc_count"] == 0


@pytest.mark.parametrize(
    ("lower", "upper", "index", "coupling_ff", "lower_ground_ff", "upper_ground_ff"),
    [
        (8, 10, 0, 2.689, 1.737472, 1.563760),
        (10, 30, 1, 2.689, 1.563760, 1.348908),
        (30, 50, 2, 2.689, 1.348908, 1.285240),
        (50, 126, 3, 1.70832, 1.285240, 1.519188),
    ],
    ids=["Metal1-Metal2", "Metal2-Metal3", "Metal3-Metal4", "Metal4-TopMetal1"],
)
def test_sg13cmos5l_parasitics_distinct_net_overlap(
    tmp_path: Path,
    lower: int,
    upper: int,
    index: int,
    coupling_ff: float,
    lower_ground_ff: float,
    upper_ground_ff: float,
):
    """Two unconnected 20x2um plates couple over 40um². Ground area is fully
    deducted on both nets, leaving each plate's 44um perimeter fringe term."""
    layout = kdb.Layout()
    layout.dbu = _DBU_UM
    top = layout.create_cell("TOP")
    for layer, name in ((lower, "LOWER"), (upper, "UPPER")):
        top.shapes(layout.layer(layer, 0)).insert(_box_um(0, 0, 20, 2))
        top.shapes(layout.layer(layer, 2)).insert(kdb.Text(name, 1000, 1000))
    path = _write_gds(layout, tmp_path / "overlap.gds")
    netlist_path = tmp_path / "overlap.spice"
    report = run_extract(path, "sg13cmos5l", output=str(netlist_path), parasitics=True)

    parasitics = report["parasitics"]
    assert parasitics["cc_count"] == 1
    assert parasitics["total_coupling_capacitance_ff"] == pytest.approx(coupling_ff)
    by_net = {net["net"]: net for net in parasitics["nets"]}
    assert set(by_net) == {"LOWER", "UPPER"}
    assert by_net["LOWER"]["capacitance_ff"] == pytest.approx(lower_ground_ff)
    assert by_net["UPPER"]["capacitance_ff"] == pytest.approx(upper_ground_ff)
    (coupling,) = by_net["LOWER"]["coupled"]
    assert coupling["net"] == "UPPER"
    assert coupling["levels"] == [[index, index + 1]]
    assert coupling["capacitance_ff"] == pytest.approx(coupling_ff)
    (card,) = [
        line
        for line in netlist_path.read_text().splitlines()
        if line.startswith("Ccc_")
    ]
    fields = card.split()
    assert set(fields[1:3]) == {"LOWER__par", "UPPER__par"}
    assert float(fields[3]) == pytest.approx(coupling_ff * 1e-15, rel=1e-6, abs=0)
