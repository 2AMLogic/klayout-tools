"""Cross-check the compiled sky130 LVS device-extraction rules
(``EXTRACTION_DECK`` in ``src/klayout_tools/decks/sky130.py``, issue #868/
#867) against the real, hand-written upstream deck they were transcribed
from -- issue #869, Epic #711 Phase 2c: "Cross-check compiled sky130 LVS
device rules against the hand-written deck on the #520 corpus."

## Precedent and scoping (mirrors the DRC phase's own cross-check, #747)

Phase 1 (#747) resolved "cross-check against the hand-written deck" not by
finding a separate hand-authored *Python* DRC deck, but by running KLayout's
own native rule-deck runner (``run_drc_klayout_engine``, issue #565) against
the real upstream ``sky130A.lydrc`` script and comparing its violations
against the compiled ``DrcRule`` deck's own results on the same golden
layouts (see ``tests/test_golden_deck.py``'s tier 3 and ``docs/cli/drc.md``'s
"Engine" -> "klayout" section).

This module is the LVS-device-extraction counterpart. Unlike the DRC side,
sky130 ships its native LVS/device-extraction deck as a single,
directly-runnable ``libs.tech/klayout/lvs/sky130.lvs`` (verified against a
real ``volare``-fetched sky130A/sky130B install for this issue --
:func:`klayout_tools.pdk.lvs_deck_file` resolves it), so a real native-deck
oracle *is* available here, unlike gf180mcu's fragments-only DRC deck. See
:func:`klayout_tools.extract.run_extract_klayout_engine`'s own docstring for
exactly how this module drives that script to get a comparable extracted
netlist out of it (it is not really an "LVS" run in the compare sense --
there is no independent reference schematic to compare against here, only a
synthesized empty stub to satisfy the script's own hard requirement for one;
this module cross-checks *extraction*, not *comparison*).

## The "#520 corpus"

Issue #520 (Tiny Tapeout) is an open epic with no vendored corpus anywhere in
this repo (verified: neither a `tt_corpus`-shaped fixture directory nor any
vendored GDS exists). Following the same convention Phase 1's own golden-pair
manifest (`tests/golden_deck/`) and Phase 2a/2b's own golden layout->netlist
pairs (`tests/test_lvs_device_provenance.py`) already established for this
kind of cross-check, this module reuses (and, where the native deck's own
device-recognition contract requires geometry the existing golden pairs did
not draw, extends) that same synthesized-golden-layout convention as the
corpus stand-in -- one layout per device rule
``EXTRACTION_DECK`` declares a ``RuleProvenance`` citation for (the same
8-rule set `tests/test_lvs_device_provenance.py` already validates against
the compiled deck in isolation).

## Results (verified 2026-08-12 against a real, volare-fetched sky130A
install, ``open_pdks c6d73a35f524070e85faff4a6a9eef49553ebc2b`` -- the same
commit `sky130.py`'s own provenance notes cite -- and a real KLayout 0.28.16
binary)

**7 of 8 provenanced device rules cross-checked; 4 agree exactly, 3 disagree
for a documented, already-known reason; 1 deferred (investigated, not
executed -- see below).**

- `nfet`/`pfet`: **exact** (L, W). Straightforward MOS4 recognition; native
  needs an explicit `nsdm`/`psdm` implant layer this deck doesn't model, but
  is otherwise identical. See `test_pfet_l_w_matches_native_deck`.
- `res_generic_po`: **exact** (R). Plain sheet-rho x squares, no refinement
  either side.
- `res_xhigh_po`: **exact** (R), at native's fixed 0.35um SKU width. See
  `test_res_xhigh_po_r_matches_native_deck_at_native_width`.
- `res_high_po`: **documented disagreement**. This deck's
  `sheet_rho_ohm_sq`/`fixed_offset_ohm` (issue #518) is a deliberately
  refined two-term fit vs. `sky130.lvs`'s own raw single-term coefficient.
  See
  `test_res_high_po_r_disagrees_with_native_deck_by_a_documented_refinement`.
- `cap_mim`/`cap_mim_m4`: **documented disagreement**. This deck's
  `perim_cap_f_um` (issue #512) adds a perimeter term `sky130.lvs`'s own
  built-in `capacitor(...)` primitive never had. See
  `test_cap_mim_c_disagrees_with_native_deck_by_a_documented_refinement`.
- `pnp`: **deferred**. Native's device selection needs an exact-area/
  exact-edge-length three-terminal geometry not constructed within this
  issue's scope. See `test_pnp_native_cross_check_deferred`.

Every non-exact-agreement rule's disagreement is a *deliberate, previously
documented* refinement this deck already carries a code-comment citation
for (issue #518, #512) -- discovered independently by this cross-check, not
introduced by it. No *undocumented* disagreement was found on the 7 rules
actually run.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools import pdk
from klayout_tools.decks.sky130 import EXTRACTION_DECK
from klayout_tools.extract import ExtractError, run_extract, run_extract_klayout_engine

_DBU_UM = 0.001

# --------------------------------------------------------------------------- #
# Availability gate -- mirrors `tests/test_golden_deck.py`'s own
# `HAVE_KLAYOUT_BINARY`/`_SKIP_NO_SKY130_CROSS_CHECK` pattern, resolving
# `pdk.lvs_deck_file` instead of `pdk.drc_deck_file`.
# --------------------------------------------------------------------------- #

HAVE_KLAYOUT_BINARY = shutil.which("klayout") is not None


def _resolve_sky130_native_lvs_deck_file() -> str | None:
    """The real ``sky130.lvs`` path, or ``None`` if no ``klayout`` binary or
    no real sky130A install resolves -- never raises, so importing this
    module never fails in an environment with neither."""
    if not HAVE_KLAYOUT_BINARY:
        return None
    try:
        return pdk.lvs_deck_file(variant="sky130A")
    except pdk.PdkNotFoundError:
        return None


_SKY130_NATIVE_LVS_DECK_FILE = _resolve_sky130_native_lvs_deck_file()
_SKIP_NO_SKY130_LVS_CROSS_CHECK = pytest.mark.skipif(
    _SKY130_NATIVE_LVS_DECK_FILE is None,
    reason=(
        "no klayout binary and/or no real sky130A install found "
        "(klayout_tools.pdk) -- the native-deck LVS cross-check needs both"
    ),
)


def _box_um(x0: float, y0: float, x1: float, y1: float) -> kdb.Box:
    return kdb.Box(
        round(x0 / _DBU_UM),
        round(y0 / _DBU_UM),
        round(x1 / _DBU_UM),
        round(y1 / _DBU_UM),
    )


def _write_gds(layout: kdb.Layout, path: Path) -> str:
    layout.write(str(path))
    return str(path)


def _run_native(path: str) -> dict:
    assert _SKY130_NATIVE_LVS_DECK_FILE is not None  # guarded by the skipif above
    try:
        return run_extract_klayout_engine(
            path, _SKY130_NATIVE_LVS_DECK_FILE, timeout_s=120.0
        )
    except ExtractError as exc:  # pragma: no cover - environment-dependent
        pytest.fail(f"native sky130.lvs extraction failed: {exc}")


def _one_native_device(report: dict) -> dict:
    assert report["device_count"] == 1, (
        f"expected exactly one device from the native deck, got "
        f"{report['device_counts']}"
    )
    return report["devices"][0]


# --------------------------------------------------------------------------- #
# MOSFET (exact agreement, modulo an implant-layer recognition gap noted in
# each test's own docstring)
# --------------------------------------------------------------------------- #


def _make_nfet_layout() -> kdb.Layout:
    """`tests/test_lvs_device_provenance.py`'s own `_make_nfet_layout`
    geometry, plus an `nsdm.drawing` (93/44) N+ implant box covering the
    active area -- required for the *native* deck to recognise the device at
    all (`nsd = nsdm.and(sd).not(nwell).interacting(ngate)`, verified against
    a real `sky130.lvs`); the compiled deck does not model this implant
    layer at all (a documented simplification -- see
    `ExtractionDeck.active`'s own docstring), so it recognises the identical
    geometry with or without it."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(93, 44, _box_um(-0.2, -0.2, 2.2, 1.2))  # nsdm.drawing (N+ implant)
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


@_SKIP_NO_SKY130_LVS_CROSS_CHECK
def test_nfet_l_w_matches_native_deck(tmp_path: Path) -> None:
    """A drawn 0.4um-gate / 1um-active-width NMOS extracts with identical
    `L`/`W` under both engines -- the compiled deck's `nfet` recognition
    (`EXTRACTION_DECK.nfet_provenance`, `sky130_fd_pr__nfet_01v8`) agrees
    exactly with the real `sky130.lvs`'s own `mos4(...)` extraction."""
    assert EXTRACTION_DECK.nfet_provenance.rule_id == "sky130_fd_pr__nfet_01v8"

    path = _write_gds(_make_nfet_layout(), tmp_path / "nfet.gds")

    compiled = run_extract(path, "sky130", output=str(tmp_path / "nfet.spice"))
    assert compiled["device_counts"] == {"nfet": 1}
    compiled_device = compiled["devices"][0]

    native_device = _one_native_device(_run_native(path))
    assert native_device["class"].lower() == "sky130_fd_pr__nfet_01v8"
    assert native_device["params"]["L"] == pytest.approx(
        compiled_device["params"]["l_um"]
    )
    assert native_device["params"]["W"] == pytest.approx(
        compiled_device["params"]["w_um"]
    )


def _make_pfet_layout() -> kdb.Layout:
    """`_make_nfet_layout`'s geometry wrapped in `nwell.drawing` plus a
    `psdm.drawing` (94/20) P+ implant box -- the native deck's PMOS
    counterpart of `_make_nfet_layout`'s `nsdm` addition
    (`psd = psdm.and(sd).and(nwell).interacting(pgate)`, verified against a
    real `sky130.lvs`)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(64, 20, _box_um(-1, -1, 3, 2))  # nwell.drawing
    draw(94, 20, _box_um(-1.2, -1.2, 3.2, 2.2))  # psdm.drawing (P+ implant)
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


@_SKIP_NO_SKY130_LVS_CROSS_CHECK
def test_pfet_l_w_matches_native_deck(tmp_path: Path) -> None:
    """The `nfet` cross-check's PMOS sibling (`EXTRACTION_DECK.pfet_provenance`,
    `sky130_fd_pr__pfet_01v8`)."""
    assert EXTRACTION_DECK.pfet_provenance.rule_id == "sky130_fd_pr__pfet_01v8"

    path = _write_gds(_make_pfet_layout(), tmp_path / "pfet.gds")

    compiled = run_extract(path, "sky130", output=str(tmp_path / "pfet.spice"))
    assert compiled["device_counts"] == {"pfet": 1}
    compiled_device = compiled["devices"][0]

    native_device = _one_native_device(_run_native(path))
    assert native_device["class"].lower() == "sky130_fd_pr__pfet_01v8"
    assert native_device["params"]["L"] == pytest.approx(
        compiled_device["params"]["l_um"]
    )
    assert native_device["params"]["W"] == pytest.approx(
        compiled_device["params"]["w_um"]
    )


# --------------------------------------------------------------------------- #
# Resistors
# --------------------------------------------------------------------------- #


def _make_resistor_layout(
    width_um: float, extra_layers: tuple[tuple[int, int], ...] = ()
) -> kdb.Layout:
    """A poly bar of `width_um` with a 6um-long `poly.res`-marked segment,
    contacted at both ends -- `tests/test_lvs_device_provenance.py`'s
    `_make_resistor_layout`/`_make_precision_resistor_layout` geometry,
    generalised to a caller-chosen width (the native deck's
    `res_high_po`/`res_xhigh_po` recognise only five *exact* fixed widths --
    see `test_res_high_po_r_disagrees_with_native_deck_by_a_documented_
    refinement`'s docstring)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(66, 20, _box_um(0, 0, 12, width_um))  # poly.drawing bar
    draw(66, 13, _box_um(3, 0, 9, width_um))  # poly.res marker, 6um segment
    for layer, datatype in extra_layers:
        draw(layer, datatype, _box_um(2.8, -0.2, 9.2, width_um + 0.2))

    draw(
        66, 44, _box_um(0.1, width_um / 2 - 0.2, 0.3, width_um / 2 + 0.2)
    )  # licon1 head A
    draw(
        66, 44, _box_um(11.7, width_um / 2 - 0.2, 11.9, width_um / 2 + 0.2)
    )  # licon1 head B
    draw(
        67, 20, _box_um(0.0, width_um / 2 - 0.3, 0.4, width_um / 2 + 0.3)
    )  # li1 head A
    draw(
        67, 20, _box_um(11.6, width_um / 2 - 0.3, 12.0, width_um / 2 + 0.3)
    )  # li1 head B
    label(67, 5, "RA", 0.2, width_um / 2)
    label(67, 5, "RB", 11.8, width_um / 2)

    return layout


@_SKIP_NO_SKY130_LVS_CROSS_CHECK
def test_res_generic_po_r_matches_native_deck(tmp_path: Path) -> None:
    """A drawn 6-square (`L=6um`/`W=1um`) poly resistor extracts with
    identical `R` under both engines: neither side applies any refinement
    to `res_generic_po` (unlike `res_high_po` below), so `R = squares *
    sheet_rho_ohm_sq` agrees exactly."""
    resistor = next(r for r in EXTRACTION_DECK.resistors if r.name == "res_generic_po")
    assert resistor.provenance.rule_id == "sky130_fd_pr__res_generic_po"

    path = _write_gds(_make_resistor_layout(1.0), tmp_path / "res.gds")

    compiled = run_extract(path, "sky130", output=str(tmp_path / "res.spice"))
    assert compiled["device_counts"] == {"res_generic_po": 1}
    compiled_device = compiled["devices"][0]

    native_device = _one_native_device(_run_native(path))
    assert native_device["class"].lower() == "sky130_fd_pr__res_generic_po"
    assert native_device["params"]["R"] == pytest.approx(
        compiled_device["params"]["r_ohm"]
    )


@_SKIP_NO_SKY130_LVS_CROSS_CHECK
def test_res_xhigh_po_r_matches_native_deck_at_native_width(tmp_path: Path) -> None:
    """The real `sky130.lvs` recognises `res_xhigh_po` as five *discrete*
    fixed-width SKUs (`poly_xhigh_0p35`/`_0p69`/`_1p41`/`_2p85`/`_5p73`,
    each gated on an *exact* drawn width via
    `poly_res_2k.interacting(poly_res_2k.edges.with_length(w-0.01um,
    w+0.01um))`, verified against a real `sky130.lvs`) -- a materially
    stricter recognition than this deck's own generic-width `res_xhigh_po`
    (any width, `psdm`+`urpm` marked). Drawing the bar at exactly the
    narrowest native SKU width (0.35um) lets both engines recognise the same
    device, and (unlike `res_high_po`) their `R` values agree exactly: this
    deck's `sheet_rho_ohm_sq=2000.0` is the *same, unrefined* coefficient
    `sky130.lvs`'s own `extract_devices(resistor_with_bulk(
    "sky130_fd_pr__res_xhigh_po_0p35", 2000, ...)` call uses (no
    `fixed_offset_ohm` needed on either side, unlike `res_high_po`'s `319.8`
    -> `324.827244` refinement, issue #518)."""
    resistor = next(r for r in EXTRACTION_DECK.resistors if r.name == "res_xhigh_po")
    assert resistor.provenance.rule_id == "sky130_fd_pr__res_xhigh_po_0p35"
    assert resistor.fixed_offset_ohm == 0.0

    path = _write_gds(
        _make_resistor_layout(0.35, extra_layers=((94, 20), (79, 20))),  # psdm, urpm
        tmp_path / "res_xhigh.gds",
    )

    compiled = run_extract(path, "sky130", output=str(tmp_path / "res_xhigh.spice"))
    assert compiled["device_counts"] == {"res_xhigh_po": 1}
    compiled_device = compiled["devices"][0]

    native_device = _one_native_device(_run_native(path))
    assert native_device["class"].lower() == "sky130_fd_pr__res_xhigh_po_0p35"
    assert native_device["params"]["R"] == pytest.approx(
        compiled_device["params"]["r_ohm"], rel=1e-6
    )


@_SKIP_NO_SKY130_LVS_CROSS_CHECK
def test_res_high_po_r_disagrees_with_native_deck_by_a_documented_refinement(
    tmp_path: Path,
) -> None:
    """Same fixed-width-SKU recognition gap as `res_xhigh_po` above (drawn
    at the narrowest native SKU width, 0.35um, with `psdm`+`rpm`), but here
    the two engines' `R` values **genuinely, and expectedly, disagree**: this
    deck's `res_high_po` (`sheet_rho_ohm_sq=324.827244`,
    `fixed_offset_ohm=379.705147`) is a *deliberately refined* two-term fit
    against the real SPICE model, measured for issue #518 specifically
    because `sky130.lvs`'s own raw, single-term `319.8` coefficient (the
    `extract_devices(resistor_with_bulk("sky130_fd_pr__res_high_po_0p35",
    319.8, ...)` call this test's own native run reproduces exactly, `R =
    6/0.35 * 319.8 = 5482.29`, no offset) under/over-estimates `R` at real
    device lengths. This is exactly the "documented, reviewed approximation"
    class of disagreement `tests/golden_deck/README.md`'s "Cross-check
    results" section established the vocabulary for on the DRC side -- not
    an unexplained failure, and not something to "fix" (there is nothing
    wrong with either coefficient; they answer different questions: raw LVS
    transcription vs. measured-accurate model)."""
    resistor = next(r for r in EXTRACTION_DECK.resistors if r.name == "res_high_po")
    assert resistor.provenance.rule_id == "sky130_fd_pr__res_high_po_0p35"
    assert resistor.sheet_rho_ohm_sq == pytest.approx(324.827244)
    assert resistor.fixed_offset_ohm == pytest.approx(379.705147)

    path = _write_gds(
        _make_resistor_layout(0.35, extra_layers=((94, 20), (86, 20))),  # psdm, rpm
        tmp_path / "res_high.gds",
    )

    compiled = run_extract(path, "sky130", output=str(tmp_path / "res_high.spice"))
    assert compiled["device_counts"] == {"res_high_po": 1}
    compiled_r_ohm = compiled["devices"][0]["params"]["r_ohm"]

    native_device = _one_native_device(_run_native(path))
    assert native_device["class"].lower() == "sky130_fd_pr__res_high_po_0p35"
    native_r_ohm = native_device["params"]["R"]

    # The raw, unrefined native coefficient this deck's own #518 refinement
    # improved on -- asserted present, not merely tolerated (mirrors
    # `test_golden_deck.py`'s own `expected_disagreement` discipline: a
    # documented approximation is *proven* to disagree, not silently
    # skipped).
    raw_native_r_ohm = (6.0 / 0.35) * 319.8
    assert native_r_ohm == pytest.approx(raw_native_r_ohm, rel=1e-6)
    assert compiled_r_ohm != pytest.approx(native_r_ohm, rel=0.01)


# --------------------------------------------------------------------------- #
# MiM capacitors
# --------------------------------------------------------------------------- #


def _make_cap_mim_layout(
    top_plate: tuple[int, int], bottom_plate: tuple[int, int]
) -> kdb.Layout:
    """`tests/test_lvs_device_provenance.py`'s own MiM-cap golden layout
    geometry (10x5um top plate over a large bottom-plate square) -- unlike
    the MOSFET/resistor fixtures above, no additional implant layer is
    needed for the native deck to recognise this device (verified against a
    real `sky130.lvs`: `capm`/`capm2`/`met3_con`/`met4_con` derive directly
    from the same plain drawn layers this deck's own `CapacitorDevice`
    reads)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    draw(*bottom_plate, _box_um(-20, -20, 20, 20))
    draw(*top_plate, _box_um(0, 0, 10, 5))
    return layout


@_SKIP_NO_SKY130_LVS_CROSS_CHECK
def test_cap_mim_c_disagrees_with_native_deck_by_a_documented_refinement(
    tmp_path: Path,
) -> None:
    """A drawn 10x5um (`area=50um^2`, `perimeter=30um`) `capm`-over-`met3`
    MiM cap **genuinely, and expectedly, disagrees** in total `C` between
    the two engines: `sky130.lvs`'s own `capacitor(...)` LVS-DSL primitive
    (a built-in KLayout extractor helper, not custom code in the script)
    computes `C = area * specific_cap` only -- no perimeter term -- so its
    `extract_devices(capacitor("sky130_fd_pr__model__cap_mim", 2e-15,
    MIMCap), ...)` call yields exactly `C = 50 * 2e-15 = 1e-13` (verified
    against a real run: `A=50`, `P=30`, `C=1e-13`, i.e. `P` is *recorded* on
    the device but never folds into `C`). This deck's own
    `CapacitorDevice.perim_cap_f_um=1.9e-16` (issue #512, "previously
    unmodelled") is a deliberate refinement sourced from a *separate* SPICE
    corner-model file (`sky130_fd_pr__cap_mim_m3_1.model.spice`'s `cpmimc`),
    not present in `sky130.lvs`'s own inline extraction call at all -- so the
    compiled deck's total `C` is always somewhat larger than the native
    deck's for any real (non-zero-perimeter) geometry. The *area* term
    itself agrees exactly (`area_cap_f_um2=2.0e-15` matches the native
    call's own `2e-15` argument bit-for-bit) -- only the deliberately-added
    perimeter refinement diverges, the same "improves on the raw LVS
    transcription for a documented reason" shape as `res_high_po` above."""
    capacitor = next(
        c
        for c in EXTRACTION_DECK.capacitors
        if c.name == "sky130_fd_pr__model__cap_mim"
    )
    assert capacitor.provenance.rule_id == "sky130_fd_pr__model__cap_mim"
    assert capacitor.area_cap_f_um2 == pytest.approx(2.0e-15)
    assert capacitor.perim_cap_f_um == pytest.approx(1.9e-16)

    path = _write_gds(
        _make_cap_mim_layout(top_plate=(89, 44), bottom_plate=(70, 20)),
        tmp_path / "cap_mim.gds",
    )

    compiled = run_extract(path, "sky130", output=str(tmp_path / "cap_mim.spice"))
    assert compiled["device_counts"] == {"sky130_fd_pr__model__cap_mim": 1}
    compiled_device = compiled["devices"][0]
    area_um2 = 50.0
    perimeter_um = 30.0
    assert compiled_device["params"]["area_um2"] == pytest.approx(area_um2)
    assert compiled_device["params"]["perimeter_um"] == pytest.approx(perimeter_um)

    native_device = _one_native_device(_run_native(path))
    assert native_device["class"].lower() == "sky130_fd_pr__model__cap_mim"
    assert native_device["params"]["A"] == pytest.approx(area_um2)
    assert native_device["params"]["P"] == pytest.approx(perimeter_um)

    # The native deck's area-only C -- asserted present, not merely
    # tolerated.
    native_c_f = native_device["params"]["C"]
    assert native_c_f == pytest.approx(area_um2 * capacitor.area_cap_f_um2, rel=1e-6)

    compiled_c_f = compiled_device["params"]["c_f"]
    expected_compiled_c_f = (
        area_um2 * capacitor.area_cap_f_um2 + perimeter_um * capacitor.perim_cap_f_um
    )
    assert compiled_c_f == pytest.approx(expected_compiled_c_f)
    assert compiled_c_f != pytest.approx(native_c_f, rel=0.01, abs=0.0)


@_SKIP_NO_SKY130_LVS_CROSS_CHECK
def test_cap_mim_m4_c_disagrees_with_native_deck_by_a_documented_refinement(
    tmp_path: Path,
) -> None:
    """`test_cap_mim_c_disagrees_with_native_deck_by_a_documented_
    refinement`'s sibling for sky130's second independent MiM stack
    (`capm2.drawing` top plate over `met4.drawing`, both stacks sharing the
    identical `area_cap_f_um2`/`perim_cap_f_um` coefficients per
    `decks/sky130.py`'s own provenance note) -- same area-only-vs-area+
    perimeter disagreement shape, different plates."""
    capacitor = next(
        c
        for c in EXTRACTION_DECK.capacitors
        if c.name == "sky130_fd_pr__model__cap_mim_m4"
    )
    assert capacitor.provenance.rule_id == "sky130_fd_pr__model__cap_mim_m4"

    path = _write_gds(
        _make_cap_mim_layout(top_plate=(97, 44), bottom_plate=(71, 20)),
        tmp_path / "cap_mim_m4.gds",
    )

    compiled = run_extract(path, "sky130", output=str(tmp_path / "cap_mim_m4.spice"))
    assert compiled["device_counts"] == {"sky130_fd_pr__model__cap_mim_m4": 1}
    compiled_c_f = compiled["devices"][0]["params"]["c_f"]

    native_device = _one_native_device(_run_native(path))
    assert native_device["class"].lower() == "sky130_fd_pr__model__cap_mim_m4"
    native_c_f = native_device["params"]["C"]

    assert native_c_f == pytest.approx(50.0 * capacitor.area_cap_f_um2, rel=1e-6)
    assert compiled_c_f != pytest.approx(native_c_f, rel=0.01, abs=0.0)


# --------------------------------------------------------------------------- #
# Bipolar (pnp) -- investigated, deferred (not executed)
# --------------------------------------------------------------------------- #


def test_pnp_native_cross_check_deferred() -> None:
    """`EXTRACTION_DECK.bipolars[0]` (`pnp`, provenance
    `sky130_fd_pr__pnp_05v5_W0p68L0p68`) is **not** cross-checked against
    the native deck by this module -- an investigated, documented deferral,
    not an oversight (issue #869's own acceptance criteria explicitly allow
    this: "a well-reasoned, explicitly-documented scoping decision").

    Verified against a real `sky130.lvs` (grep for `pnp_5v0_0p68x0p68_e`):
    the native device-selection for *this specific* vertical-PNP flavour
    requires an emitter shape whose drawn area falls in `[0.3364, 0.6084]
    um^2` *and* whose edges are `[0.58, 0.78]um` long (i.e. drawn as close
    to an exact 0.68x0.68um square as this repo's box-only golden-layout
    convention can realistically hit) -- unlike the MOSFET/resistor cases
    above, this is not a "draw at one specific magic width" adjustment but a
    combined area-*and*-edge-length gate on a genuinely different terminal
    shape (`pnp_b`/`pnp_c` additionally require a separately-shaped
    `nsdm`-covered n-tap base tie and a `psdm`-covered p-tap collector tie,
    both individually gated `not_interacting(poly)` and
    `interacting(pnpid)`/`inside(pnpid)`) -- constructing that geometry
    correctly (and confirming it does not also, coincidentally, trip one of
    `sky130.lvs`'s *other* four `bjt3(...)` device classes nearby) is a
    materially larger, bespoke geometry-construction effort than this
    issue's other seven rules needed, and was not completed within this
    issue's scope. `EXTRACTION_DECK`'s own compiled `pnp` recognition
    (`tests/test_lvs_device_provenance.py`'s
    `test_golden_pair_sky130_pnp_extracts_with_provenance_cited_device`)
    remains covered in isolation; only the second-source cross-check is
    deferred. Tracked as a candidate follow-on, not silently dropped --
    mirrors how gf180mcu's own `--engine klayout` DRC cross-check is
    deferred in `docs/cli/drc.md`."""
    (pnp,) = EXTRACTION_DECK.bipolars
    assert pnp.provenance is not None
    assert pnp.provenance.rule_id == "sky130_fd_pr__pnp_05v5_W0p68L0p68"


# --------------------------------------------------------------------------- #
# Coverage discipline -- mirrors `test_lvs_device_provenance.py`'s own
# `test_golden_pairs_cover_every_provenanced_sky130_device_rule` /
# `test_golden_deck.py`'s `test_golden_manifest_covers_every_width_space_
# rule`: every provenanced sky130 device rule has a cross-check verdict in
# this module (run + agree, run + documented disagreement, or investigated +
# deferred) -- a future provenance-backfilled device rule with none of the
# three fails this assertion instead of silently under-covering.
# --------------------------------------------------------------------------- #

#: Upstream `rule_id`s this module reaches a verdict for, one way or another
#: -- hand-maintained, same discipline as
#: `test_lvs_device_provenance.py`'s `_GOLDEN_PAIR_TESTED_RULE_IDS`.
_CROSS_CHECK_VERDICT_RULE_IDS = frozenset(
    {
        "sky130_fd_pr__nfet_01v8",  # agrees
        "sky130_fd_pr__pfet_01v8",  # agrees
        "sky130_fd_pr__res_generic_po",  # agrees
        "sky130_fd_pr__res_xhigh_po_0p35",  # agrees (at native's fixed width)
        "sky130_fd_pr__res_high_po_0p35",  # documented disagreement (#518)
        "sky130_fd_pr__model__cap_mim",  # documented disagreement (#512)
        "sky130_fd_pr__model__cap_mim_m4",  # documented disagreement (#512)
        "sky130_fd_pr__pnp_05v5_W0p68L0p68",  # deferred (investigated)
    }
)


def _provenanced_device_rule_ids(deck) -> set[str]:
    """Mirrors `test_lvs_device_provenance.py`'s own helper of the same
    name (duplicated rather than imported to keep this module's cross-check
    self-contained, the same "some duplication is acceptable" tradeoff
    `tests/golden_deck/` and this repo's other golden-pair modules already
    make)."""
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


def test_cross_check_reaches_a_verdict_for_every_provenanced_sky130_device_rule() -> (
    None
):
    assert (
        _provenanced_device_rule_ids(EXTRACTION_DECK) == _CROSS_CHECK_VERDICT_RULE_IDS
    )
