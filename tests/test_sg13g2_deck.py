"""Tests for the sg13g2 DRC/LVS deck (Epic #711 Phase 3b, issue #905 --
"Compile an SG13G2 DRC/LVS deck with `klt deck`, the second PDK-generality
proof").

Mirrors `tests/test_lvs_device_provenance.py`'s own structure for the
LVS-device side of sky130/gf180mcu (issue #868/#867), scoped to sg13g2's
`EXTRACTION_DECK`:

- **Provenance-populated assertions** -- `EXTRACTION_DECK.nfet_provenance`/
  `.pfet_provenance` cite a real, verifiable `mos_extraction.lvs`
  `extract_devices(mos4(...))` call (see `sg13g2.py`'s own module docstring
  for the exact citation and the IHP-Open-PDK v0.3.0 pin).
- **Golden layout->netlist pairs** -- a minimal, hand-computed synthetic
  NMOS/PMOS layout, run through `run_extract`, whose extracted `l_um`/`w_um`
  exactly match the drawn geometry -- validating the model end-to-end (PDK
  source citation -> deck fields -> extracted netlist), not just that the
  citation string is present.
- **Coverage discipline** -- every `EXTRACTION_DECK` entry with a populated
  `provenance` has a matching golden-pair test, mirroring
  `test_golden_pairs_cover_every_provenanced_sky130_device_rule`.

The DRC-side rule tests (width/space golden pairs, provenance-populated
assertions for all 19 rules, and the 5 enclosing/separation rules' hand-
written violate/clean pairs) live in `tests/test_golden_deck.py` (generic,
parametrized by `DECK_NAMES`) and `tests/test_drc.py`'s own "sg13g2" section
respectively -- not duplicated here.

**No #524 cross-check** (issue #905's own acceptance criterion: "cross-check
against #524 if it has landed by the time this is implemented; otherwise
state explicitly that no hand-written deck existed"): issue #524 ("Curated
SG13G2 (IHP-Open-PDK) DRC/LVS deck for `klt drc`/`klt lvs`") remains open
and unmerged as of this module's authoring -- there is no second,
independently hand-written `sg13g2` deck in this repo to diff this one
against. See `sg13g2.py`'s own module docstring, "No #524 cross-check"
section, for the full explanation.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.decks import RuleProvenance
from klayout_tools.decks import sg13g2 as sg13g2_deck_module
from klayout_tools.decks.sg13g2 import EXTRACTION_DECK
from klayout_tools.extract import run_extract

_DBU_UM = 0.001
_IHP_OPEN_PDK_COMMIT = "5cccb161f7492697cfa52eb14dc03beb00bdca9e"  # v0.3.0 tag


def _box_um(x0: float, y0: float, x1: float, y1: float) -> kdb.Box:
    """A `kdb.Box` from micrometre coordinates at sg13g2's own
    `NOMINAL_DBU_UM` convention (1 nm/unit) -- mirrors
    `test_lvs_device_provenance.py`'s own `_box_um` helper."""
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
# Provenance populated (issue #905 AC: "every compiled rule cites its
# SG13G2 PDK source line")
# --------------------------------------------------------------------------- #


def test_sg13g2_mos_provenance_cites_mos_extraction_lvs():
    """`EXTRACTION_DECK.nfet_provenance`/`pfet_provenance` cite the real
    `mos_extraction.lvs` `mos4('sg13_lv_nmos'/'sg13_lv_pmos')`
    `extract_devices` calls (verified against a real IHP-Open-PDK v0.3.0
    install fetched by `scripts/fetch-ihp-sg13g2.sh`)."""
    assert EXTRACTION_DECK.nfet_provenance == RuleProvenance(
        source_repo="IHP-GmbH/IHP-Open-PDK",
        source_path=(
            "ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks/mos_extraction.lvs"
        ),
        rule_id="sg13_lv_nmos",
        commit=_IHP_OPEN_PDK_COMMIT,
    )
    assert EXTRACTION_DECK.pfet_provenance == RuleProvenance(
        source_repo="IHP-GmbH/IHP-Open-PDK",
        source_path=(
            "ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks/mos_extraction.lvs"
        ),
        rule_id="sg13_lv_pmos",
        commit=_IHP_OPEN_PDK_COMMIT,
    )


def test_sg13g2_extraction_deck_curated_device_families():
    """Issue #1231 curates MOS (thin- *and* thick-oxide) plus the two
    unambiguous poly-resistor flavours; issue #1235 adds the third poly
    resistor (`rhigh`, its sheet-rho ambiguity resolved -- see `sg13g2.py`'s
    resistor note) and the two metal resistors that fit inside this deck's
    curated Metal1/Metal2 stack (`res_metal1`/`res_metal2`); issue #1234
    additionally curates the two antenna diodes.

    `capacitors` is populated as of issue #1454, which closed out the
    deferral #1233 opened: #1233 investigated `cap_cmim`/`rfcmim` and found
    both plates land on Metal5/TopMetal1, above this deck's then-current
    Metal1/Via1/Metal2 stack -- declaring the entry then would have
    recognised an isolated-node capacitor (`CapacitorDevice`'s own
    documented "Known limitation"), so recognition was deferred behind the
    stack extension itself (issue #1243, shared with #1235's metal
    resistors). #1243 landed via PR #1247 (`metals`/`vias` now reach
    TopMetal2), so both entries below set
    `top_plate_via`/`top_plate_via_metal` on the first pass. See
    `sg13g2.py`'s "MIM capacitors" docstring section for the full history.

    `bipolars` staying empty is a deferral of a different kind: issue #1232
    *investigated* populating it and found the stock `BipolarDevice` model
    cannot faithfully express SG13G2's own `CustomBJTExtractor`-based
    derivation (see `sg13g2.py`'s "SiGe HBTs -- investigated, declined"
    docstring section for the full finding) -- see
    `test_sg13g2_bipolars_declined_after_investigation` below for a test
    that documents *why*, not just *that*. Issue #1234's own investigation of
    `schottky_nbl1` reached the same "declined" conclusion for a different
    reason (a fixed-size, size-filtered emitter region and a dynamic
    per-instance collector derivation, not a compound multi-layer marker) --
    see `test_sg13g2_schottky_nbl1_declined_after_investigation` below.

    `mom_capacitors` is populated as of issue #1466 (found while
    investigating #1463): `cap_cmomi`/`cap_cmomf`, a structurally distinct
    MoM capacitor family `CapacitorDevice` cannot represent -- see
    `sg13g2.py`'s "MoM capacitors" docstring section for the full history.

    Named explicitly here so a future extension of this deck must update
    this assertion, rather than silently leaving the coverage-discipline
    test below out of sync."""
    assert {c.name for c in EXTRACTION_DECK.capacitors} == {"cap_cmim", "rfcmim"}
    assert {c.name for c in EXTRACTION_DECK.mom_capacitors} == {
        "cap_cmomi",
        "cap_cmomf",
    }
    assert EXTRACTION_DECK.bipolars == ()
    assert {d.name for d in EXTRACTION_DECK.diodes} == {"dantenna", "dpantenna"}
    assert {r.name for r in EXTRACTION_DECK.resistors} == {
        "rsil",
        "rppd",
        "rhigh",
        "res_metal1",
        "res_metal2",
    }
    assert EXTRACTION_DECK.device_classes == (
        "nfet",
        "pfet",
        "cap_cmim",
        "rfcmim",
        "cap_cmomi",
        "cap_cmomf",
        "resistor",
        "dantenna",
        "dpantenna",
    )


def test_sg13g2_mim_capacitors_wire_both_plates_into_the_tracked_metal_stack():
    """Issue #1233's own deferral condition, pinned as a regression test:
    the reason `cap_cmim`/`rfcmim` waited for #1243 is that a MiM cap whose
    plates sit above the deck's tracked `metals[]` stack extracts as a
    correctly-valued device between two nets nothing else in the graph
    touches (`CapacitorDevice`'s "Known limitation"). Both plates must now
    resolve into that stack -- Metal5 as the `bottom_plate` directly,
    TopMetal1 through `top_plate_via_metal` -- or the entries have
    regressed to the isolated-node state #1233 declined to ship."""
    for capacitor in EXTRACTION_DECK.capacitors:
        assert capacitor.bottom_plate == (67, 0)  # Metal5.drawing
        assert capacitor.bottom_plate in EXTRACTION_DECK.metals
        assert capacitor.top_plate_via == (129, 0)  # Vmim.drawing
        assert capacitor.top_plate_via_metal == (126, 0)  # TopMetal1.drawing
        assert capacitor.top_plate_via_metal in EXTRACTION_DECK.metals


def test_sg13g2_capacitor_provenance_cites_cap_extraction_lvs():
    """Both curated MIM-capacitor entries carry a `provenance` citing the
    real `cap_extraction.lvs` `MIMCAPExtractor.new(...)` call that defines
    them upstream (issue #1454, mirroring the resistor/diode entries'
    own citations)."""
    for capacitor in EXTRACTION_DECK.capacitors:
        assert capacitor.provenance == RuleProvenance(
            source_repo="IHP-GmbH/IHP-Open-PDK",
            source_path=(
                "ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks/cap_extraction.lvs"
            ),
            rule_id=capacitor.name,
            commit=_IHP_OPEN_PDK_COMMIT,
        )


def test_sg13g2_bipolars_declined_after_investigation():
    """Issue #1232's own finding, pinned as a regression test: SG13G2's
    real `bjt_extraction.lvs` recognises its SiGe HBTs
    (`npn13G2`/`npn13G2l`/`npn13G2v`/`pnpMPA`) through a custom Ruby
    `CustomBJTExtractor` (`custom_bjt_extractor.lvs`) -- not KLayout's stock
    `DeviceExtractorBJT3Transistor` that `BipolarDevice`/`extract.py`'s
    bipolar-recognition wiring assumes -- with a device marker that is
    itself a 3-layer compound boolean (`trans_drw.and(pwell)
    .and(ptap_holes)`, and `pwell` is not even a literal drawn layer in
    this PDK) and terminal pins distinguished by drawn bounding-box/area
    filters (`with_bbox_min`/`with_bbox_max`/`with_area`) this engine's
    device-recognition primitives have no equivalent for.

    This test exists so that if a future change ever populates
    `EXTRACTION_DECK.bipolars` without revisiting this finding, it fails
    loudly here rather than silently landing a mapping nobody re-verified
    against the real PDK derivation -- the exact "self-consistent golden
    pair that still does not match the PDK's real connectivity" failure
    mode issue #1232's own acceptance criteria warned against."""
    assert EXTRACTION_DECK.bipolars == ()
    assert "bjt" not in EXTRACTION_DECK.device_classes


def test_sg13g2_resistor_provenance_cites_res_extraction_lvs():
    """All five curated resistor entries cite the real `res_extraction.lvs`
    `extract_devices(...)` call they were transcribed from (`rsil`/`rppd`:
    issue #1231; `rhigh`/`res_metal1`/`res_metal2`: issue #1235), with the
    sheet resistance taken from the PDK's own citable constants."""
    by_name = {r.name: r for r in EXTRACTION_DECK.resistors}
    for name in ("rsil", "rppd", "rhigh", "res_metal1", "res_metal2"):
        assert by_name[name].provenance == RuleProvenance(
            source_repo="IHP-GmbH/IHP-Open-PDK",
            source_path=(
                "ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks/res_extraction.lvs"
            ),
            rule_id=name,
            commit=_IHP_OPEN_PDK_COMMIT,
        )
    # `rsilG2_rspec`/`rppdG2_rspec` in
    # `libs.tech/klayout/python/sg13g2_pycell_lib/sg13g2_tech.json`
    # (`techName == "SG13G2"` selects the `G2` suffix -- see `rppd_code.py`).
    assert by_name["rsil"].sheet_rho_ohm_sq == pytest.approx(7.0)
    assert by_name["rppd"].sheet_rho_ohm_sq == pytest.approx(260.0)
    # `rhighG2_rspec` (1360.0), corroborated by `cornerRES.lib`'s `res_typ`
    # corner `rsh_rhigh` -- see `sg13g2.py`'s resistor note for the full
    # tie-break (issue #1235).
    assert by_name["rhigh"].sheet_rho_ohm_sq == pytest.approx(1360.0)
    # `RSH_RES_METAL1`/`RSH_RES_METAL2` (res_extraction.lvs's own inline
    # constants, cited upstream to `libs.tech/parasitics/itf/
    # sg13g2_typ.itf`).
    assert by_name["res_metal1"].sheet_rho_ohm_sq == pytest.approx(0.110)
    assert by_name["res_metal2"].sheet_rho_ohm_sq == pytest.approx(0.088)


def test_sg13g2_rhigh_requires_both_implants_disambiguating_it_from_rppd():
    """`rhigh`'s `requires` includes both `pSD`/`nSD` together (upstream:
    `rhigh_res = polyres_mk.and(psd_drw).and(nsd_drw).and(salblock_drw)`),
    which is what naturally keeps it distinct from `rppd` (whose own
    `excludes` subtract `nSD`/`nSD_block`, `res_derivations.lvs`'s
    `rppd_res = ... .not(nsd_block).not(nsd_drw)`) -- a segment carrying
    nSD can only ever match `rhigh`, never be misclassified as `rppd`, and a
    segment *without* nSD can only ever match `rppd`, never `rhigh` (issue
    #1235's own edge-case test plan item)."""
    by_name = {r.name: r for r in EXTRACTION_DECK.resistors}
    rhigh = by_name["rhigh"]
    rppd = by_name["rppd"]
    assert (7, 0) in rhigh.requires  # nSD required
    assert (7, 0) in rppd.excludes  # nSD excluded
    assert (14, 0) in rhigh.requires  # pSD required
    assert (14, 0) in rppd.requires  # pSD required
    assert (28, 0) in rhigh.requires  # SalBlock required
    assert (28, 0) in rppd.requires  # SalBlock required


def test_sg13g2_thick_gate_ox_declares_a_mos_flavour():
    """`ThickGateOx` (44/0) is no longer a diagnostic-only voltage-domain
    marker: `EXTRACTION_DECK` declares one `mos_flavours` entry keyed on it
    (issue #1231), whose `nfet_provenance`/`pfet_provenance` cite
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


def test_sg13g2_derives_well_substrate_taps_from_implant_layers():
    """`EXTRACTION_DECK` declares no distinct drawn tap mask (`tap` stays
    `None`), but -- issue #1273, mirroring gf180mcu's own `tap_nplus`/
    `tap_pplus` fix (#1084) -- declares `tap_nplus`/`tap_pplus` so
    `extract.py` can derive an equivalent well-/substrate-tie region from
    the same `nSD`/`pSD` implant layers already used for MOS source/drain
    recognition. `well_label` stays `None` too: sg13g2 has no datatype-5
    -style pin/label text layer distinct from its per-metal `*_text` layers
    (see the module's own `EXTRACTION_DECK` docstring note)."""
    assert EXTRACTION_DECK.tap is None
    assert EXTRACTION_DECK.tap_nplus == (7, 0)  # nSD.drawing
    assert EXTRACTION_DECK.tap_pplus == (14, 0)  # pSD.drawing
    assert EXTRACTION_DECK.well_label is None


# --------------------------------------------------------------------------- #
# Golden layout -> netlist pairs (issue #905 AC: "golden layout->netlist
# pairs" for LVS device-extraction rules)
# --------------------------------------------------------------------------- #


def _make_nmos_layout(*, thick_gate_ox: bool = False) -> kdb.Layout:
    """One drawn NMOS on sg13g2's curated MOS-recognition layers: a 2x1um
    Activ strip crossed by a 0.4um-wide GatPoly gate bar (Activ outside
    NWell, so it recognises as NMOS) -- the exact device
    `EXTRACTION_DECK.nfet_provenance` cites (`sg13_lv_nmos`). `W` is the
    Activ strip's own 1um cross-extent; `L` is the GatPoly bar's 0.4um
    width.

    With `thick_gate_ox=True` the same geometry is additionally covered by
    `ThickGateOx` (44/0), making it sg13g2's thick-oxide ("-HV") flavour
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
    label(8, 25, "S", 0.2, 0.5)
    label(8, 25, "D", 1.8, 0.5)

    draw(6, 0, _box_um(0.9, 1.0, 1.1, 1.2))  # gate Cont
    draw(8, 0, _box_um(0.85, 0.95, 1.15, 1.25))  # gate Metal1 pad
    label(8, 25, "G", 1.0, 1.1)

    return layout


def test_golden_pair_sg13g2_nfet_l_w_matches_drawn_geometry(tmp_path: Path):
    """A drawn 0.4um-gate / 1um-active-width NMOS extracts with exactly
    those `l_um`/`w_um` -- validating `EXTRACTION_DECK.nfet_provenance`'s
    `sg13_lv_nmos` citation end-to-end: a golden layout, run through
    `run_extract`, reproduces the hand-computed netlist device parameters
    the drawn geometry implies."""
    path = _write_gds(_make_nmos_layout(), tmp_path / "nmos.gds")
    report = run_extract(path, "sg13g2", output=str(tmp_path / "nmos.spice"))

    assert report["device_counts"] == {"nfet": 1}
    (device,) = report["devices"]
    assert device["class"] == "nfet"
    assert device["params"]["l_um"] == pytest.approx(0.4)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert device["nets"]["s"] == "S"
    assert device["nets"]["d"] == "D"
    assert device["nets"]["g"] == "G"
    assert device["nets"]["b"] == "vsubs"  # no drawn PWell tap -> substrate_net
    assert EXTRACTION_DECK.nfet_provenance.rule_id == "sg13_lv_nmos"


def _make_pmos_layout(*, thick_gate_ox: bool = False) -> kdb.Layout:
    """The `_make_nmos_layout` geometry wrapped in `NWell.drawing` (Activ
    *inside* NWell recognises as PMOS, the deck's own NMOS/PMOS split -- see
    `ExtractionDeck.active`/`.poly`/`.nwell`'s own docstring) -- the exact
    device `EXTRACTION_DECK.pfet_provenance` cites (`sg13_lv_pmos`). Unlike
    sky130's `nwell.pin`, sg13g2 declares no `well_label` layer (see
    `sg13g2.py`'s own docstring), so the body net here resolves to a
    synthesized, unlabelled net rather than a named pin -- documented
    explicitly by the assertion below, not silently ignored."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(31, 0, _box_um(-1, -1, 3, 2))  # NWell.drawing, encloses Activ -> PMOS

    draw(1, 0, _box_um(0, 0, 2, 1))  # Activ.drawing, W=1um
    draw(5, 0, _box_um(0.8, -0.2, 1.2, 1.2))  # GatPoly.drawing gate, L=0.4um
    if thick_gate_ox:
        draw(44, 0, _box_um(-0.3, -0.3, 2.3, 1.3))  # ThickGateOx -> "-HV"

    draw(6, 0, _box_um(0.1, 0.3, 0.3, 0.7))  # Cont (source side)
    draw(6, 0, _box_um(1.7, 0.3, 1.9, 0.7))  # Cont (drain side)
    draw(8, 0, _box_um(0.0, 0.2, 0.4, 0.8))  # Metal1 (source pad)
    draw(8, 0, _box_um(1.6, 0.2, 2.0, 0.8))  # Metal1 (drain pad)
    label(8, 25, "S", 0.2, 0.5)
    label(8, 25, "D", 1.8, 0.5)

    draw(6, 0, _box_um(0.9, 1.0, 1.1, 1.2))  # gate Cont
    draw(8, 0, _box_um(0.85, 0.95, 1.15, 1.25))  # gate Metal1 pad
    label(8, 25, "G", 1.0, 1.1)

    return layout


def test_golden_pair_sg13g2_pfet_l_w_matches_drawn_geometry(tmp_path: Path):
    """A drawn 0.4um-gate / 1um-active-width PMOS (Activ wrapped in NWell)
    extracts with exactly those `l_um`/`w_um` -- validating
    `EXTRACTION_DECK.pfet_provenance`'s `sg13_lv_pmos` citation end-to-end,
    the PMOS sibling of `test_golden_pair_sg13g2_nfet_l_w_matches_drawn_
    geometry` above."""
    path = _write_gds(_make_pmos_layout(), tmp_path / "pmos.gds")
    report = run_extract(path, "sg13g2", output=str(tmp_path / "pmos.spice"))

    assert report["device_counts"] == {"pfet": 1}
    (device,) = report["devices"]
    assert device["class"] == "pfet"
    assert device["params"]["l_um"] == pytest.approx(0.4)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert device["nets"]["s"] == "S"
    assert device["nets"]["d"] == "D"
    assert device["nets"]["g"] == "G"
    assert EXTRACTION_DECK.pfet_provenance.rule_id == "sg13_lv_pmos"


# --------------------------------------------------------------------------- #
# Metal3-TopMetal2 connectivity stack extension (issue #1243)
# --------------------------------------------------------------------------- #


def _make_nmos_layout_routed_through_full_metal_stack() -> kdb.Layout:
    """The same NMOS `_make_nmos_layout` draws, except the drain terminal is
    routed straight up through *every* level of the extended
    `EXTRACTION_DECK.metals`/`.vias` stack (Metal1 -> Via1 -> Metal2 -> Via2
    -> Metal3 -> Via3 -> Metal4 -> Via4 -> Metal5 -> TopVia1 -> TopMetal1 ->
    TopVia2 -> TopMetal2) instead of being labelled directly at Metal1 -- the
    golden layout issue #1243's own acceptance criteria ask for ("routing a
    device out through the new top of the stack"). The drain net's own `"D"`
    label is drawn only at the very top (TopMetal2.text, 134/25); if any
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
    label(8, 25, "S", 0.2, 0.5)

    draw(6, 0, _box_um(0.9, 1.0, 1.1, 1.2))  # gate Cont
    draw(8, 0, _box_um(0.85, 0.95, 1.15, 1.25))  # gate Metal1 pad
    label(8, 25, "G", 1.0, 1.1)

    # Drain: Cont lands on Metal1, then every metal/via level of the
    # extended stack stacks straight up, each level's box wide/tall enough
    # to satisfy that level's own DRC width floor and the via enclosure
    # margin below/above it (see `sg13g2.py`'s `DECK`) -- not load-bearing
    # for this connectivity-only test, but keeping the fixture DRC-clean
    # too avoids a golden layout that would fail `klt drc` on the very
    # stack it is meant to demonstrate.
    draw(6, 0, _box_um(1.7, 0.3, 1.9, 0.7))  # Cont (drain side)
    conductor_layers = [
        (8, 0),  # Metal1.drawing
        (10, 0),  # Metal2.drawing
        (30, 0),  # Metal3.drawing
        (50, 0),  # Metal4.drawing
        (67, 0),  # Metal5.drawing
        (126, 0),  # TopMetal1.drawing
        (134, 0),  # TopMetal2.drawing
    ]
    via_layers = [
        (19, 0),  # Via1.drawing
        (29, 0),  # Via2.drawing
        (49, 0),  # Via3.drawing
        (66, 0),  # Via4.drawing
        (125, 0),  # TopVia1.drawing
        (133, 0),  # TopVia2.drawing
    ]
    for layer, datatype in conductor_layers:
        draw(layer, datatype, _box_um(1.5, 0.0, 4.5, 3.0))
    for layer, datatype in via_layers:
        draw(layer, datatype, _box_um(2.0, 0.5, 3.0, 1.5))
    label(134, 25, "D", 3.0, 1.5)  # TopMetal2.text -- the top of the stack

    return layout


def test_golden_pair_sg13g2_nfet_drain_routes_through_full_metal_stack(
    tmp_path: Path,
):
    """The device-recognition geometry is unchanged from
    `test_golden_pair_sg13g2_nfet_l_w_matches_drawn_geometry` above (same
    `l_um`/`w_um`), but the drain terminal is only labelled at TopMetal2, the
    top of the stack issue #1243 extends `EXTRACTION_DECK.metals`/`.vias` to
    reach. Before #1243 (`metals`/`vias` capped at Metal1/Via1/Metal2), a
    drain routed this far out would resolve to an *isolated*,
    unlabelled/synthesized net -- `deck.metals`/`.vias` had no entries above
    Metal2 to carry the connection, so the "D" label at TopMetal2 would never
    reach the transistor's own drain terminal at all."""
    path = _write_gds(
        _make_nmos_layout_routed_through_full_metal_stack(),
        tmp_path / "nmos_full_stack.gds",
    )
    report = run_extract(path, "sg13g2", output=str(tmp_path / "nmos_full_stack.spice"))

    assert report["device_counts"] == {"nfet": 1}
    (device,) = report["devices"]
    assert device["class"] == "nfet"
    assert device["params"]["l_um"] == pytest.approx(0.4)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert device["nets"]["s"] == "S"
    assert device["nets"]["d"] == "D"
    assert device["nets"]["g"] == "G"
    assert device["nets"]["b"] == "vsubs"


# --------------------------------------------------------------------------- #
# Thick-oxide ("-HV") MOS flavour (issue #1231)
# --------------------------------------------------------------------------- #


def _make_pdk_install(tmp_path: Path) -> str:
    """A minimal IHP-Open-PDK-shaped install tree `klt pdk find` resolves as
    variant `ihp-sg13g2` -- mirrors `test_extract.py`'s own
    `_make_pdk_install` (the resolver only probes for the variant directory's
    `libs.tech` marker; the model binding itself is a curated table, not a
    file read)."""
    root = tmp_path / "pdk_install"
    (root / "ihp-sg13g2" / "libs.tech").mkdir(parents=True)
    return str(root)


def _device_cards(report: dict) -> list[str]:
    text = Path(report["netlist_path"]).read_text()
    return [line for line in text.splitlines() if line and line[0] in ("M", "X")]


def test_golden_pair_sg13g2_thick_oxide_nmos_binds_sg13_hv_nmos(tmp_path: Path):
    """The acceptance criterion of issue #1231: a golden layout whose NMOS is
    drawn inside `ThickGateOx` (44/0) extracts bound to the real
    `sg13_hv_nmos` model, not the thin-oxide `sg13_lv_nmos` -- and the
    `ThickGateOx` voltage-domain warning that used to fire for exactly this
    geometry no longer does."""
    path = _write_gds(_make_nmos_layout(thick_gate_ox=True), tmp_path / "hv_nmos.gds")
    report = run_extract(
        path,
        "sg13g2",
        pdk_variant="ihp-sg13g2",
        pdk_root=_make_pdk_install(tmp_path),
        output=str(tmp_path / "hv_nmos.spice"),
    )

    # The structural class label is deliberately unchanged (see `MOSFlavour`'s
    # own docstring) -- only the bound model name distinguishes the flavours.
    assert report["device_counts"] == {"nfet": 1}
    (device,) = report["devices"]
    assert device["params"]["l_um"] == pytest.approx(0.4)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert report["voltage_domain_warnings"] == []

    cards = _device_cards(report)
    assert cards and all(line.startswith("X") for line in cards)
    assert any(" sg13_hv_nmos " in line for line in cards)
    assert not any(" sg13_lv_nmos " in line for line in cards)


def test_golden_pair_sg13g2_thick_oxide_pmos_binds_sg13_hv_pmos(tmp_path: Path):
    """The PMOS sibling of the NMOS golden pair above (`sg13_hv_pmos`)."""
    path = _write_gds(_make_pmos_layout(thick_gate_ox=True), tmp_path / "hv_pmos.gds")
    report = run_extract(
        path,
        "sg13g2",
        pdk_variant="ihp-sg13g2",
        pdk_root=_make_pdk_install(tmp_path),
        output=str(tmp_path / "hv_pmos.spice"),
    )

    assert report["device_counts"] == {"pfet": 1}
    assert report["voltage_domain_warnings"] == []
    cards = _device_cards(report)
    assert any(" sg13_hv_pmos " in line for line in cards)
    assert not any(" sg13_lv_pmos " in line for line in cards)


def test_sg13g2_thin_oxide_mos_still_binds_sg13_lv_nmos(tmp_path: Path):
    """Regression control: the *same* NMOS geometry with no `ThickGateOx`
    drawn over it still binds the deck's default thin-oxide model, so the
    flavour split above is attributable to the marker and nothing else."""
    path = _write_gds(_make_nmos_layout(), tmp_path / "lv_nmos.gds")
    report = run_extract(
        path,
        "sg13g2",
        pdk_variant="ihp-sg13g2",
        pdk_root=_make_pdk_install(tmp_path),
        output=str(tmp_path / "lv_nmos.spice"),
    )

    assert report["device_counts"] == {"nfet": 1}
    assert report["voltage_domain_warnings"] == []
    cards = _device_cards(report)
    assert any(" sg13_lv_nmos " in line for line in cards)
    assert not any(" sg13_hv_nmos " in line for line in cards)


def test_sg13g2_thick_oxide_mos_was_misclassified_before_mos_flavours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Negative control for the fix: with `mos_flavours` stripped back to its
    pre-#1231 empty state (and nothing else changed), the *same* thick-oxide
    golden layout binds `sg13_lv_nmos` -- the silent mis-classification issue
    #1231 reports -- and `ThickGateOx` fires the residual-gap
    `voltage_domain_warnings` diagnostic it fired before the fix."""
    monkeypatch.setattr(
        sg13g2_deck_module,
        "EXTRACTION_DECK",
        dataclasses.replace(EXTRACTION_DECK, mos_flavours=()),
    )

    path = _write_gds(
        _make_nmos_layout(thick_gate_ox=True), tmp_path / "hv_nmos_pre_fix.gds"
    )
    report = run_extract(
        path,
        "sg13g2",
        pdk_variant="ihp-sg13g2",
        pdk_root=_make_pdk_install(tmp_path),
        output=str(tmp_path / "hv_nmos_pre_fix.spice"),
    )

    cards = _device_cards(report)
    assert any(" sg13_lv_nmos " in line for line in cards)
    assert not any(" sg13_hv_nmos " in line for line in cards)
    assert [w["marker"] for w in report["voltage_domain_warnings"]] == ["44/0"]


# --------------------------------------------------------------------------- #
# Drawn poly resistors (issue #1231)
# --------------------------------------------------------------------------- #

_RES_SQUARES = 6.0  # 6um marked segment / 1um drawn width


def _make_poly_resistor_layout(
    extra_layers: tuple[tuple[int, int], ...],
) -> kdb.Layout:
    """A 12x1um `GatPoly` bar with a 6um-long `polyres`-marked segment
    (`L=6um`/`W=1um`, 6.0 squares), plus `extra_layers` drawn over that same
    segment to narrow it to one specific sg13g2 poly-resistor flavour
    (`rsil`: `EXTBlock`+`Res`; `rppd`: `EXTBlock`+`pSD`+`SalBlock`) --
    mirrors `test_lvs_device_provenance.py`'s own
    `_make_gf180mcu_resistor_layout`."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(5, 0, _box_um(0, 0, 12, 1))  # GatPoly bar, W=1um
    draw(128, 0, _box_um(3, 0, 9, 1))  # polyres marker, 6um segment -> L=6um
    for layer, datatype in extra_layers:
        draw(layer, datatype, _box_um(2.8, -0.2, 9.2, 1.2))

    draw(6, 0, _box_um(0.1, 0.3, 0.3, 0.7))  # Cont (head A)
    draw(6, 0, _box_um(11.7, 0.3, 11.9, 0.7))  # Cont (head B)
    draw(8, 0, _box_um(0.0, 0.2, 0.4, 0.8))  # Metal1 (head A pad)
    draw(8, 0, _box_um(11.6, 0.2, 12.0, 0.8))  # Metal1 (head B pad)
    label(8, 25, "RA", 0.2, 0.5)
    label(8, 25, "RB", 11.8, 0.5)

    return layout


@pytest.mark.parametrize(
    ("name", "extra_layers"),
    [
        # rsil: polyres & EXTBlock & Res, with pSD/nSD/SalBlock all absent
        # (`rsil_res = polyres_mk.and(res_drw).not(rsil_exc)`).
        ("rsil", ((111, 0), (24, 0))),
        # rppd: polyres & EXTBlock & pSD & SalBlock, nSD absent
        # (`rppd_res = polyres_mk.and(psd_drw).and(salblock_drw)...`).
        ("rppd", ((111, 0), (14, 0), (28, 0))),
        # rhigh: polyres & EXTBlock & pSD & nSD & SalBlock -- both implants
        # present together (`rhigh_res = polyres_mk.and(psd_drw)
        # .and(nsd_drw).and(salblock_drw)`, issue #1235).
        ("rhigh", ((111, 0), (14, 0), (7, 0), (28, 0))),
    ],
)
def test_golden_pair_sg13g2_poly_resistor_r_ohm_matches_provenance_coefficient(
    tmp_path: Path, name: str, extra_layers: tuple[tuple[int, int], ...]
):
    """A drawn 6-square `GatPoly` bar marked `polyres` and narrowed to one
    flavour extracts as that device class with `R = squares *
    sheet_rho_ohm_sq`, computed from the deck's own provenance-cited
    coefficient -- and its two heads resolve to the drawn, labelled Metal1
    pads (the resistor is not left shorted through the poly bar)."""
    resistor = next(r for r in EXTRACTION_DECK.resistors if r.name == name)

    path = _write_gds(
        _make_poly_resistor_layout(extra_layers), tmp_path / f"{name}.gds"
    )
    report = run_extract(path, "sg13g2", output=str(tmp_path / f"{name}.spice"))

    assert report["device_counts"] == {name: 1}
    (device,) = report["devices"]
    assert device["class"] == name
    assert device["params"]["l_um"] == pytest.approx(6.0)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert device["params"]["r_ohm"] == pytest.approx(
        _RES_SQUARES * resistor.sheet_rho_ohm_sq
    )
    assert {device["nets"]["a"], device["nets"]["b"]} == {"RA", "RB"}


def test_sg13g2_unmarked_poly_bar_is_not_a_resistor(tmp_path: Path):
    """A `GatPoly` bar carrying the `polyres` marker but *none* of the
    flavour-selecting layers stays ordinary interconnect: a segment this deck
    cannot positively identify keeps today's short rather than extracting
    with a guessed sheet resistance (`ResistorDevice.excludes`' own
    "known-unmodelled beats silently wrong" discipline)."""
    path = _write_gds(_make_poly_resistor_layout(()), tmp_path / "bare.gds")
    report = run_extract(path, "sg13g2", output=str(tmp_path / "bare.spice"))

    assert report["device_counts"] == {}


# --------------------------------------------------------------------------- #
# Drawn metal resistors (issue #1235)
# --------------------------------------------------------------------------- #

_METAL_RES_SQUARES = 6.0  # 6um marked segment / 1um drawn width


def _make_metal_resistor_layout(
    metal_layer: int, marker_layer: int, label_layer: int
) -> kdb.Layout:
    """A 12x1um metal bar on `(metal_layer, 0)` with a 6um-long
    `(marker_layer, 29)`-marked segment (`L=6um`/`W=1um`, 6.0 squares) and
    two labelled heads on `(label_layer, 25)` -- the metal-resistor analogue
    of `_make_poly_resistor_layout` above (`res_metal1`/`res_metal2`: issue
    #1235)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(metal_layer, 0, _box_um(0, 0, 12, 1))  # metal bar, W=1um
    draw(marker_layer, 29, _box_um(3, 0, 9, 1))  # res marker, 6um -> L=6um
    label(label_layer, 25, "RA", 0.2, 0.5)
    label(label_layer, 25, "RB", 11.8, 0.5)

    return layout


@pytest.mark.parametrize(
    ("name", "metal_layer"),
    [
        ("res_metal1", 8),  # Metal1.drawing / Metal1.res (8/29)
        ("res_metal2", 10),  # Metal2.drawing / Metal2.res (10/29)
    ],
)
def test_golden_pair_sg13g2_metal_resistor_r_ohm_matches_provenance_coefficient(
    tmp_path: Path, name: str, metal_layer: int
):
    """A drawn 6-square metal bar marked with its own metal-resistor layer
    (`metal1_res`/`metal2_res`) extracts as that device class with `R =
    squares * sheet_rho_ohm_sq`, computed from the deck's own
    provenance-cited coefficient -- and its two heads resolve to the drawn,
    labelled metal pads (the resistor is not left shorted through the metal
    bar)."""
    resistor = next(r for r in EXTRACTION_DECK.resistors if r.name == name)

    path = _write_gds(
        _make_metal_resistor_layout(metal_layer, metal_layer, metal_layer),
        tmp_path / f"{name}.gds",
    )
    report = run_extract(path, "sg13g2", output=str(tmp_path / f"{name}.spice"))

    assert report["device_counts"] == {name: 1}
    (device,) = report["devices"]
    assert device["class"] == name
    assert device["params"]["l_um"] == pytest.approx(6.0)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert device["params"]["r_ohm"] == pytest.approx(
        _METAL_RES_SQUARES * resistor.sheet_rho_ohm_sq
    )
    assert {device["nets"]["a"], device["nets"]["b"]} == {"RA", "RB"}


def test_sg13g2_unmarked_metal1_bar_is_not_a_resistor(tmp_path: Path):
    """A Metal1 bar with no `metal1_res` marker stays ordinary interconnect
    -- same "known-unmodelled beats silently wrong" discipline as the poly
    resistors above."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(8, 0)).insert(_box_um(0, 0, 12, 1))
    li = layout.layer(8, 25)
    top.shapes(li).insert(
        kdb.Text("RA", kdb.Trans(round(0.2 / _DBU_UM), round(0.5 / _DBU_UM)))
    )
    top.shapes(li).insert(
        kdb.Text("RB", kdb.Trans(round(11.8 / _DBU_UM), round(0.5 / _DBU_UM)))
    )

    path = _write_gds(layout, tmp_path / "bare_metal1.gds")
    report = run_extract(path, "sg13g2", output=str(tmp_path / "bare_metal1.spice"))

    assert report["device_counts"] == {}


# --------------------------------------------------------------------------- #
# --pdk resistor model binding (issue #1457): rsil/rppd/rhigh bind to their
# real 3-terminal subcircuits; res_metal1/res_metal2 stay a documented
# bare-`R`-card carve-out (verified against a real fetched IHP-Open-PDK
# install -- see `pdk_models.py`'s module docstring). Mirrors
# `test_extract.py`'s `test_pdk_resolved_writes_x_card_model_binding_sky130`/
# `..._gf180mcu`, reusing this file's own poly/metal-resistor fixtures above.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "extra_layers"),
    [
        ("rsil", ((111, 0), (24, 0))),
        ("rppd", ((111, 0), (14, 0), (28, 0))),
        ("rhigh", ((111, 0), (14, 0), (7, 0), (28, 0))),
    ],
)
def test_pdk_resolved_binds_poly_resistor_sg13g2(
    tmp_path: Path, name: str, extra_layers: tuple[tuple[int, int], ...]
):
    """--pdk binds each of sg13g2's drawn poly resistors (`rsil`/`rppd`/
    `rhigh`) to its real 3-terminal subcircuit of the same name, whose
    geometry parameters are spelled `l`/`w` (issue #1457) -- not the bare,
    value-only `R`-card form (which cannot even represent the bulk-tie third
    terminal in valid SPICE)."""
    path = _write_gds(
        _make_poly_resistor_layout(extra_layers), tmp_path / f"{name}.gds"
    )
    out = str(tmp_path / f"{name}.spice")
    report = run_extract(
        path,
        "sg13g2",
        pdk_variant="ihp-sg13g2",
        pdk_root=_make_pdk_install(tmp_path),
        output=out,
    )

    assert report["device_counts"] == {name: 1}
    cards = _device_cards(report)
    assert cards, "expected a device card"
    assert all(card.startswith("X") for card in cards)
    (card,) = cards
    assert f" {name} " in card
    substrate = EXTRACTION_DECK.substrate_net
    # Three terminals: the two contacted heads plus the substrate-tied bulk.
    assert card.split()[1:4] == ["RA", "RB", substrate]
    # sg13g2 has no ambient `.option scale` (unlike sky130), so the geometry
    # literal keeps the explicit micrometre-unit suffix, same as gf180mcu.
    assert "l=6U" in card and card.endswith("w=1U")


def test_pdk_resolved_leaves_res_metal1_as_bare_r_card(tmp_path: Path):
    """Regression/carve-out guard: `res_metal1` has no curated resistor-model
    table entry (issue #1457's verified finding -- the real fetched install
    defines no `.subckt`/`.model` for it at all, unlike `rsil`/`rppd`/
    `rhigh`), so it stays the bare `R`-card form under `--pdk`, exactly like
    without `--pdk` -- never a guessed subcircuit call."""
    path = _write_gds(_make_metal_resistor_layout(8, 8, 8), tmp_path / "res_metal1.gds")
    out = str(tmp_path / "res_metal1.spice")
    report = run_extract(
        path,
        "sg13g2",
        pdk_variant="ihp-sg13g2",
        pdk_root=_make_pdk_install(tmp_path),
        output=out,
    )

    assert report["device_counts"] == {"res_metal1": 1}
    text = Path(out).read_text()
    device_lines = [
        line for line in text.splitlines() if line and line[0] in ("M", "X", "R")
    ]
    (card,) = device_lines
    assert card.startswith("R")
    assert card.endswith("res_metal1")


# --------------------------------------------------------------------------- #
# MIM capacitors (issue #1454, unblocked by #1243's metals/vias extension)
# --------------------------------------------------------------------------- #

# `cmim_core`'s own model card (`libs.tech/ngspice/models/capacitors_mod.lib`,
# `CJ=cap_carea`/`CJSW=40E-18`) at the typical corner
# (`cornerCAP.lib`'s `.LIB cap_typ`, `cap_carea = 1.5E-15`).
_MIM_AREA_CAP_F_UM2 = 1.5e-15
_MIM_PERIM_CAP_F_UM = 4.0e-17


def _make_sg13g2_mim_layout(*, pwell_block: bool) -> kdb.Layout:
    """A 10x5um `MIM` (36/0) top plate over a `Metal5` (67/0) bottom plate,
    with the `Vmim` (129/0) via stack up to `TopMetal1` (126/0) the PDK's own
    `cmim`/`rfcmim` PyCells draw -- mirroring
    `test_lvs_device_provenance.py`'s own sky130 `capm`-over-`met3` golden
    pair.

    `pwell_block` draws the `PWell.block` (46/21) ring `rfcmim`'s own
    `rfmim_area = pwell_block.interacting(mim_drw)` derivation requires (and
    `cap_cmim`'s `mimcap_exclude` subtracts) -- the one drawn layer that
    tells the two flavours apart."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    if pwell_block:
        # `rfmim_area` -- covers both plates, exactly as `rfcmim_code.py`'s
        # own `Box(-3, -3, lu+3, wu+3)` PWell.block rectangle does.
        draw(46, 21, _box_um(-2, -2, 12, 7))
    draw(67, 0, _box_um(-1, -1, 11, 6))  # Metal5 bottom plate (overhangs MIM)
    draw(36, 0, _box_um(0, 0, 10, 5))  # MIM top plate, 10x5um
    draw(129, 0, _box_um(2, 2, 3, 3))  # Vmim (MIM -> TopMetal1)
    draw(126, 0, _box_um(1.5, 1.5, 3.5, 3.5))  # TopMetal1 landing pad
    label(67, 25, "BOT", 10.5, 5.5)
    label(126, 25, "TOP", 2.5, 2.5)

    return layout


@pytest.mark.parametrize(
    ("name", "pwell_block"),
    [
        # `cmim_top = mim_top.not(mimcap_exclude)` -- `mimcap_exclude`'s head
        # term is `pwell_block`, so the plain MiM cap is the *absence* of it.
        ("cap_cmim", False),
        # `rfmim_top = mim_top.and(rfmim_area)`, `rfmim_area =
        # pwell_block.interacting(mim_drw)` -- the RF flavour requires it.
        ("rfcmim", True),
    ],
)
def test_golden_pair_sg13g2_mim_capacitor_c_f_matches_provenance_coefficients(
    tmp_path: Path, name: str, pwell_block: bool
):
    """A drawn 10x5um `MIM` top plate over a `Metal5` bottom plate extracts
    as `cap_cmim`/`rfcmim` with `C = area * area_cap_f_um2 + perimeter *
    perim_cap_f_um`, computed from the deck's own provenance-cited
    coefficients -- the sg13g2 sibling of
    `test_golden_pair_sky130_capacitor_c_f_matches_provenance_coefficients`
    (issue #1454)."""
    capacitor = next(c for c in EXTRACTION_DECK.capacitors if c.name == name)
    assert capacitor.provenance.rule_id == name
    assert capacitor.area_cap_f_um2 == _MIM_AREA_CAP_F_UM2
    assert capacitor.perim_cap_f_um == _MIM_PERIM_CAP_F_UM

    path = _write_gds(
        _make_sg13g2_mim_layout(pwell_block=pwell_block), tmp_path / f"{name}.gds"
    )
    report = run_extract(path, "sg13g2", output=str(tmp_path / f"{name}.spice"))

    assert report["device_counts"] == {name: 1}
    (device,) = report["devices"]
    assert device["class"] == name
    area_um2 = 50.0
    perimeter_um = 30.0
    assert device["params"]["area_um2"] == pytest.approx(area_um2)
    assert device["params"]["perimeter_um"] == pytest.approx(perimeter_um)
    assert device["params"]["c_f"] == pytest.approx(
        area_um2 * capacitor.area_cap_f_um2 + perimeter_um * capacitor.perim_cap_f_um
    )
    # #1243's whole point: both plates land on tracked `metals[]` levels, so
    # the recognised device is wired into the rest of the extracted graph
    # (Metal5 bottom plate directly; MIM top plate through `Vmim` ->
    # TopMetal1) rather than floating on two isolated nodes.
    assert {device["nets"]["a"], device["nets"]["b"]} == {"TOP", "BOT"}


def test_sg13g2_mim_capacitor_c_f_reproduces_pdk_reference_instance(tmp_path: Path):
    """The PDK's own worked reference instance, reproduced end-to-end: a 7x7um
    `cap_cmim` is documented as `C=74.620f` in `custom_reader.lvs`'s own
    example netlist card (`C1 PLUS MINUS cap_cmim w=6.99u l=6.99u m=1
    C=74.620f`). `49 um^2 * 1.5e-15 + 28 um * 40e-18 = 74.62 fF` -- an
    independent cross-check that this deck's transcribed coefficients are the
    ones the PDK's own MiM extractor uses, not merely self-consistent."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    draw(67, 0, _box_um(-1, -1, 8, 8))  # Metal5 bottom plate
    draw(36, 0, _box_um(0, 0, 7, 7))  # MIM top plate, 7x7um
    draw(129, 0, _box_um(2, 2, 3, 3))  # Vmim
    draw(126, 0, _box_um(1.5, 1.5, 3.5, 3.5))  # TopMetal1

    path = _write_gds(layout, tmp_path / "cmim_7x7.gds")
    report = run_extract(path, "sg13g2", output=str(tmp_path / "cmim_7x7.spice"))

    assert report["device_counts"] == {"cap_cmim": 1}
    (device,) = report["devices"]
    assert device["params"]["c_f"] == pytest.approx(74.62e-15)


def test_sg13g2_mim_capacitor_flavours_are_mutually_exclusive(tmp_path: Path):
    """`cap_cmim` and `rfcmim` share byte-identical plate geometry and are
    told apart solely by `PWell.block`: the same drawn MiM stack must extract
    as exactly one of them, never both (upstream's own
    `cmim_top = mim_top.not(pwell_block...)` vs.
    `rfmim_top = mim_top.and(rfmim_area)` split)."""
    for pwell_block, expected in ((False, "cap_cmim"), (True, "rfcmim")):
        path = _write_gds(
            _make_sg13g2_mim_layout(pwell_block=pwell_block),
            tmp_path / f"mim_{expected}.gds",
        )
        report = run_extract(
            path, "sg13g2", output=str(tmp_path / f"mim_{expected}.spice")
        )
        assert report["device_counts"] == {expected: 1}


def test_sg13g2_metal5_without_mim_marker_is_not_a_capacitor(tmp_path: Path):
    """Plain Metal5-under-TopMetal1 routing with no drawn `MIM` (36/0) plate
    stays ordinary interconnect -- the capacitor entries recognise the
    purpose-drawn MiM dielectric mark, not any two stacked metals."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(67, 0)).insert(_box_um(-1, -1, 11, 6))
    top.shapes(layout.layer(126, 0)).insert(_box_um(0, 0, 10, 5))

    path = _write_gds(layout, tmp_path / "no_mim.gds")
    report = run_extract(path, "sg13g2", output=str(tmp_path / "no_mim.spice"))

    assert report["device_counts"] == {}


# --------------------------------------------------------------------------- #
# MoM capacitors (issue #1466, found while investigating #1463)
# --------------------------------------------------------------------------- #

# `cap_cmomi`/`cap_cmomf` postdate this module's own `_IHP_OPEN_PDK_COMMIT`
# (the `v0.3.0` tag) -- see `sg13g2.py`'s `_IHP_OPEN_PDK_MOM_COMMIT` comment
# for why these entries cite a different, newer commit.
_IHP_OPEN_PDK_MOM_COMMIT = "d2cc0355f26235c777dfcc6867b390fa1e78083f"


def test_sg13g2_mom_capacitor_provenance_cites_cap_extraction_lvs():
    """Both curated MoM-capacitor entries carry a `provenance` citing the
    real, live-fetched `cap_extraction.lvs` `extract_devices(CapMomExtractor
    .new(...))` calls they were transcribed from (issue #1466) -- pinned to
    `_IHP_OPEN_PDK_MOM_COMMIT`, not this module's default
    `_IHP_OPEN_PDK_COMMIT` (that older `v0.3.0` tag has no MoM
    `extract_devices` call at all -- see `sg13g2.py`'s own "MoM capacitors"
    docstring section)."""
    by_name = {c.name: c for c in EXTRACTION_DECK.mom_capacitors}
    assert set(by_name) == {"cap_cmomi", "cap_cmomf"}
    for name in ("cap_cmomi", "cap_cmomf"):
        assert by_name[name].provenance == RuleProvenance(
            source_repo="IHP-GmbH/IHP-Open-PDK",
            source_path=(
                "ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks/cap_extraction.lvs"
            ),
            rule_id=name,
            commit=_IHP_OPEN_PDK_MOM_COMMIT,
        )


def _make_sg13g2_mom_layout(*, marker: tuple[int, int]) -> kdb.Layout:
    """A 10x4um MoM-capacitor marker with two `Metal1.pin` (8/2) port shapes
    side by side (the `double`/`none` PCell port-placement option -- both
    ports on the *same* metal level) -- mirrors the PDK's own
    `cap_cmomi_derivations.lvs`/`cap_cmomf_derivations.lvs` port derivation
    (`metal1_pin.and(marker)`). Each port is contacted out to its own
    `Metal1` routing stub and labelled, so a correct extraction reports two
    distinct, real (not anonymous single-shape) nets. `marker` is the
    device's own recognition layer (`Recog.mom` 99/39 for `cap_cmomi`,
    `Recog.momf` 99/40 for `cap_cmomf`) -- the only layer that tells the two
    devices apart, since everything else about this layout is identical."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: tuple[int, int], box: kdb.Box) -> None:
        top.shapes(layout.layer(*layer)).insert(box)

    def label(layer: tuple[int, int], text: str, x: float, y: float) -> None:
        li = layout.layer(*layer)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(marker, _box_um(0, 0, 10, 4))  # device marker, 10um x 4um
    draw((8, 2), _box_um(0, 0, 1, 1))  # Metal1.pin, PLUS
    draw((8, 2), _box_um(9, 3, 10, 4))  # Metal1.pin, MINUS
    draw((8, 0), _box_um(-2, 0, 0, 1))  # Metal1 routing stub off PLUS
    draw((8, 0), _box_um(10, 3, 12, 4))  # Metal1 routing stub off MINUS
    label((8, 25), "PLUS_NET", -1, 0.5)
    label((8, 25), "MINUS_NET", 11, 3.5)

    return layout


@pytest.mark.parametrize(
    "name, marker", [("cap_cmomi", (99, 39)), ("cap_cmomf", (99, 40))]
)
def test_golden_pair_sg13g2_mom_capacitor_w_l_matches_marker_geometry(
    tmp_path: Path, name: str, marker: tuple[int, int]
):
    """A drawn 10x4um MoM-cap marker with two side-by-side `Metal1.pin` ports
    extracts as `cap_cmomi`/`cap_cmomf` with `w_um`/`l_um` read straight off
    the marker's own bounding box (`l` -> X extent, `w` -> Y extent -- the
    real device's own axis mapping, transcribed from
    `custom_mom_extractor.lvs`), both port nets on distinct, correctly-named
    terminals, and **no** `c_f`/`area_um2`/`perimeter_um` key at all: unlike
    `cap_cmim`/`rfcmim`, this device's real compact model computes its own
    capacitance from `density[N]*active_area + Cfeed`, not from anything
    `klt extract` measures -- see `docs/json-contract.md`'s "MoM capacitor
    devices" note for the JSON-shape decision this documents."""
    mom_capacitor = next(c for c in EXTRACTION_DECK.mom_capacitors if c.name == name)
    assert mom_capacitor.provenance.rule_id == name

    path = _write_gds(_make_sg13g2_mom_layout(marker=marker), tmp_path / f"{name}.gds")
    report = run_extract(path, "sg13g2", output=str(tmp_path / f"{name}.spice"))

    assert report["device_counts"] == {name: 1}
    (device,) = report["devices"]
    assert device["class"] == name
    assert device["params"] == {"w_um": pytest.approx(4.0), "l_um": pytest.approx(10.0)}
    assert {device["nets"]["a"], device["nets"]["b"]} == {"PLUS_NET", "MINUS_NET"}


def test_sg13g2_mom_capacitor_stacked_ports_stay_on_separate_metal_nets(
    tmp_path: Path,
):
    """The `same`-feed PCell configuration stacks the two ports on *adjacent*
    metal levels at identical (x, y) rather than side by side -- the
    structural case a two-different-layer `CapacitorDevice` split cannot
    express at all (issue #1466's whole rationale for a new device shape).
    Both ports must still resolve to their own metal's distinct net, never
    bridged through the shared marker footprint."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: tuple[int, int], box: kdb.Box) -> None:
        top.shapes(layout.layer(*layer)).insert(box)

    def label(layer: tuple[int, int], text: str, x: float, y: float) -> None:
        li = layout.layer(*layer)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw((99, 39), _box_um(0, 0, 6, 6))  # Recog.mom marker
    draw((50, 2), _box_um(1, 1, 2, 2))  # Metal4.pin
    draw((67, 2), _box_um(1, 1, 2, 2))  # Metal5.pin, same (x, y) -- stacked
    draw((50, 0), _box_um(-3, 1, 1, 2))  # Metal4 routing stub
    draw((67, 0), _box_um(2, 1, 5, 2))  # Metal5 routing stub
    label((50, 25), "M4_NET", -2, 1.5)
    label((67, 25), "M5_NET", 4, 1.5)

    path = _write_gds(layout, tmp_path / "cap_cmomi_stacked.gds")
    report = run_extract(
        path, "sg13g2", output=str(tmp_path / "cap_cmomi_stacked.spice")
    )

    assert report["device_counts"] == {"cap_cmomi": 1}
    (device,) = report["devices"]
    assert {device["nets"]["a"], device["nets"]["b"]} == {"M4_NET", "M5_NET"}
    assert device["params"] == {"w_um": pytest.approx(6.0), "l_um": pytest.approx(6.0)}


def test_sg13g2_mom_capacitor_malformed_marker_is_dropped_with_warning(
    tmp_path: Path,
):
    """A marker with anything other than exactly two port polygons under it
    (here: three) is not extracted as a device at all -- mirroring upstream's
    own `CapMomExtractor` guard ("expected exactly 2 port regions ... found
    N") rather than guessing which two of N ports belong together. The drop
    is reported as a warning naming the device, not silently swallowed."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(99, 39)).insert(_box_um(0, 0, 6, 6))
    top.shapes(layout.layer(8, 2)).insert(_box_um(0, 0, 0.5, 0.5))
    top.shapes(layout.layer(8, 2)).insert(_box_um(1, 1, 1.5, 1.5))
    top.shapes(layout.layer(8, 2)).insert(_box_um(5, 5, 5.5, 5.5))

    path = _write_gds(layout, tmp_path / "cap_cmomi_malformed.gds")
    report = run_extract(
        path, "sg13g2", output=str(tmp_path / "cap_cmomi_malformed.spice")
    )

    assert report["device_counts"] == {}
    assert any(
        "cap_cmomi" in warning and "expected exactly 2 port" in warning
        for warning in report["warnings"]
    )


def test_sg13g2_mom_capacitor_flavours_are_told_apart_solely_by_marker_layer():
    """`cap_cmomi`/`cap_cmomf` share byte-identical recognition geometry
    (marker + per-metal pin ports) and are told apart *only* by which marker
    layer is drawn -- mirroring `test_sg13g2_mim_capacitor_flavours_are_
    mutually_exclusive`'s own `cap_cmim`/`rfcmim` discipline, for a
    "distinguished by drawn mask alone" family rather than a
    `PWell.block`-gated one."""
    by_name = {c.name: c for c in EXTRACTION_DECK.mom_capacitors}
    assert by_name["cap_cmomi"].marker == (99, 39)
    assert by_name["cap_cmomf"].marker == (99, 40)
    assert by_name["cap_cmomi"].marker != by_name["cap_cmomf"].marker
    assert by_name["cap_cmomi"].metal_pins == by_name["cap_cmomf"].metal_pins


def test_sg13g2_mom_capacitor_metal_pins_reach_only_the_thin_metal_stack():
    """Both entries' `metal_pins` cover exactly `Metal1.pin`..`Metal5.pin`
    (8/2, 10/2, 30/2, 50/2, 67/2) -- `None` for `TopMetal1`/`TopMetal2` --
    matching upstream's own `cap_extraction.lvs` call, which only ever wires
    up `m1p`..`m5p`: this device family lives entirely on the g2 thin-metal
    stack, never reaching the two top-metal levels this deck's own `metals`
    stack (issue #1243) otherwise tracks."""
    for capacitor in EXTRACTION_DECK.mom_capacitors:
        assert len(capacitor.metal_pins) == len(EXTRACTION_DECK.metals)
        assert capacitor.metal_pins == (
            (8, 2),
            (10, 2),
            (30, 2),
            (50, 2),
            (67, 2),
            None,
            None,
        )


# --------------------------------------------------------------------------- #
# Antenna diodes (issue #1234)
# --------------------------------------------------------------------------- #


def test_sg13g2_diode_provenance_cites_diode_extraction_lvs():
    """Both curated antenna-diode entries carry a `provenance` citing the
    real `diode_extraction.lvs` `extract_devices(diode(...))` calls they were
    transcribed from (issue #1234)."""
    by_name = {d.name: d for d in EXTRACTION_DECK.diodes}
    assert set(by_name) == {"dantenna", "dpantenna"}
    for name in ("dantenna", "dpantenna"):
        assert by_name[name].provenance == RuleProvenance(
            source_repo="IHP-GmbH/IHP-Open-PDK",
            source_path=(
                "ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks/diode_extraction.lvs"
            ),
            rule_id=name,
            commit=_IHP_OPEN_PDK_COMMIT,
        )


def _make_sg13g2_diode_layout() -> kdb.Layout:
    """A minimal dual-diode layout on sg13g2's curated diode-recognition
    layers -- one `dantenna` (n+ diffusion/p-substrate) and one `dpantenna`
    (p+ diffusion/NWell) junction, each 1um x 1um (area 1.0um^2, perimeter
    4.0um) -- mirrors `test_lvs_device_provenance.py`'s own
    `_make_gf180mcu_diode_layout` topology."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    # dantenna: n+ diffusion in the p-substrate (no drawn PWell mask).
    draw(99, 31, _box_um(-0.2, -0.2, 1.2, 1.2))  # Recog.diode marker
    draw(1, 0, _box_um(0, 0, 1, 1))  # Activ (cathode)
    draw(6, 0, _box_um(0.3, 0.3, 0.7, 0.7))  # Cont over the cathode
    draw(8, 0, _box_um(0.2, 0.2, 0.8, 0.8))  # Metal1 over the cathode
    label(8, 25, "CATH", 0.5, 0.5)

    # dpantenna: p+ diffusion in NWell.
    draw(31, 0, _box_um(4, 0, 7, 3))  # NWell (cathode)
    draw(99, 31, _box_um(4.8, 0.8, 6.2, 2.2))  # Recog.diode marker
    draw(1, 0, _box_um(5, 1, 6, 2))  # Activ (anode)
    draw(14, 0, _box_um(5, 1, 6, 2))  # pSD (p+ doped)
    draw(6, 0, _box_um(5.3, 1.3, 5.7, 1.7))  # Cont over the anode
    draw(8, 0, _box_um(5.2, 1.2, 5.8, 1.8))  # Metal1 over the anode
    label(8, 25, "ANOD", 5.5, 1.5)

    return layout


def test_golden_pair_sg13g2_diodes_extract_with_provenance_cited_devices(
    tmp_path: Path,
):
    """The synthetic dual-diode layout extracts exactly the two `D` devices
    its topology describes, each matching its own provenance-cited entry --
    validating `EXTRACTION_DECK.diodes[].provenance`'s `dantenna`/
    `dpantenna` citations end-to-end (issue #1234)."""
    path = _write_gds(_make_sg13g2_diode_layout(), tmp_path / "diode.gds")
    report = run_extract(path, "sg13g2", output=str(tmp_path / "diode.spice"))

    assert report["device_counts"] == {"dantenna": 1, "dpantenna": 1}
    by_class = {device["class"]: device for device in report["devices"]}

    dantenna = by_class["dantenna"]
    assert dantenna["nets"] == {"a": EXTRACTION_DECK.substrate_net, "c": "CATH"}
    assert dantenna["params"] == {"area_um2": 1.0, "perimeter_um": 4.0}

    dpantenna = by_class["dpantenna"]
    assert dpantenna["nets"]["a"] == "ANOD"
    assert dpantenna["params"] == {"area_um2": 1.0, "perimeter_um": 4.0}


def test_sg13g2_schottky_nbl1_declined_after_investigation():
    """Issue #1234's own finding, pinned as a regression test: SG13G2's real
    `schottky_nbl1` is extracted upstream via the *same* stock
    `DeviceExtractorBJT3Transistor` extractor `BipolarDevice` wires up
    (`bjt3('schottky_nbl1', Esd3Term)`, `Esd3Term < RBA::
    DeviceClassBJT3Transistor`) -- but its emitter terminal is a fixed-size
    box synthesized from a bounding-box-size-filtered region
    (`with_bbox_min`/`with_bbox_max` then `middle(as_boxes).sized(...)`) and
    its collector terminal a dynamic per-instance `.covering(...)`
    derivation, neither expressible by `BipolarDevice`'s plain
    layer-intersection `base`/`emitter`/`marker`/`collector` fields (see
    `sg13g2.py`'s "Schottky diode (schottky_nbl1) -- investigated, declined"
    docstring section for the full finding).

    This test exists so that if a future change ever populates a
    `schottky_nbl1` entry (in either `bipolars` or `diodes`) without
    revisiting this finding, it fails loudly here rather than silently
    landing a mapping nobody re-verified against the real PDK derivation."""
    assert "schottky_nbl1" not in EXTRACTION_DECK.device_classes
    assert {d.name for d in EXTRACTION_DECK.diodes} == {"dantenna", "dpantenna"}
    assert EXTRACTION_DECK.bipolars == ()


# --------------------------------------------------------------------------- #
# Coverage discipline
# --------------------------------------------------------------------------- #


def _provenanced_device_rule_ids() -> set[str]:
    """Mirrors `test_lvs_device_provenance.py`'s own
    `_provenanced_device_rule_ids`, scoped to sg13g2's `EXTRACTION_DECK`
    (MOS -- thin- and thick-oxide -- plus the three curated poly resistors,
    the two curated metal resistors, the two curated antenna diodes and the
    two curated MIM capacitors; see
    `test_sg13g2_extraction_deck_curated_device_families` above for what is
    still unrecognised)."""
    ids: set[str] = set()
    if EXTRACTION_DECK.nfet_provenance is not None:
        ids.add(EXTRACTION_DECK.nfet_provenance.rule_id)
    if EXTRACTION_DECK.pfet_provenance is not None:
        ids.add(EXTRACTION_DECK.pfet_provenance.rule_id)
    for flavour in EXTRACTION_DECK.mos_flavours:
        for provenance in (flavour.nfet_provenance, flavour.pfet_provenance):
            if provenance is not None:
                ids.add(provenance.rule_id)
    for resistor in EXTRACTION_DECK.resistors:
        if resistor.provenance is not None:
            ids.add(resistor.provenance.rule_id)
    for capacitor in EXTRACTION_DECK.capacitors:
        if capacitor.provenance is not None:
            ids.add(capacitor.provenance.rule_id)
    for mom_capacitor in EXTRACTION_DECK.mom_capacitors:
        if mom_capacitor.provenance is not None:
            ids.add(mom_capacitor.provenance.rule_id)
    for bipolar in EXTRACTION_DECK.bipolars:
        if bipolar.provenance is not None:
            ids.add(bipolar.provenance.rule_id)
    for diode in EXTRACTION_DECK.diodes:
        if diode.provenance is not None:
            ids.add(diode.provenance.rule_id)
    return ids


_GOLDEN_PAIR_TESTED_RULE_IDS = frozenset(
    {
        "sg13_lv_nmos",
        "sg13_lv_pmos",
        "sg13_hv_nmos",
        "sg13_hv_pmos",
        "rsil",
        "rppd",
        "rhigh",
        "res_metal1",
        "res_metal2",
        "dantenna",
        "dpantenna",
        "cap_cmim",
        "rfcmim",
        "cap_cmomi",
        "cap_cmomf",
    }
)


def test_golden_pairs_cover_every_provenanced_sg13g2_device_rule():
    """Issue #905's own acceptance criterion: every compiled LVS device rule
    ships a golden layout->netlist pair. Mirrors
    `test_golden_pairs_cover_every_provenanced_sky130_device_rule` -- a
    future provenance-backfilled device rule (e.g. a resistor/capacitor
    entry) added without a matching golden-pair test fails this assertion
    loudly instead of silently under-covering."""
    assert _provenanced_device_rule_ids() == _GOLDEN_PAIR_TESTED_RULE_IDS
