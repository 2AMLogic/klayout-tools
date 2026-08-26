"""Tests for the sg13cmos5l (IHP-Open-PDK) MOS-only starter deck (issue
#1400, decomposed from #1398).

`src/klayout_tools/decks/sg13cmos5l.py`'s DRC width/space rules are already
exercised end-to-end (coverage, provenance-populated, curated-engine
agreement) by `tests/test_golden_deck.py`'s `DECK_NAMES`-parametrized tests
-- `sg13cmos5l` was added to that tuple by this issue, so those three tiers
already cover every rule in `sg13cmos5l.DECK` with no new test code needed
here.

This module covers the two things `test_golden_deck.py` does *not*, mirroring
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

See `src/klayout_tools/decks/sg13cmos5l.py`'s module docstring for this
deck's full provenance notes, scope (`width`/`space` DRC checks only, across
`Activ`/`GatPoly`/`Metal1`; one LV MOSFET LVS device class pair, plus the
three poly resistors), and what was deliberately left un-transcribed and why
(HV MOS flavour, metal resistors, diodes, capacitors, the Metal2-TopMetal1
stack, parasitics).
"""

from __future__ import annotations

from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.decks import RuleProvenance, get_deck
from klayout_tools.decks.sg13cmos5l import EXTRACTION_DECK
from klayout_tools.drc import run_drc
from klayout_tools.extract import run_extract

_DBU_UM = 0.001


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
    `Metal1`), no other check kind."""
    deck = get_deck("sg13cmos5l")
    assert len(deck) == 6
    assert {rule.check for rule in deck} == {"width", "space"}
    assert {rule.layer for rule in deck} == {(1, 0), (5, 0), (8, 0)}


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


def _make_nfet_layout() -> kdb.Layout:
    """One drawn NMOS on sg13cmos5l's curated MOS-recognition layers: a
    2x1um `Activ.drawing` strip crossed by a 0.4um-wide `GatPoly.drawing`
    gate bar (active outside `NWell.drawing`, so it recognises as NMOS) --
    the exact device `EXTRACTION_DECK.nfet_provenance` cites
    (`sg13_lv_nmos`). cmos5l's `Cont.drawing` (`6/0`) lands directly on
    `Metal1.drawing` (`8/0`), the same single-first-metal-level shape
    `sg13g2.py`'s own deck has. `W` is the active strip's own 1um
    cross-extent; `L` is the poly bar's 0.4um width."""
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


def _make_pfet_layout() -> kdb.Layout:
    """The `_make_nfet_layout` geometry with the same active/poly/contact
    stack, wrapped in `NWell.drawing` (active *inside* `NWell` recognises as
    PMOS, the deck's own NMOS/PMOS split) -- the exact device
    `EXTRACTION_DECK.pfet_provenance` cites (`sg13_lv_pmos`). A direct
    `NWell.pin` (`well_label`) text names the body/well net without needing
    a separate tap-contact stack, mirroring `sg13g2.py`'s own `well_label`
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
