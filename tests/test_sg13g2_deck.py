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


def test_sg13g2_extraction_deck_has_no_capacitor_bipolar_diode_entries():
    """Issue #1231 curates MOS (thin- *and* thick-oxide) plus the two
    unambiguous poly-resistor flavours only -- `diodes` remains empty
    because no follow-on issue has curated it yet, exactly like sky130/
    gf180mcu's own pre-#542 state (see `sg13g2.py`'s "Scope guard" docstring
    section).

    `capacitors` staying empty is a deliberate deferral, not a plain gap:
    issue #1233 investigated populating it for `cap_cmim`/`rfcmim` and found
    both plates land on Metal5/TopMetal1, above this deck's curated
    Metal1/Via1/Metal2 stack -- declaring the entry today would recognise an
    isolated-node capacitor (`CapacitorDevice`'s own documented "Known
    limitation"), so recognition is deferred until the stack itself is
    extended (issue #1243, shared with #1235's metal resistors). See
    `sg13g2.py`'s "MIM capacitors -- investigated, deferred" docstring
    section for the full finding.

    `bipolars` staying empty is the same kind of deferral for a different
    reason: issue #1232 *investigated* populating it and found the stock
    `BipolarDevice` model cannot faithfully express SG13G2's own
    `CustomBJTExtractor`-based derivation (see `sg13g2.py`'s "SiGe HBTs --
    investigated, declined" docstring section for the full finding) -- see
    `test_sg13g2_bipolars_declined_after_investigation` below for a test
    that documents *why*, not just *that*.

    Named explicitly here so a future extension of this deck must update
    this assertion, rather than silently leaving the coverage-discipline
    test below out of sync."""
    assert EXTRACTION_DECK.capacitors == ()
    assert EXTRACTION_DECK.bipolars == ()
    assert EXTRACTION_DECK.diodes == ()
    assert {r.name for r in EXTRACTION_DECK.resistors} == {"rsil", "rppd"}
    assert EXTRACTION_DECK.device_classes == ("nfet", "pfet", "resistor")


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
    """Both curated poly-resistor entries cite the real `res_extraction.lvs`
    `extract_devices(GeneralNTerminalExtractor.new(...))` call they were
    transcribed from (issue #1231), with the sheet resistance taken from the
    PDK's own `sg13g2_tech.json` `*_rspec` constant."""
    by_name = {r.name: r for r in EXTRACTION_DECK.resistors}
    for name in ("rsil", "rppd"):
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
# Coverage discipline
# --------------------------------------------------------------------------- #


def _provenanced_device_rule_ids() -> set[str]:
    """Mirrors `test_lvs_device_provenance.py`'s own
    `_provenanced_device_rule_ids`, scoped to sg13g2's `EXTRACTION_DECK`
    (MOS -- thin- and thick-oxide -- plus the two curated poly resistors; see
    `test_sg13g2_extraction_deck_has_no_capacitor_bipolar_diode_entries`
    above for what is still unrecognised)."""
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
