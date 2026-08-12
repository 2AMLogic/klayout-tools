"""Tests for the SG13G2 (IHP-Open-PDK) starter deck (issue #905, Epic #711
Phase 3b -- "compile" an SG13G2 DRC/LVS deck, the second PDK-generality
proof after gf180mcu's Phase 3a).

`src/klayout_tools/decks/sg13g2.py`'s DRC width/space rules are already
exercised end-to-end (coverage, provenance-populated, curated-engine
agreement) by `tests/test_golden_deck.py`'s `DECK_NAMES`-parametrized tests
-- `sg13g2` was added to that tuple by this issue, so those three tiers
already cover every rule in `sg13g2.DECK` with no new test code needed here.

This module covers the two things `test_golden_deck.py` does *not*:

- **`klt drc --deck sg13g2` runs at all**, end to end through the CLI-facing
  `run_drc()` entry point (not just via the golden-pair manifest machinery).
- **LVS device-recognition provenance + a golden layout->netlist pair**,
  mirroring `tests/test_lvs_device_provenance.py`'s sky130 MOSFET tests --
  `EXTRACTION_DECK.nfet_provenance`/`pfet_provenance` cite a real
  `mos_extraction.lvs` device-class name, and a minimal drawn NMOS/PMOS
  extracts with the `l_um`/`w_um` its own geometry implies.

See `src/klayout_tools/decks/sg13g2.py`'s module docstring for this deck's
full provenance notes, scope ("width"/"space" DRC checks only, across
`Activ`/`GatPoly`/`Metal1`/`Metal2`; one MOSFET LVS device class), and what
was deliberately left un-transcribed and why.
"""

from __future__ import annotations

from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.decks import RuleProvenance, get_deck
from klayout_tools.decks.sg13g2 import EXTRACTION_DECK
from klayout_tools.drc import run_drc
from klayout_tools.extract import run_extract

_DBU_UM = 0.001


def _box_um(x0: float, y0: float, x1: float, y1: float) -> kdb.Box:
    """A `kdb.Box` from micrometre coordinates at sg13g2's own
    `NOMINAL_DBU_UM` convention (1 nm/unit, matching `sg13g2.lyt`'s own
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
# klt drc --deck sg13g2 (end to end through run_drc, not just the golden
# manifest machinery)
# --------------------------------------------------------------------------- #


def test_sg13g2_deck_registered_with_eight_width_space_rules():
    """`get_deck("sg13g2")` resolves (issue #905's registration in
    `decks/__init__.py`'s registries) and is exactly the curated
    width/space-only starter subset the module docstring describes -- 8
    rules across 4 layers (`Activ`, `GatPoly`, `Metal1`, `Metal2`), no other
    check kind."""
    deck = get_deck("sg13g2")
    assert len(deck) == 8
    assert {rule.check for rule in deck} == {"width", "space"}
    assert {rule.layer for rule in deck} == {(1, 0), (5, 0), (8, 0), (10, 0)}


def test_run_drc_sg13g2_metal1_width_violation(tmp_path: Path):
    """A 0.11um `Metal1.drawing` bar (below the 0.16um `M1.a` floor) trips
    `metal1.width.1` under `klt drc --deck sg13g2` end to end."""
    layout = kdb.Layout()
    layout.dbu = _DBU_UM
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(8, 0)).insert(_box_um(0, 0, 0.11, 4))
    path = _write_gds(layout, tmp_path / "m1_violate.gds")

    report = run_drc(path, "sg13g2")
    assert report["status"] == "violations"
    assert report["rule_counts"].get("metal1.width.1", 0) >= 1


def test_run_drc_sg13g2_metal1_clean(tmp_path: Path):
    """A 0.21um `Metal1.drawing` bar (above the 0.16um `M1.a` floor, and
    with no second shape to trip a space check) is fully clean under
    `klt drc --deck sg13g2`."""
    layout = kdb.Layout()
    layout.dbu = _DBU_UM
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(8, 0)).insert(_box_um(0, 0, 0.21, 4))
    path = _write_gds(layout, tmp_path / "m1_clean.gds")

    report = run_drc(path, "sg13g2")
    assert report["status"] == "clean"


# --------------------------------------------------------------------------- #
# LVS device-recognition provenance (issue #905 AC: "every compiled rule
# cites its SG13G2 PDK source line")
# --------------------------------------------------------------------------- #


def test_sg13g2_mos_provenance_cites_mos_extraction_lvs():
    """`EXTRACTION_DECK.nfet_provenance`/`pfet_provenance` cite the real
    `mos_extraction.lvs` `mos4('sg13_lv_nmos'/'sg13_lv_pmos')`
    `extract_devices` calls (verified against a real IHP-Open-PDK v0.3.0
    install fetched via `scripts/fetch-ihp-sg13g2.sh`)."""
    assert EXTRACTION_DECK.nfet_provenance == RuleProvenance(
        source_repo="IHP-GmbH/IHP-Open-PDK",
        source_path=(
            "ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks/mos_extraction.lvs"
        ),
        rule_id="sg13_lv_nmos",
        commit="5cccb161f7492697cfa52eb14dc03beb00bdca9e",
    )
    assert EXTRACTION_DECK.pfet_provenance == RuleProvenance(
        source_repo="IHP-GmbH/IHP-Open-PDK",
        source_path=(
            "ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks/mos_extraction.lvs"
        ),
        rule_id="sg13_lv_pmos",
        commit="5cccb161f7492697cfa52eb14dc03beb00bdca9e",
    )


# --------------------------------------------------------------------------- #
# Golden layout -> netlist pairs (issue #905 AC: "klt deck compiles SG13G2
# LVS device-extraction rules ... with golden layout->netlist pairs")
# --------------------------------------------------------------------------- #


def _make_nfet_layout() -> kdb.Layout:
    """One drawn NMOS on sg13g2's curated MOS-recognition layers: a 2x1um
    `Activ.drawing` strip crossed by a 0.4um-wide `GatPoly.drawing` gate bar
    (active outside `NWell.drawing`, so it recognises as NMOS) -- the exact
    device `EXTRACTION_DECK.nfet_provenance` cites (`sg13_lv_nmos`). Unlike
    sky130's `li1`-mediated contact stack, sg13g2's `Cont.drawing` (`6/0`)
    lands directly on `Metal1.drawing` (`8/0`) -- see the deck module's own
    "SG13G2 draws NMOS/PMOS device contacts ... landing directly on Metal1"
    docstring note. `W` is the active strip's own 1um cross-extent; `L` is
    the poly bar's 0.4um width."""
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
    label(8, 2, "S", 0.2, 0.5)  # Metal1.pin
    label(8, 2, "D", 1.8, 0.5)

    draw(6, 0, _box_um(0.9, 1.0, 1.1, 1.2))  # gate contact
    draw(8, 0, _box_um(0.85, 0.95, 1.15, 1.25))  # gate pad
    label(8, 2, "G", 1.0, 1.1)

    return layout


def test_golden_pair_sg13g2_nfet_l_w_matches_drawn_geometry(tmp_path: Path):
    """A drawn 0.4um-gate / 1um-active-width NMOS extracts with exactly
    those `l_um`/`w_um` -- validating `EXTRACTION_DECK.nfet_provenance`'s
    `sg13_lv_nmos` citation end-to-end: a golden layout, run through
    `run_extract`, reproduces the hand-computed netlist device parameters
    the drawn geometry implies."""
    path = _write_gds(_make_nfet_layout(), tmp_path / "nfet.gds")
    report = run_extract(path, "sg13g2", output=str(tmp_path / "nfet.spice"))

    assert report["device_counts"] == {"nfet": 1}
    (device,) = report["devices"]
    assert device["class"] == "nfet"
    assert device["params"]["l_um"] == pytest.approx(0.4)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert device["nets"]["b"] == EXTRACTION_DECK.substrate_net
    assert EXTRACTION_DECK.nfet_provenance.rule_id == "sg13_lv_nmos"


def _make_pfet_layout() -> kdb.Layout:
    """The `_make_nfet_layout` geometry with the same active/poly/contact
    stack, wrapped in `NWell.drawing` (active *inside* `NWell` recognises as
    PMOS, the deck's own NMOS/PMOS split -- see `ExtractionDeck.active`/
    `.poly`/`.nwell`'s own docstring) -- the exact device
    `EXTRACTION_DECK.pfet_provenance` cites (`sg13_lv_pmos`). A direct
    `NWell.pin` (`well_label`) text names the body/well net without needing
    a separate tap-contact stack, mirroring sky130's own `well_label`
    convention."""
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


def test_golden_pair_sg13g2_pfet_l_w_matches_drawn_geometry(tmp_path: Path):
    """A drawn 0.4um-gate / 1um-active-width PMOS (active wrapped in
    `NWell`) extracts with exactly those `l_um`/`w_um` -- validating
    `EXTRACTION_DECK.pfet_provenance`'s `sg13_lv_pmos` citation end-to-end,
    the PMOS sibling of
    `test_golden_pair_sg13g2_nfet_l_w_matches_drawn_geometry` above."""
    path = _write_gds(_make_pfet_layout(), tmp_path / "pfet.gds")
    report = run_extract(path, "sg13g2", output=str(tmp_path / "pfet.spice"))

    assert report["device_counts"] == {"pfet": 1}
    (device,) = report["devices"]
    assert device["class"] == "pfet"
    assert device["params"]["l_um"] == pytest.approx(0.4)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert device["nets"]["b"] == "VPB"
    assert EXTRACTION_DECK.pfet_provenance.rule_id == "sg13_lv_pmos"
