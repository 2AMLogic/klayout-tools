"""Tests for the LVS device-extraction rule model's per-rule provenance field
(issue #868, Epic #711 Phase 2a: "Define the LVS device-recognition rule
model in klt deck") and its full-coverage golden-pair validation (issue #867,
Phase 2b: "Compile sky130 LVS device-extraction rules with golden netlist
pairs").

Phase 1 (issue #747) added `DrcRule.provenance` -- a machine-readable
citation of the exact upstream PDK source each *DRC* rule was transcribed
from -- and validated it against a golden violate/pass pair per rule. This
module is the LVS-device-recognition counterpart: `RuleProvenance` (the same
type Phase 1 introduced) is now also carried by `ResistorDevice`,
`CapacitorDevice`, `BipolarDevice`, `DiodeDevice`, and (via
`ExtractionDeck.nfet_provenance`/`pfet_provenance`) MOS recognition -- see
`klayout_tools.decks`'s own docstrings for the field additions and
`decks/sky130.py`'s `EXTRACTION_DECK` for the backfilled citations.

Three tiers, mirroring `tests/test_golden_deck.py`'s split for the DRC side:

- **Provenance-populated assertions**: sky130's curated `EXTRACTION_DECK`
  cites a real, verifiable upstream `sky130.lvs` device-class name for its
  MOSFET, resistor, capacitor, and bipolar entries -- each device rule now
  traces to its PDK source line, closing issue #868's acceptance criterion
  of the same name.
- **Golden layout->netlist pairs**: a minimal, hand-computed synthetic
  layout per *compiled device rule* -- not just one representative per
  device *class* -- run through `run_extract`, asserting the extracted
  netlist's device parameters exactly match a value computed independently
  from the deck's own provenance-cited coefficients. Issue #868 (Phase 2a)
  validated one entry per named class (MOSFET: `nfet` only; resistor:
  `res_generic_po` only; capacitor: `sky130_fd_pr__model__cap_mim` only) as
  its model-definition pilot; issue #867 (Phase 2b) extends this to *every*
  entry that carries a populated `provenance` citation -- `pfet`,
  `res_high_po`, `res_xhigh_po`, `sky130_fd_pr__model__cap_mim_m4`, and the
  `pnp` bipolar -- closing #867's "every compiled device rule ships a golden
  pair" acceptance criterion. This validates the model end-to-end, from PDK
  source citation through to netlist output, not just that the field is
  populated.
- **Coverage discipline**
  (`test_golden_pairs_cover_every_provenanced_sky130_device_rule`): mirrors
  `test_golden_deck.py`'s own `test_golden_manifest_covers_every_width_
  space_rule` -- asserts the set of upstream `rule_id`s exercised by the
  golden-pair tests above exactly equals the set of `rule_id`s
  `EXTRACTION_DECK` actually declares `provenance` for, so a future
  provenance-backfilled device rule with no golden pair (or a golden pair
  for a rule_id the deck no longer declares) fails loudly instead of
  silently under-covering.
"""

from __future__ import annotations

from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.decks import RuleProvenance
from klayout_tools.decks.gf180mcu import EXTRACTION_DECK as GF180MCU_DECK
from klayout_tools.decks.sky130 import EXTRACTION_DECK
from klayout_tools.extract import run_extract

_DBU_UM = 0.001


def _box_um(x0: float, y0: float, x1: float, y1: float) -> kdb.Box:
    """A `kdb.Box` from micrometre coordinates at sky130's own
    `NOMINAL_DBU_UM` convention (1 nm/unit) -- mirrors `test_extract.py`'s
    own `_box_um` helper."""
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
# Provenance populated (issue #868 AC: "each device rule traces to its PDK
# source line")
# --------------------------------------------------------------------------- #


def test_sky130_mos_provenance_cites_sky130_lvs():
    """`EXTRACTION_DECK.nfet_provenance`/`pfet_provenance` cite the real
    `sky130.lvs` `mos4("sky130_fd_pr__nfet_01v8"/"...pfet_01v8")`
    `extract_devices` calls (verified against a real sky130A install, the
    same `volare enable sky130 <commit>` pin `sky130.py`'s DRC-rule
    provenance already cites)."""
    assert EXTRACTION_DECK.nfet_provenance == RuleProvenance(
        source_repo="efabless/sky130_klayout_pdk",
        source_path="libs.tech/klayout/lvs/sky130.lvs",
        rule_id="sky130_fd_pr__nfet_01v8",
        commit="c6d73a35f524070e85faff4a6a9eef49553ebc2b",
    )
    assert EXTRACTION_DECK.pfet_provenance == RuleProvenance(
        source_repo="efabless/sky130_klayout_pdk",
        source_path="libs.tech/klayout/lvs/sky130.lvs",
        rule_id="sky130_fd_pr__pfet_01v8",
        commit="c6d73a35f524070e85faff4a6a9eef49553ebc2b",
    )


def test_sky130_resistor_provenance_cites_sky130_lvs():
    """Every one of sky130's three curated resistor entries carries a
    `provenance` citing the real `sky130.lvs` device-class name its
    `sheet_rho_ohm_sq` was transcribed from/measured against."""
    by_name = {r.name: r for r in EXTRACTION_DECK.resistors}
    assert set(by_name) == {"res_generic_po", "res_high_po", "res_xhigh_po"}

    for resistor in EXTRACTION_DECK.resistors:
        assert resistor.provenance is not None
        assert resistor.provenance.source_repo == "efabless/sky130_klayout_pdk"
        assert resistor.provenance.source_path == "libs.tech/klayout/lvs/sky130.lvs"

    assert (
        by_name["res_generic_po"].provenance.rule_id == "sky130_fd_pr__res_generic_po"
    )
    assert by_name["res_high_po"].provenance.rule_id == "sky130_fd_pr__res_high_po_0p35"
    assert (
        by_name["res_xhigh_po"].provenance.rule_id == "sky130_fd_pr__res_xhigh_po_0p35"
    )


def test_sky130_capacitor_provenance_cites_sky130_lvs():
    """Both of sky130's curated MiM-capacitor entries carry a `provenance`
    citing the real `sky130.lvs` `extract_devices(capacitor(...))` call
    their `area_cap_f_um2` coefficient was refined from."""
    by_name = {c.name: c for c in EXTRACTION_DECK.capacitors}
    assert set(by_name) == {
        "sky130_fd_pr__model__cap_mim",
        "sky130_fd_pr__model__cap_mim_m4",
    }
    for capacitor in EXTRACTION_DECK.capacitors:
        assert capacitor.provenance is not None
        assert capacitor.provenance.rule_id == capacitor.name
        assert capacitor.provenance.source_repo == "efabless/sky130_klayout_pdk"
        assert capacitor.provenance.source_path == "libs.tech/klayout/lvs/sky130.lvs"


def test_sky130_bipolar_provenance_cites_sky130_lvs():
    """sky130's one curated bipolar entry (vertical PNP) carries a
    `provenance` citing a real `sky130.lvs` `bjt3(...)` device-class name."""
    (pnp,) = EXTRACTION_DECK.bipolars
    assert pnp.class_name == "pnp"
    assert pnp.provenance is not None
    assert pnp.provenance.rule_id == "sky130_fd_pr__pnp_05v5_W0p68L0p68"
    assert pnp.provenance.source_repo == "efabless/sky130_klayout_pdk"


def test_gf180mcu_devices_have_no_backfilled_provenance_yet():
    """Issue #868 backfills provenance on sky130 only, mirroring issue
    #747's own single-deck-first pilot scope for `DrcRule.provenance` --
    gf180mcu's device entries leave the field at its `None` default (an
    unpopulated field, not a claim that no provenance exists; the prose
    citation in `gf180mcu.py`'s own inline comments remains the record)."""
    assert GF180MCU_DECK.nfet_provenance is None
    assert GF180MCU_DECK.pfet_provenance is None
    assert GF180MCU_DECK.resistors, "sanity: deck still declares resistors"
    assert all(resistor.provenance is None for resistor in GF180MCU_DECK.resistors)
    assert all(capacitor.provenance is None for capacitor in GF180MCU_DECK.capacitors)
    assert all(bipolar.provenance is None for bipolar in GF180MCU_DECK.bipolars)
    assert all(diode.provenance is None for diode in GF180MCU_DECK.diodes)


# --------------------------------------------------------------------------- #
# Golden layout -> netlist pairs (issue #868 AC: "validated against at least
# one device class with a golden layout->netlist pair")
# --------------------------------------------------------------------------- #


def _make_nfet_layout() -> kdb.Layout:
    """One drawn NMOS on sky130's curated MOS-recognition layers: a 2x1um
    active strip crossed by a 0.4um-wide poly gate bar (active outside
    `nwell`, so it recognises as NMOS) -- the exact device
    `EXTRACTION_DECK.nfet_provenance` cites (`sky130_fd_pr__nfet_01v8`).
    `W` is the active strip's own 1um cross-extent; `L` is the poly bar's
    0.4um width."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(65, 20, _box_um(0, 0, 2, 1))  # diff.drawing, W=1um
    draw(66, 20, _box_um(0.8, -0.2, 1.2, 1.2))  # poly.drawing gate, L=0.4um

    draw(66, 44, _box_um(0.1, 0.3, 0.3, 0.7))  # licon1 (source side)
    draw(66, 44, _box_um(1.7, 0.3, 1.9, 0.7))  # licon1 (drain side)
    draw(67, 20, _box_um(0.0, 0.2, 0.4, 0.8))  # li1 (source pad)
    draw(67, 20, _box_um(1.6, 0.2, 2.0, 0.8))  # li1 (drain pad)
    label(67, 5, "S", 0.2, 0.5)
    label(67, 5, "D", 1.8, 0.5)

    draw(66, 44, _box_um(0.9, 1.0, 1.1, 1.2))  # gate contact
    draw(67, 20, _box_um(0.85, 0.95, 1.15, 1.25))  # gate pad
    label(67, 5, "G", 1.0, 1.1)

    return layout


def test_golden_pair_sky130_nfet_l_w_matches_drawn_geometry(tmp_path: Path):
    """A drawn 0.4um-gate / 1um-active-width NMOS extracts with exactly
    those `l_um`/`w_um` -- validating `EXTRACTION_DECK.nfet_provenance`'s
    `sky130_fd_pr__nfet_01v8` citation end-to-end: a golden layout, run
    through `run_extract`, reproduces the hand-computed netlist device
    parameters the drawn geometry implies."""
    path = _write_gds(_make_nfet_layout(), tmp_path / "nfet.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "nfet.spice"))

    assert report["device_counts"] == {"nfet": 1}
    (device,) = report["devices"]
    assert device["class"] == "nfet"
    assert device["params"]["l_um"] == pytest.approx(0.4)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert EXTRACTION_DECK.nfet_provenance.rule_id == "sky130_fd_pr__nfet_01v8"


def _make_pfet_layout() -> kdb.Layout:
    """The `_make_nfet_layout` geometry with the same active/poly/contact
    stack, wrapped in `nwell.drawing` (active *inside* `nwell` recognises as
    PMOS, the deck's own NMOS/PMOS split -- see `ExtractionDeck.active`/
    `.poly`/`.nwell`'s own docstring) -- the exact device
    `EXTRACTION_DECK.pfet_provenance` cites (`sky130_fd_pr__pfet_01v8`). A
    direct `nwell.pin` (`well_label`) text names the body/well net without
    needing a separate tap-contact stack, mirroring `well_label`'s own
    "label a pin on the drawn layer itself" convention (see
    `ExtractionDeck.well_label`'s docstring in `decks/__init__.py`)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(64, 20, _box_um(-1, -1, 3, 2))  # nwell.drawing, encloses active -> PMOS
    label(64, 5, "VPB", 2.5, 1.5)  # nwell.pin, body/well net label

    draw(65, 20, _box_um(0, 0, 2, 1))  # diff.drawing, W=1um
    draw(66, 20, _box_um(0.8, -0.2, 1.2, 1.2))  # poly.drawing gate, L=0.4um

    draw(66, 44, _box_um(0.1, 0.3, 0.3, 0.7))  # licon1 (source side)
    draw(66, 44, _box_um(1.7, 0.3, 1.9, 0.7))  # licon1 (drain side)
    draw(67, 20, _box_um(0.0, 0.2, 0.4, 0.8))  # li1 (source pad)
    draw(67, 20, _box_um(1.6, 0.2, 2.0, 0.8))  # li1 (drain pad)
    label(67, 5, "S", 0.2, 0.5)
    label(67, 5, "D", 1.8, 0.5)

    draw(66, 44, _box_um(0.9, 1.0, 1.1, 1.2))  # gate contact
    draw(67, 20, _box_um(0.85, 0.95, 1.15, 1.25))  # gate pad
    label(67, 5, "G", 1.0, 1.1)

    return layout


def test_golden_pair_sky130_pfet_l_w_matches_drawn_geometry(tmp_path: Path):
    """A drawn 0.4um-gate / 1um-active-width PMOS (active wrapped in
    `nwell`) extracts with exactly those `l_um`/`w_um` -- validating
    `EXTRACTION_DECK.pfet_provenance`'s `sky130_fd_pr__pfet_01v8` citation
    end-to-end, the PMOS sibling of
    `test_golden_pair_sky130_nfet_l_w_matches_drawn_geometry` above (issue
    #867)."""
    path = _write_gds(_make_pfet_layout(), tmp_path / "pfet.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "pfet.spice"))

    assert report["device_counts"] == {"pfet": 1}
    (device,) = report["devices"]
    assert device["class"] == "pfet"
    assert device["params"]["l_um"] == pytest.approx(0.4)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert EXTRACTION_DECK.pfet_provenance.rule_id == "sky130_fd_pr__pfet_01v8"


def _make_resistor_layout() -> kdb.Layout:
    """A drawn poly-resistor bar: a 12x1um `poly.drawing` bar with a 6um-long
    `poly.res` marker centred on it, contacted at both ends -- `L=6um`/
    `W=1um` (6.0 squares) against `res_generic_po`'s `sheet_rho_ohm_sq`,
    the coefficient `EXTRACTION_DECK.resistors[0].provenance` cites as
    transcribed from `sky130.lvs`'s `sky130_fd_pr__res_generic_po`."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(66, 20, _box_um(0, 0, 12, 1))  # poly.drawing bar, W=1um
    draw(66, 13, _box_um(3, 0, 9, 1))  # poly.res marker, 6um segment -> L=6um

    draw(66, 44, _box_um(0.1, 0.3, 0.3, 0.7))  # licon1 (head A)
    draw(66, 44, _box_um(11.7, 0.3, 11.9, 0.7))  # licon1 (head B)
    draw(67, 20, _box_um(0.0, 0.2, 0.4, 0.8))  # li1 (head A pad)
    draw(67, 20, _box_um(11.6, 0.2, 12.0, 0.8))  # li1 (head B pad)
    label(67, 5, "RA", 0.2, 0.5)
    label(67, 5, "RB", 11.8, 0.5)

    return layout


def test_golden_pair_sky130_resistor_r_ohm_matches_provenance_coefficient(
    tmp_path: Path,
):
    """A drawn 6-square poly resistor extracts with `R = squares *
    sheet_rho_ohm_sq`, computed directly from the deck's own
    provenance-cited `res_generic_po` entry -- proving the model (geometry
    + coefficient + provenance citation) reproduces the correct netlist
    value from a golden layout, not just that the citation string is
    present."""
    resistor = next(r for r in EXTRACTION_DECK.resistors if r.name == "res_generic_po")
    assert resistor.provenance.rule_id == "sky130_fd_pr__res_generic_po"

    path = _write_gds(_make_resistor_layout(), tmp_path / "res.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "res.spice"))

    assert report["device_counts"] == {"res_generic_po": 1}
    (device,) = report["devices"]
    assert device["class"] == "res_generic_po"
    squares = 6.0  # 6um marked segment / 1um width
    assert device["params"]["l_um"] == pytest.approx(6.0)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert device["params"]["r_ohm"] == pytest.approx(
        squares * resistor.sheet_rho_ohm_sq
    )


_RES_SQUARES = 6.0  # 6um marked segment / 1um width, shared by every resistor
# fixture below (`_make_resistor_layout` and `_make_precision_resistor_layout`)


def _make_precision_resistor_layout(
    extra_layers: tuple[tuple[int, int], ...],
) -> kdb.Layout:
    """`_make_resistor_layout`'s identical 12x1um `poly.drawing` bar / 6um
    `poly.res`-marked segment geometry, plus one or more additional
    `extra_layers` drawn over that same segment -- sky130's `res_high_po`/
    `res_xhigh_po` (`decks/sky130.py`'s `EXTRACTION_DECK.resistors`) each
    narrow the plain `poly.res`-marked `res_generic_po` candidate down to a
    specific precision flavour via `ResistorDevice.requires` (the P+ implant
    `psdm` plus a precision-implant mask, `rpm` or `urpm`), the same
    `requires`/`excludes` narrowing `test_extract.py`'s own
    `test_sky130_precision_implant_mask_extracts_own_flavour` exercises."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(66, 20, _box_um(0, 0, 12, 1))  # poly.drawing bar, W=1um
    draw(66, 13, _box_um(3, 0, 9, 1))  # poly.res marker, 6um segment -> L=6um
    for layer, datatype in extra_layers:
        draw(layer, datatype, _box_um(2.8, -0.2, 9.2, 1.2))

    draw(66, 44, _box_um(0.1, 0.3, 0.3, 0.7))  # licon1 (head A)
    draw(66, 44, _box_um(11.7, 0.3, 11.9, 0.7))  # licon1 (head B)
    draw(67, 20, _box_um(0.0, 0.2, 0.4, 0.8))  # li1 (head A pad)
    draw(67, 20, _box_um(11.6, 0.2, 12.0, 0.8))  # li1 (head B pad)
    label(67, 5, "RA", 0.2, 0.5)
    label(67, 5, "RB", 11.8, 0.5)

    return layout


def test_golden_pair_sky130_res_high_po_r_ohm_matches_provenance_coefficient(
    tmp_path: Path,
):
    """A drawn 6-square poly bar additionally marked with `psdm`/`rpm`
    extracts as `res_high_po` with `R = squares * sheet_rho_ohm_sq +
    fixed_offset_ohm` -- the fixed head/end-effect correction issue #518
    measured against the real PDK model -- computed directly from the
    deck's own provenance-cited `res_high_po` entry (issue #867)."""
    resistor = next(r for r in EXTRACTION_DECK.resistors if r.name == "res_high_po")
    assert resistor.provenance.rule_id == "sky130_fd_pr__res_high_po_0p35"

    layout = _make_precision_resistor_layout(((94, 20), (86, 20)))  # psdm, rpm
    path = _write_gds(layout, tmp_path / "res_high_po.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "res_high_po.spice"))

    assert report["device_counts"] == {"res_high_po": 1}
    (device,) = report["devices"]
    assert device["class"] == "res_high_po"
    assert device["params"]["r_ohm"] == pytest.approx(
        _RES_SQUARES * resistor.sheet_rho_ohm_sq + resistor.fixed_offset_ohm
    )


def test_golden_pair_sky130_res_xhigh_po_r_ohm_matches_provenance_coefficient(
    tmp_path: Path,
):
    """The `res_high_po` golden pair's sibling for `res_xhigh_po`: `psdm` +
    `urpm` (instead of `rpm`) narrows the same marked poly bar to the
    2 kohm/sq flavour, whose `fixed_offset_ohm` is untouched at its `0.0`
    default (issue #867)."""
    resistor = next(r for r in EXTRACTION_DECK.resistors if r.name == "res_xhigh_po")
    assert resistor.provenance.rule_id == "sky130_fd_pr__res_xhigh_po_0p35"

    layout = _make_precision_resistor_layout(((94, 20), (79, 20)))  # psdm, urpm
    path = _write_gds(layout, tmp_path / "res_xhigh_po.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "res_xhigh_po.spice"))

    assert report["device_counts"] == {"res_xhigh_po": 1}
    (device,) = report["devices"]
    assert device["class"] == "res_xhigh_po"
    assert device["params"]["r_ohm"] == pytest.approx(
        _RES_SQUARES * resistor.sheet_rho_ohm_sq + resistor.fixed_offset_ohm
    )


def test_golden_pair_sky130_capacitor_c_f_matches_provenance_coefficients(
    tmp_path: Path,
):
    """A drawn 10x5um `capm` MiM-cap top plate over a `met3.drawing` bottom
    plate extracts with `C = area * area_cap_f_um2 + perimeter *
    perim_cap_f_um`, computed directly from the deck's own
    provenance-cited `sky130_fd_pr__model__cap_mim` entry -- the third of
    issue #868's three named device classes (MOSFET, resistor, capacitor)
    validated against a golden layout->netlist pair."""
    capacitor = next(
        c
        for c in EXTRACTION_DECK.capacitors
        if c.name == "sky130_fd_pr__model__cap_mim"
    )
    assert capacitor.provenance.rule_id == "sky130_fd_pr__model__cap_mim"

    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    draw(70, 20, _box_um(-20, -20, 20, 20))  # met3.drawing (bottom plate)
    draw(89, 44, _box_um(0, 0, 10, 5))  # capm.drawing (top plate, 10x5um)

    path = _write_gds(layout, tmp_path / "cap.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "cap.spice"))

    assert report["device_counts"] == {"sky130_fd_pr__model__cap_mim": 1}
    (device,) = report["devices"]
    area_um2 = 50.0
    perimeter_um = 30.0
    expected_c_f = (
        area_um2 * capacitor.area_cap_f_um2 + perimeter_um * capacitor.perim_cap_f_um
    )
    assert device["params"]["area_um2"] == pytest.approx(area_um2)
    assert device["params"]["perimeter_um"] == pytest.approx(perimeter_um)
    assert device["params"]["c_f"] == pytest.approx(expected_c_f)


def test_golden_pair_sky130_capacitor_cap_mim_m4_c_f_matches_provenance_coefficients(
    tmp_path: Path,
):
    """The `cap_mim` golden pair's sibling for sky130's *second* independent
    MiM stack, `sky130_fd_pr__model__cap_mim_m4` (`capm2.drawing` top plate
    over a `met4.drawing` bottom plate, one metal level up from `cap_mim`'s
    `met3.drawing`) -- same `C = area * area_cap_f_um2 + perimeter *
    perim_cap_f_um` formula, same coefficients (both stacks share the tt-
    corner `camimc`/`cpmimc` values, see `decks/sky130.py`'s provenance
    note), different plates (issue #867)."""
    capacitor = next(
        c
        for c in EXTRACTION_DECK.capacitors
        if c.name == "sky130_fd_pr__model__cap_mim_m4"
    )
    assert capacitor.provenance.rule_id == "sky130_fd_pr__model__cap_mim_m4"

    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    draw(71, 20, _box_um(-20, -20, 20, 20))  # met4.drawing (bottom plate)
    draw(97, 44, _box_um(0, 0, 10, 5))  # capm2.drawing (top plate, 10x5um)

    path = _write_gds(layout, tmp_path / "cap_m4.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "cap_m4.spice"))

    assert report["device_counts"] == {"sky130_fd_pr__model__cap_mim_m4": 1}
    (device,) = report["devices"]
    area_um2 = 50.0
    perimeter_um = 30.0
    expected_c_f = (
        area_um2 * capacitor.area_cap_f_um2 + perimeter_um * capacitor.perim_cap_f_um
    )
    assert device["params"]["area_um2"] == pytest.approx(area_um2)
    assert device["params"]["perimeter_um"] == pytest.approx(perimeter_um)
    assert device["params"]["c_f"] == pytest.approx(expected_c_f)


def _make_pnp_layout() -> kdb.Layout:
    """One drawn vertical PNP on sky130's curated bipolar-recognition
    layers: an `nwell.drawing` base marked with `pnp.drawing`, a p+
    `diff.drawing` emitter inside it (contacted + labelled), and a base tie
    on the *distinct* `tap.drawing` layer (also contacted + labelled) -- the
    exact device `EXTRACTION_DECK.bipolars[0].provenance` cites
    (`sky130_fd_pr__pnp_05v5_W0p68L0p68`). Mirrors `test_extract.py`'s own
    `_make_sky130_bjt_layout` fixture (no drawn collector -- the collector is
    the native P-substrate, tied to the deck's `substrate_net` global, per
    `BipolarDevice`'s docstring)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(64, 20, _box_um(0, 0, 2, 2))  # nwell.drawing (base)
    draw(82, 44, _box_um(0, 0, 2, 2))  # pnp.drawing (device-mark)

    draw(65, 20, _box_um(0.8, 0.8, 1.2, 1.2))  # diff.drawing (emitter)
    draw(66, 44, _box_um(0.9, 0.9, 1.1, 1.1))  # licon1 over the emitter
    draw(67, 20, _box_um(0.85, 0.85, 1.15, 1.15))  # li1 over the emitter
    label(67, 5, "EMIT", 1.0, 1.0)

    draw(65, 44, _box_um(0.1, 0.1, 0.3, 0.3))  # tap.drawing (base tie)
    draw(66, 44, _box_um(0.15, 0.15, 0.25, 0.25))  # licon1 over the tap
    draw(67, 20, _box_um(0.1, 0.1, 0.3, 0.3))  # li1 over the tap
    label(64, 5, "BASE", 0.2, 0.2)

    return layout


def test_golden_pair_sky130_pnp_extracts_with_provenance_cited_device(
    tmp_path: Path,
):
    """A drawn vertical PNP (base=`nwell`, emitter=`diff`, marker=
    `pnp.drawing`) extracts as exactly one `pnp` device with its emitter/base
    terminals resolved to their labelled nets and the collector tied to the
    deck's substrate global -- validating
    `EXTRACTION_DECK.bipolars[0].provenance`'s `sky130_fd_pr__pnp_05v5_
    W0p68L0p68` citation end-to-end (issue #867).

    Unlike the MOSFET/resistor/capacitor golden pairs above,
    `DeviceClassBJT3Transistor`'s `AE`/`AB`/`AC` area parameters are not
    surfaced in `report["devices"][].params` at all (`extract.py`'s
    `_describe_devices` has no `elif` branch for those parameter names --
    only `W`/`L`/`C`/`A`/`P`/`R`/`AS`/`AD`/`PS`/`PD` are read back), so there
    is no per-rule numeric coefficient here to cross-check against
    `provenance` the way `sheet_rho_ohm_sq`/`area_cap_f_um2` provide for
    resistors/capacitors -- correct class + net resolution *is* this golden
    pair's "expected SPICE device out", the full bar this device rule's
    citation (a device-class-name selection, not a geometric coefficient)
    actually supports."""
    (pnp,) = EXTRACTION_DECK.bipolars
    assert pnp.provenance.rule_id == "sky130_fd_pr__pnp_05v5_W0p68L0p68"

    path = _write_gds(_make_pnp_layout(), tmp_path / "pnp.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "pnp.spice"))

    assert report["device_counts"] == {"pnp": 1}
    (device,) = report["devices"]
    assert device["class"] == "pnp"
    assert device["nets"] == {"c": "vsubs", "b": "BASE", "e": "EMIT"}


# --------------------------------------------------------------------------- #
# Coverage discipline (issue #867 AC: "every compiled device rule ships a
# golden pair")
# --------------------------------------------------------------------------- #


def _provenanced_device_rule_ids(deck) -> set[str]:
    """Every upstream `RuleProvenance.rule_id` `deck` declares -- across MOS
    (`nfet_provenance`/`pfet_provenance`), `resistors`, `capacitors`,
    `bipolars`, and `diodes` -- with a populated (non-`None`) `provenance`
    field. Mirrors `tests/test_golden_deck.py`'s own `{rule.id for rule in
    DECK if rule.check in (...)}` coverage-set construction for the DRC
    side, generalised across `ExtractionDeck`'s five device-recognition
    collections instead of one flat rule list."""
    ids: set[str] = set()
    if deck.nfet_provenance is not None:
        ids.add(deck.nfet_provenance.rule_id)
    if deck.pfet_provenance is not None:
        ids.add(deck.pfet_provenance.rule_id)
    for resistor in deck.resistors:
        if resistor.provenance is not None:
            ids.add(resistor.provenance.rule_id)
    for capacitor in deck.capacitors:
        if capacitor.provenance is not None:
            ids.add(capacitor.provenance.rule_id)
    for bipolar in deck.bipolars:
        if bipolar.provenance is not None:
            ids.add(bipolar.provenance.rule_id)
    for diode in deck.diodes:
        if diode.provenance is not None:
            ids.add(diode.provenance.rule_id)
    return ids


#: Upstream `rule_id`s exercised by a golden layout->netlist pair test in
#: this module, as of issue #867 -- hand-maintained (there is no single
#: generic fixture generator for device-recognition geometry the way
#: `tests/golden_deck/generate_golden_deck.py` derives DRC width/space
#: fixtures from a threshold alone; each device class's recognition geometry
#: -- a MOS gate, a marked poly bar, a MiM-cap plate pair, a marked-nwell
#: BJT -- is structurally distinct). A rule_id landing here with no
#: corresponding test above, or a golden-pair test above whose rule_id is
#: missing here, is caught by `_provenanced_device_rule_ids` not matching
#: this set, not by silent under-coverage.
_GOLDEN_PAIR_TESTED_RULE_IDS = frozenset(
    {
        "sky130_fd_pr__nfet_01v8",
        "sky130_fd_pr__pfet_01v8",
        "sky130_fd_pr__res_generic_po",
        "sky130_fd_pr__res_high_po_0p35",
        "sky130_fd_pr__res_xhigh_po_0p35",
        "sky130_fd_pr__model__cap_mim",
        "sky130_fd_pr__model__cap_mim_m4",
        "sky130_fd_pr__pnp_05v5_W0p68L0p68",
    }
)


def test_golden_pairs_cover_every_provenanced_sky130_device_rule():
    """Issue #867's own acceptance criterion: 'every compiled device rule
    ships a golden pair (layout in, expected SPICE device out) the deck
    correctly extracts.' `EXTRACTION_DECK`'s provenance-cited device rules
    (issue #868, Phase 2a: MOS, all three resistors, both capacitors, the
    one bipolar -- 8 entries total) are all backfilled and, as of this
    module's golden-pair tests above, all individually validated -- this
    coverage test is what keeps that true: a new provenance-backfilled
    device rule added without a matching golden-pair test (or a stale
    `_GOLDEN_PAIR_TESTED_RULE_IDS` entry for a rule_id the deck no longer
    declares) fails this assertion, exactly like
    `test_golden_deck.py`'s `test_golden_manifest_covers_every_width_
    space_rule` does for the DRC side."""
    assert _provenanced_device_rule_ids(EXTRACTION_DECK) == _GOLDEN_PAIR_TESTED_RULE_IDS
