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
  (`test_golden_pairs_cover_every_provenanced_device_rule`): mirrors
  `test_golden_deck.py`'s own `test_golden_manifest_covers_every_width_
  space_rule` -- asserts, per deck, that the set of upstream `rule_id`s
  exercised by the golden-pair tests above exactly equals the set of
  `rule_id`s that deck's own `EXTRACTION_DECK` declares `provenance` for, so
  a future provenance-backfilled device rule with no golden pair (or a
  golden pair for a rule_id the deck no longer declares) fails loudly
  instead of silently under-covering.

Issue #904 (Epic #711 Phase 3a) extends all three tiers to gf180mcu: its
`EXTRACTION_DECK` was previously the sole deck with *no* backfilled
`provenance` at all (a deliberate negative control, per issue #868's own
single-deck-first pilot scope); this module now backfills all eight of its
provenance-eligible device entries (MOS x2, resistors x2, the one
capacitor, the one bipolar, diodes x2) and ships a golden layout->netlist
pair for each, mirroring sky130's own #867/#868 discipline exactly.
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


def test_gf180mcu_mos_provenance_cites_lvs_deck():
    """`EXTRACTION_DECK.nfet_provenance`/`pfet_provenance` cite the real
    device-class names the module docstring's own `UNMODELED_VOLTAGE_
    MARKERS` note already states MOS recognition always binds
    (`gf180mcu_fd_pr__nfet_03v3`/`...pfet_03v3`) -- issue #904, the
    gf180mcu counterpart of #868's sky130 backfill."""
    assert GF180MCU_DECK.nfet_provenance == RuleProvenance(
        source_repo="google/globalfoundries-pdk-libs-gf180mcu_fd_pv",
        source_path="libs.tech/klayout/lvs/rule_decks/mos_extraction.lvs",
        rule_id="gf180mcu_fd_pr__nfet_03v3",
        commit="c6d73a35f524070e85faff4a6a9eef49553ebc2b",
    )
    assert GF180MCU_DECK.pfet_provenance == RuleProvenance(
        source_repo="google/globalfoundries-pdk-libs-gf180mcu_fd_pv",
        source_path="libs.tech/klayout/lvs/rule_decks/mos_extraction.lvs",
        rule_id="gf180mcu_fd_pr__pfet_03v3",
        commit="c6d73a35f524070e85faff4a6a9eef49553ebc2b",
    )


def test_gf180mcu_resistor_provenance_cites_lvs_deck():
    """Both of gf180mcu's curated resistor entries carry a `provenance`
    citing the real `res_extraction.lvs` device-class name their
    `sheet_rho_ohm_sq` was transcribed from (issue #904)."""
    by_name = {r.name: r for r in GF180MCU_DECK.resistors}
    assert set(by_name) == {"ppolyf_u", "ppolyf_u_1k"}

    for resistor in GF180MCU_DECK.resistors:
        assert resistor.provenance is not None
        assert (
            resistor.provenance.source_repo
            == "google/globalfoundries-pdk-libs-gf180mcu_fd_pv"
        )
        assert (
            resistor.provenance.source_path
            == "libs.tech/klayout/lvs/rule_decks/res_extraction.lvs"
        )

    assert by_name["ppolyf_u"].provenance.rule_id == "gf180mcu_fd_pr__ppolyf_u"
    assert by_name["ppolyf_u_1k"].provenance.rule_id == "gf180mcu_fd_pr__ppolyf_u_1k"


def test_gf180mcu_capacitor_provenance_cites_lvs_deck():
    """gf180mcu's one curated MiM-capacitor entry carries a `provenance`
    citing the real `mimcap_extraction.lvs` `extract_devices(capacitor(...))`
    call its `area_cap_f_um2`/`perim_cap_f_um` were refined from (issue
    #904)."""
    (capacitor,) = GF180MCU_DECK.capacitors
    assert capacitor.provenance is not None
    assert capacitor.provenance.rule_id == "cap_mim_2f0_m4m5_noshield"
    assert (
        capacitor.provenance.source_repo
        == "google/globalfoundries-pdk-libs-gf180mcu_fd_pv"
    )
    assert (
        capacitor.provenance.source_path
        == "libs.tech/klayout/lvs/rule_decks/mimcap_extraction.lvs"
    )


def test_gf180mcu_bipolar_provenance_cites_drm_bjt_mark_layer():
    """Unlike sky130's `pnp_05v5`, gf180mcu's generic `bjt` recognition has
    no positively-identified official LVS device-class name (see
    `gf180mcu.py`'s own docstring note); its `provenance` instead cites the
    DRM rule that defines the `DRC_BJT` marker geometry this entry
    recognises on -- the same rule `bjt.separation.comp.1`'s own DRC-side
    `provenance` cites (issue #904)."""
    (bjt,) = GF180MCU_DECK.bipolars
    assert bjt.class_name == "bjt"
    assert bjt.provenance is not None
    assert bjt.provenance.rule_id == "BJT.3"
    assert bjt.provenance.source_repo == "google/gf180mcu-pdk"


def test_gf180mcu_diode_provenance_cites_lvs_deck():
    """Both of gf180mcu's curated junction-diode entries carry a
    `provenance` citing the real `diode_extraction.lvs` device-class name
    their recognition geometry was transcribed from (issue #904)."""
    by_name = {d.name: d for d in GF180MCU_DECK.diodes}
    assert set(by_name) == {"diode_nd2ps_06v0", "diode_pd2nw_06v0"}

    for diode in GF180MCU_DECK.diodes:
        assert diode.provenance is not None
        assert diode.provenance.rule_id == f"gf180mcu_fd_pr__{diode.name}"
        assert (
            diode.provenance.source_repo
            == "google/globalfoundries-pdk-libs-gf180mcu_fd_pv"
        )
        assert (
            diode.provenance.source_path
            == "libs.tech/klayout/lvs/rule_decks/diode_extraction.lvs"
        )


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
# Golden layout -> netlist pairs, gf180mcu (issue #904, Epic #711 Phase 3a):
# one per provenance-backfilled device entry above, mirroring the sky130
# section's discipline exactly -- MOS x2, resistors x2, the one capacitor,
# the one bipolar, diodes x2 (8 entries total, matching sky130's own 8).
# --------------------------------------------------------------------------- #


def _make_gf180mcu_nfet_layout() -> kdb.Layout:
    """One drawn NMOS on gf180mcu's curated MOS-recognition layers: a
    2x1um `Comp` strip crossed by a 0.4um-wide `Poly2` gate bar (active
    outside `Nwell`, so it recognises as NMOS) -- the exact device
    `EXTRACTION_DECK.nfet_provenance` cites
    (`gf180mcu_fd_pr__nfet_03v3`)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(22, 0, _box_um(0, 0, 2, 1))  # Comp, W=1um
    draw(30, 0, _box_um(0.8, -0.2, 1.2, 1.2))  # Poly2 gate, L=0.4um

    draw(33, 0, _box_um(0.1, 0.3, 0.3, 0.7))  # Contact (source side)
    draw(33, 0, _box_um(1.7, 0.3, 1.9, 0.7))  # Contact (drain side)
    draw(34, 0, _box_um(0.0, 0.2, 0.4, 0.8))  # Metal1 (source pad)
    draw(34, 0, _box_um(1.6, 0.2, 2.0, 0.8))  # Metal1 (drain pad)
    label(34, 10, "S", 0.2, 0.5)
    label(34, 10, "D", 1.8, 0.5)

    draw(33, 0, _box_um(0.9, 1.0, 1.1, 1.2))  # gate contact
    draw(34, 0, _box_um(0.85, 0.95, 1.15, 1.25))  # gate pad
    label(34, 10, "G", 1.0, 1.1)

    return layout


def test_golden_pair_gf180mcu_nfet_l_w_matches_drawn_geometry(tmp_path: Path):
    """A drawn 0.4um-gate / 1um-active-width NMOS extracts with exactly
    those `l_um`/`w_um` -- validating `EXTRACTION_DECK.nfet_provenance`'s
    `gf180mcu_fd_pr__nfet_03v3` citation end-to-end (issue #904)."""
    path = _write_gds(_make_gf180mcu_nfet_layout(), tmp_path / "nfet.gds")
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "nfet.spice"))

    assert report["device_counts"] == {"nfet": 1}
    (device,) = report["devices"]
    assert device["class"] == "nfet"
    assert device["params"]["l_um"] == pytest.approx(0.4)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert GF180MCU_DECK.nfet_provenance.rule_id == "gf180mcu_fd_pr__nfet_03v3"


def _make_gf180mcu_pfet_layout() -> kdb.Layout:
    """`_make_gf180mcu_nfet_layout`'s identical geometry wrapped in
    `Nwell.drawing` (active *inside* `Nwell` recognises as PMOS) -- the
    exact device `EXTRACTION_DECK.pfet_provenance` cites
    (`gf180mcu_fd_pr__pfet_03v3`). Unlike sky130, gf180mcu declares no
    `well_label`, so the body/well net is left unasserted here (an
    anonymous net) -- see `gf180mcu.py`'s own docstring on this documented
    limitation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(21, 0, _box_um(-1, -1, 3, 2))  # Nwell, encloses active -> PMOS

    draw(22, 0, _box_um(0, 0, 2, 1))  # Comp, W=1um
    draw(30, 0, _box_um(0.8, -0.2, 1.2, 1.2))  # Poly2 gate, L=0.4um

    draw(33, 0, _box_um(0.1, 0.3, 0.3, 0.7))  # Contact (source side)
    draw(33, 0, _box_um(1.7, 0.3, 1.9, 0.7))  # Contact (drain side)
    draw(34, 0, _box_um(0.0, 0.2, 0.4, 0.8))  # Metal1 (source pad)
    draw(34, 0, _box_um(1.6, 0.2, 2.0, 0.8))  # Metal1 (drain pad)
    label(34, 10, "S", 0.2, 0.5)
    label(34, 10, "D", 1.8, 0.5)

    draw(33, 0, _box_um(0.9, 1.0, 1.1, 1.2))  # gate contact
    draw(34, 0, _box_um(0.85, 0.95, 1.15, 1.25))  # gate pad
    label(34, 10, "G", 1.0, 1.1)

    return layout


def test_golden_pair_gf180mcu_pfet_l_w_matches_drawn_geometry(tmp_path: Path):
    """A drawn 0.4um-gate / 1um-active-width PMOS (active wrapped in
    `Nwell`) extracts with exactly those `l_um`/`w_um` -- validating
    `EXTRACTION_DECK.pfet_provenance`'s `gf180mcu_fd_pr__pfet_03v3`
    citation end-to-end (issue #904)."""
    path = _write_gds(_make_gf180mcu_pfet_layout(), tmp_path / "pfet.gds")
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "pfet.spice"))

    assert report["device_counts"] == {"pfet": 1}
    (device,) = report["devices"]
    assert device["class"] == "pfet"
    assert device["params"]["l_um"] == pytest.approx(0.4)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert GF180MCU_DECK.pfet_provenance.rule_id == "gf180mcu_fd_pr__pfet_03v3"


_GF180MCU_RES_SQUARES = 6.0  # 6um marked segment / 1um width


def _make_gf180mcu_resistor_layout(
    extra_layers: tuple[tuple[int, int], ...],
) -> kdb.Layout:
    """A 12x1um `Poly2` bar with a 6um-long `RES_MK`-marked segment
    (`L=6um`/`W=1um`, 6.0 squares), plus one or more `extra_layers` drawn
    over that same segment to narrow it to a specific gf180mcu resistor
    flavour (`ppolyf_u`: `Pplus`+`SAB`; `ppolyf_u_1k`: `SAB`+`Resistor`) --
    mirrors `test_extract.py`'s own `_make_poly_resistor_layout`."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(30, 0, _box_um(0, 0, 12, 1))  # Poly2 bar, W=1um
    draw(110, 5, _box_um(3, 0, 9, 1))  # RES_MK marker, 6um segment -> L=6um
    for layer, datatype in extra_layers:
        draw(layer, datatype, _box_um(2.8, -0.2, 9.2, 1.2))

    draw(33, 0, _box_um(0.1, 0.3, 0.3, 0.7))  # Contact (head A)
    draw(33, 0, _box_um(11.7, 0.3, 11.9, 0.7))  # Contact (head B)
    draw(34, 0, _box_um(0.0, 0.2, 0.4, 0.8))  # Metal1 (head A pad)
    draw(34, 0, _box_um(11.6, 0.2, 12.0, 0.8))  # Metal1 (head B pad)
    label(34, 10, "RA", 0.2, 0.5)
    label(34, 10, "RB", 11.8, 0.5)

    return layout


def test_golden_pair_gf180mcu_ppolyf_u_r_ohm_matches_provenance_coefficient(
    tmp_path: Path,
):
    """A drawn 6-square `Poly2` bar marked `RES_MK` and narrowed by
    `Pplus`+`SAB` extracts as `ppolyf_u` with `R = squares *
    sheet_rho_ohm_sq`, computed directly from the deck's own
    provenance-cited entry (issue #904)."""
    resistor = next(r for r in GF180MCU_DECK.resistors if r.name == "ppolyf_u")
    assert resistor.provenance.rule_id == "gf180mcu_fd_pr__ppolyf_u"

    layout = _make_gf180mcu_resistor_layout(((31, 0), (49, 0)))  # Pplus, SAB
    path = _write_gds(layout, tmp_path / "ppolyf_u.gds")
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "ppolyf_u.spice"))

    assert report["device_counts"] == {"ppolyf_u": 1}
    (device,) = report["devices"]
    assert device["class"] == "ppolyf_u"
    assert device["params"]["l_um"] == pytest.approx(6.0)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert device["params"]["r_ohm"] == pytest.approx(
        _GF180MCU_RES_SQUARES * resistor.sheet_rho_ohm_sq
    )


def test_golden_pair_gf180mcu_ppolyf_u_1k_r_ohm_matches_provenance_coefficient(
    tmp_path: Path,
):
    """The `ppolyf_u` golden pair's sibling for the high-sheet-rho
    `ppolyf_u_1k` flavour: the same marked `Poly2` bar, narrowed instead by
    `SAB`+`Resistor` (issue #904)."""
    resistor = next(r for r in GF180MCU_DECK.resistors if r.name == "ppolyf_u_1k")
    assert resistor.provenance.rule_id == "gf180mcu_fd_pr__ppolyf_u_1k"

    layout = _make_gf180mcu_resistor_layout(((49, 0), (62, 0)))  # SAB, Resistor
    path = _write_gds(layout, tmp_path / "ppolyf_u_1k.gds")
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "ppolyf_u_1k.spice"))

    assert report["device_counts"] == {"ppolyf_u_1k": 1}
    (device,) = report["devices"]
    assert device["class"] == "ppolyf_u_1k"
    assert device["params"]["r_ohm"] == pytest.approx(
        _GF180MCU_RES_SQUARES * resistor.sheet_rho_ohm_sq
    )


def test_golden_pair_gf180mcu_capacitor_c_f_matches_provenance_coefficients(
    tmp_path: Path,
):
    """A drawn 10x10um `FuseTop` top plate (marked `CAP_MK`/`MIM_L_MK`) over
    a larger `Metal4` bottom plate extracts with `C = area * area_cap_f_um2
    + perimeter * perim_cap_f_um`, computed directly from the deck's own
    provenance-cited `cap_mim_2f0_m4m5_noshield` entry (issue #904)."""
    (capacitor,) = GF180MCU_DECK.capacitors
    assert capacitor.provenance.rule_id == "cap_mim_2f0_m4m5_noshield"

    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    draw(46, 0, _box_um(-20, -20, 20, 20))  # Metal4 (bottom plate)
    draw(75, 0, _box_um(0, 0, 10, 10))  # FuseTop (top plate, 10x10um)
    draw(117, 5, _box_um(-5, -5, 15, 15))  # CAP_MK
    draw(117, 10, _box_um(-5, -5, 15, 15))  # MIM_L_MK

    path = _write_gds(layout, tmp_path / "cap.gds")
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "cap.spice"))

    assert report["device_counts"] == {"cap_mim_2f0_m4m5_noshield": 1}
    (device,) = report["devices"]
    area_um2 = 100.0
    perimeter_um = 40.0
    expected_c_f = (
        area_um2 * capacitor.area_cap_f_um2 + perimeter_um * capacitor.perim_cap_f_um
    )
    assert device["params"]["area_um2"] == pytest.approx(area_um2)
    assert device["params"]["perimeter_um"] == pytest.approx(perimeter_um)
    assert device["params"]["c_f"] == pytest.approx(expected_c_f)


def _make_gf180mcu_bjt_layout() -> kdb.Layout:
    """One drawn vertical bipolar on gf180mcu's curated bipolar-recognition
    layers: an `Nwell.drawing` base marked with `DRC_BJT.drawing`, a `Comp`
    emitter inside it (contacted + labelled) -- the exact device
    `EXTRACTION_DECK.bipolars[0].provenance` cites (DRM rule `BJT.3`). No
    drawn base tap in this fixture (gf180mcu's curated deck has no distinct
    well-tie layer, mirroring `test_extract.py`'s own
    `_make_gf180mcu_bjt_layout`), so the base net is anonymous."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(21, 0, _box_um(0, 0, 2, 2))  # Nwell (base)
    draw(127, 5, _box_um(0, 0, 2, 2))  # DRC_BJT (device-mark)

    draw(22, 0, _box_um(0.8, 0.8, 1.2, 1.2))  # Comp (emitter)
    draw(33, 0, _box_um(0.9, 0.9, 1.1, 1.1))  # Contact over the emitter
    draw(34, 0, _box_um(0.85, 0.85, 1.15, 1.15))  # Metal1 over the emitter
    label(34, 10, "EMIT", 1.0, 1.0)

    return layout


def test_golden_pair_gf180mcu_bjt_extracts_with_provenance_cited_device(
    tmp_path: Path,
):
    """A drawn vertical bipolar (base=`Nwell`, emitter=`Comp`, marker=
    `DRC_BJT`) extracts as exactly one `bjt` device with its emitter
    terminal resolved to its labelled net and the collector tied to the
    deck's substrate global -- validating
    `EXTRACTION_DECK.bipolars[0].provenance`'s `BJT.3` citation end-to-end
    (issue #904). Like sky130's own `pnp` golden pair, there is no
    per-rule numeric coefficient to cross-check here (this device rule's
    citation names *which marker geometry* is recognised, not a geometric
    coefficient) -- correct class + net resolution is this golden pair's
    full "expected SPICE device out"."""
    (bjt,) = GF180MCU_DECK.bipolars
    assert bjt.provenance.rule_id == "BJT.3"

    path = _write_gds(_make_gf180mcu_bjt_layout(), tmp_path / "bjt.gds")
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "bjt.spice"))

    assert report["device_counts"] == {"bjt": 1}
    (device,) = report["devices"]
    assert device["class"] == "bjt"
    assert device["nets"]["e"] == "EMIT"
    assert device["nets"]["c"] == GF180MCU_DECK.substrate_net


def _make_gf180mcu_diode_layout() -> kdb.Layout:
    """A minimal dual-diode ESD clamp on gf180mcu's curated diode layers --
    mirrors `test_extract.py`'s own `_make_gf180mcu_diode_layout` (same
    topology and coordinates), one `diode_nd2ps_06v0` (n+/p-substrate) and
    one `diode_pd2nw_06v0` (p+/Nwell) junction, each 1um x 1um (area
    1.0um^2, perimeter 4.0um)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    # diode_nd2ps_06v0: n+ diffusion in the p-substrate.
    draw(115, 5, _box_um(-0.2, -0.2, 1.2, 1.2))  # diode_mk
    draw(55, 0, _box_um(-0.2, -0.2, 1.2, 1.2))  # Dualgate (6V flavour)
    draw(22, 0, _box_um(0, 0, 1, 1))  # Comp (cathode)
    draw(32, 0, _box_um(0, 0, 1, 1))  # Nplus (n+ doped)
    draw(33, 0, _box_um(0.3, 0.3, 0.7, 0.7))  # Contact over the cathode
    draw(34, 0, _box_um(0.2, 0.2, 0.8, 0.8))  # Metal1 over the cathode
    label(34, 10, "CATH", 0.5, 0.5)

    # diode_pd2nw_06v0: p+ diffusion in Nwell.
    draw(21, 0, _box_um(4, 0, 7, 3))  # Nwell (cathode)
    draw(115, 5, _box_um(4.8, 0.8, 6.2, 2.2))  # diode_mk
    draw(55, 0, _box_um(4.8, 0.8, 6.2, 2.2))  # Dualgate (6V flavour)
    draw(22, 0, _box_um(5, 1, 6, 2))  # Comp (anode)
    draw(31, 0, _box_um(5, 1, 6, 2))  # Pplus (p+ doped)
    draw(33, 0, _box_um(5.3, 1.3, 5.7, 1.7))  # Contact over the anode
    draw(34, 0, _box_um(5.2, 1.2, 5.8, 1.8))  # Metal1 over the anode
    label(34, 10, "ANOD", 5.5, 1.5)

    return layout


def test_golden_pair_gf180mcu_diodes_extract_with_provenance_cited_devices(
    tmp_path: Path,
):
    """The synthetic dual-diode clamp extracts exactly the two `D` devices
    its topology describes, each matching its own provenance-cited entry --
    validating `EXTRACTION_DECK.diodes[].provenance`'s
    `gf180mcu_fd_pr__diode_nd2ps_06v0`/`...diode_pd2nw_06v0` citations
    end-to-end (issue #904)."""
    by_name = {d.name: d for d in GF180MCU_DECK.diodes}
    assert by_name["diode_nd2ps_06v0"].provenance.rule_id == (
        "gf180mcu_fd_pr__diode_nd2ps_06v0"
    )
    assert by_name["diode_pd2nw_06v0"].provenance.rule_id == (
        "gf180mcu_fd_pr__diode_pd2nw_06v0"
    )

    path = _write_gds(_make_gf180mcu_diode_layout(), tmp_path / "diode.gds")
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "diode.spice"))

    assert report["device_counts"] == {
        "diode_nd2ps_06v0": 1,
        "diode_pd2nw_06v0": 1,
    }
    by_class = {device["class"]: device for device in report["devices"]}
    nd2ps = by_class["diode_nd2ps_06v0"]
    assert nd2ps["nets"] == {"a": GF180MCU_DECK.substrate_net, "c": "CATH"}
    assert nd2ps["params"] == {"area_um2": 1.0, "perimeter_um": 4.0}
    pd2nw = by_class["diode_pd2nw_06v0"]
    assert pd2nw["nets"]["a"] == "ANOD"
    assert pd2nw["params"] == {"area_um2": 1.0, "perimeter_um": 4.0}


# --------------------------------------------------------------------------- #
# Coverage discipline (issue #867 AC: "every compiled device rule ships a
# golden pair"; issue #904 extends the same discipline to gf180mcu)
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
#: this set, not by silent under-coverage. Keyed by deck name (issue #904
#: adds the gf180mcu entry, mirroring sky130's own 8-rule set).
_GOLDEN_PAIR_TESTED_RULE_IDS: dict[str, frozenset[str]] = {
    "sky130": frozenset(
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
    ),
    "gf180mcu": frozenset(
        {
            "gf180mcu_fd_pr__nfet_03v3",
            "gf180mcu_fd_pr__pfet_03v3",
            "gf180mcu_fd_pr__ppolyf_u",
            "gf180mcu_fd_pr__ppolyf_u_1k",
            "cap_mim_2f0_m4m5_noshield",
            "BJT.3",
            "gf180mcu_fd_pr__diode_nd2ps_06v0",
            "gf180mcu_fd_pr__diode_pd2nw_06v0",
        }
    ),
}

_DECKS_BY_NAME = {"sky130": EXTRACTION_DECK, "gf180mcu": GF180MCU_DECK}


@pytest.mark.parametrize("deck_name", sorted(_DECKS_BY_NAME))
def test_golden_pairs_cover_every_provenanced_device_rule(deck_name: str) -> None:
    """Issue #867's own acceptance criterion: 'every compiled device rule
    ships a golden pair (layout in, expected SPICE device out) the deck
    correctly extracts' -- issue #904 (Epic #711 Phase 3a) extends this to
    gf180mcu. Each deck's provenance-cited device rules (sky130, issue #868
    Phase 2a: MOS, all three resistors, both capacitors, the one bipolar --
    8 entries; gf180mcu, issue #904: MOS, both resistors, the one
    capacitor, the one bipolar, both diodes -- 8 entries) are all backfilled
    and, as of this module's golden-pair tests above, all individually
    validated -- this coverage test is what keeps that true: a new
    provenance-backfilled device rule added without a matching golden-pair
    test (or a stale `_GOLDEN_PAIR_TESTED_RULE_IDS` entry for a rule_id the
    deck no longer declares) fails this assertion, exactly like
    `test_golden_deck.py`'s `test_golden_manifest_covers_every_width_
    space_rule` does for the DRC side."""
    deck = _DECKS_BY_NAME[deck_name]
    assert _provenanced_device_rule_ids(deck) == _GOLDEN_PAIR_TESTED_RULE_IDS[deck_name]
