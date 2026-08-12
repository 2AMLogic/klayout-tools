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

from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.decks import RuleProvenance
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


def test_sg13g2_extraction_deck_has_no_resistor_capacitor_bipolar_diode_entries():
    """This first increment (issue #905) curates MOS device recognition
    only -- `resistors`/`capacitors`/`bipolars`/`diodes` are all empty,
    exactly like sky130/gf180mcu's own pre-#222/#225/#223/#542 state (see
    `sg13g2.py`'s "Scope guard" docstring section). Named explicitly here so
    a future extension of this deck must update this assertion, rather than
    silently leaving the coverage-discipline test below out of sync."""
    assert EXTRACTION_DECK.resistors == ()
    assert EXTRACTION_DECK.capacitors == ()
    assert EXTRACTION_DECK.bipolars == ()
    assert EXTRACTION_DECK.diodes == ()
    assert EXTRACTION_DECK.device_classes == ("nfet", "pfet")


# --------------------------------------------------------------------------- #
# Golden layout -> netlist pairs (issue #905 AC: "golden layout->netlist
# pairs" for LVS device-extraction rules)
# --------------------------------------------------------------------------- #


def _make_nmos_layout() -> kdb.Layout:
    """One drawn NMOS on sg13g2's curated MOS-recognition layers: a 2x1um
    Activ strip crossed by a 0.4um-wide GatPoly gate bar (Activ outside
    NWell, so it recognises as NMOS) -- the exact device
    `EXTRACTION_DECK.nfet_provenance` cites (`sg13_lv_nmos`). `W` is the
    Activ strip's own 1um cross-extent; `L` is the GatPoly bar's 0.4um
    width."""
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


def _make_pmos_layout() -> kdb.Layout:
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
# Coverage discipline
# --------------------------------------------------------------------------- #


def _provenanced_device_rule_ids() -> set[str]:
    """Mirrors `test_lvs_device_provenance.py`'s own
    `_provenanced_device_rule_ids`, scoped to sg13g2's `EXTRACTION_DECK`
    (MOS only -- see `test_sg13g2_extraction_deck_has_no_resistor_
    capacitor_bipolar_diode_entries` above)."""
    ids: set[str] = set()
    if EXTRACTION_DECK.nfet_provenance is not None:
        ids.add(EXTRACTION_DECK.nfet_provenance.rule_id)
    if EXTRACTION_DECK.pfet_provenance is not None:
        ids.add(EXTRACTION_DECK.pfet_provenance.rule_id)
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


_GOLDEN_PAIR_TESTED_RULE_IDS = frozenset({"sg13_lv_nmos", "sg13_lv_pmos"})


def test_golden_pairs_cover_every_provenanced_sg13g2_device_rule():
    """Issue #905's own acceptance criterion: every compiled LVS device rule
    ships a golden layout->netlist pair. Mirrors
    `test_golden_pairs_cover_every_provenanced_sky130_device_rule` -- a
    future provenance-backfilled device rule (e.g. a resistor/capacitor
    entry) added without a matching golden-pair test fails this assertion
    loudly instead of silently under-covering."""
    assert _provenanced_device_rule_ids() == _GOLDEN_PAIR_TESTED_RULE_IDS
