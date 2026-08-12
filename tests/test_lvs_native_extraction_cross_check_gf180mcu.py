"""Cross-check the compiled gf180mcu LVS device-extraction rules
(``EXTRACTION_DECK`` in ``src/klayout_tools/decks/gf180mcu.py``, issue #904)
against the real, hand-written upstream deck they were transcribed from --
Epic #711 Phase 3a's own AC4: "if an existing hand-written gf180 deck exists
in klt, the compiled deck's agreement with it is measured on a corpus and
reported (not asserted)."

## Precedent (mirrors #869's sky130 module exactly)

This module is the gf180mcu sibling of
``tests/test_lvs_native_extraction_cross_check.py`` (issue #869, Epic #711
Phase 2c) -- same infrastructure
(:func:`klayout_tools.extract.run_extract_klayout_engine`), same
"synthesized golden layout as corpus stand-in" convention (issue #520's own
Tiny Tapeout corpus is still unbuilt -- see that module's own "The #520
corpus" section), same reporting discipline (agree / documented
disagreement / investigated-and-deferred, never silently skipped).

**Unlike gf180mcu's own DRC-side native cross-check** (deferred in
``tests/test_golden_deck.py``/``docs/cli/drc.md`` because gf180mcu's native
*DRC* deck ships only as unassembled fragments with no single runnable
file), gf180mcu's native **LVS** deck *is* single-file and directly
runnable: a real, ``volare``-fetched ``gf180mcuD`` install ships
``libs.tech/klayout/lvs/gf180mcu.lvs`` (verified for this issue; resolved
via :func:`klayout_tools.pdk.lvs_deck_file`, whose own docstring already
flagged this exact possibility as of issue #869: "a future caller is free
to attempt it"). This module is that attempt.

## Results (verified against a real, volare-fetched gf180mcuD install and a
real KLayout binary)

**5 of 8 provenanced device rules cross-checked; 4 agree exactly, 1 agrees
with a documented refinement disagreement; 3 deferred (investigated, not
executed).**

- `gf180mcu_fd_pr__nfet_03v3` / `gf180mcu_fd_pr__pfet_03v3`: **exact** (L,
  W). The native deck's `mos4(...)` recognition needs an explicit
  `Nplus`/`Pplus` (32/0, 31/0) implant layer covering the active area that
  the compiled deck's own MOS recognition does not model (a documented
  simplification -- `EXTRACTION_DECK.active`'s own docstring) -- the exact
  same class of gap issue #869 found for sky130's `nsdm`/`psdm`. With that
  implant layer added, both flavours extract with identical `L`/`W`.
- `gf180mcu_fd_pr__ppolyf_u` / `gf180mcu_fd_pr__ppolyf_u_1k`: **exact**
  (`R`). Plain sheet-rho x squares on both sides, no refinement either way
  -- the native deck's default `$poly_res` (`'1k'`) matches the compiled
  deck's own default flavour exactly, so no `extra_rd` override is needed.
- `cap_mim_2f0_m4m5_noshield`: **documented disagreement**. The native
  deck's own `$metal_level` global defaults to `'6LM'`
  (`topmin1_metal` = `Metal5`), while `decks/gf180mcu.py`'s MiM capacitor
  models the 5LM variant (`Metal4` as the stack's bottom plate, per that
  module's own docstring) -- cross-checking needs
  `extra_rd={"metal_level": "5LM"}` (issue #904 adds this parameter to
  `run_extract_klayout_engine`) to point the native deck at the same stack.
  With that override, the native deck's raw single-term area coefficient
  (`2.0e-15` F/um^2) disagrees with the compiled deck's own refined
  two-term fit (`area_cap_f_um2=1.99e-15`, `perim_cap_f_um=2.383e-16`,
  issue #512's own perimeter-term refinement) -- the exact same class of
  disagreement issue #869 found for sky130's `cap_mim`/`cap_mim_m4`.
- `BJT.3` (the generic `bjt` bipolar) and both diodes
  (`gf180mcu_fd_pr__diode_nd2ps_06v0`, `...diode_pd2nw_06v0`): **deferred**
  -- see each deferral test's own docstring below for the specific
  investigated reason (an exact-geometry vertical-BJT device family for the
  bipolar, a topologically different terminal definition -- an `LVPWELL`/
  well-tap region the compiled deck's own simpler recognition does not draw
  at all -- for the diodes).

Every non-exact-agreement rule's disagreement/deferral is either a
*deliberate, previously documented* refinement this deck already carries a
code-comment citation for (issue #512, discovered independently by this
cross-check, not introduced by it), or a genuinely different native
device-recognition topology investigated and written up here, not silently
dropped -- mirroring #869's own reporting discipline exactly.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools import pdk
from klayout_tools.decks.gf180mcu import EXTRACTION_DECK
from klayout_tools.extract import ExtractError, run_extract, run_extract_klayout_engine

_DBU_UM = 0.001

# --------------------------------------------------------------------------- #
# Availability gate -- mirrors `test_lvs_native_extraction_cross_check.py`'s
# own `HAVE_KLAYOUT_BINARY`/`_SKIP_NO_SKY130_LVS_CROSS_CHECK` pattern,
# resolving the `gf180mcuD` variant instead of `sky130A`.
# --------------------------------------------------------------------------- #

HAVE_KLAYOUT_BINARY = shutil.which("klayout") is not None


def _resolve_gf180mcu_native_lvs_deck_file() -> str | None:
    """The real ``gf180mcu.lvs`` path, or ``None`` if no ``klayout`` binary
    or no real gf180mcuD install resolves -- never raises, so importing
    this module never fails in an environment with neither."""
    if not HAVE_KLAYOUT_BINARY:
        return None
    try:
        return pdk.lvs_deck_file(variant="gf180mcuD")
    except pdk.PdkNotFoundError:
        return None


_GF180MCU_NATIVE_LVS_DECK_FILE = _resolve_gf180mcu_native_lvs_deck_file()
_SKIP_NO_GF180MCU_LVS_CROSS_CHECK = pytest.mark.skipif(
    _GF180MCU_NATIVE_LVS_DECK_FILE is None,
    reason=(
        "no klayout binary and/or no real gf180mcuD install found "
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


def _run_native(path: str, extra_rd: dict[str, str] | None = None) -> dict:
    """``bare_length_area_units=False``: unlike ``sky130.lvs``'s custom
    SPICE writer delegate, ``gf180mcu.lvs`` uses KLayout's own built-in
    ``write_spice(...)``, which emits an explicit unit suffix on every
    length/area value (``L=0.4U``, not a bare ``L=0.4``) --
    `kdb.NetlistSpiceReader()` parses that suffix correctly on read-back, so
    the sky130-specific x1e6/x1e12 "undo" `run_extract_klayout_engine`
    applies by default would *mis*-scale gf180mcu's already-correct values
    (verified empirically for issue #904: a written `L=0.4U` round-trips as
    plain `0.4`, not `400000.0`) -- see that function's own docstring."""
    assert _GF180MCU_NATIVE_LVS_DECK_FILE is not None  # guarded by the skipif above
    try:
        return run_extract_klayout_engine(
            path,
            _GF180MCU_NATIVE_LVS_DECK_FILE,
            timeout_s=120.0,
            extra_rd=extra_rd,
            bare_length_area_units=False,
        )
    except ExtractError as exc:  # pragma: no cover - environment-dependent
        pytest.fail(f"native gf180mcu.lvs extraction failed: {exc}")


def _one_native_device(report: dict) -> dict:
    assert report["device_count"] == 1, (
        f"expected exactly one device from the native deck, got "
        f"{report['device_counts']}"
    )
    return report["devices"][0]


# --------------------------------------------------------------------------- #
# MOSFET (exact agreement, modulo an implant-layer recognition gap noted in
# each test's own docstring -- the gf180mcu counterpart of #869's own
# nsdm/psdm finding)
# --------------------------------------------------------------------------- #


def _make_nfet_layout() -> kdb.Layout:
    """`tests/test_lvs_device_provenance.py`'s own
    `_make_gf180mcu_nfet_layout` geometry, plus an `Nplus.drawing` (32/0) N+
    implant box covering the active area -- required for the *native* deck
    to recognise the device at all (`ngate = nplus.and(tgate)`, verified
    against a real `gf180mcu.lvs`); the compiled deck does not model this
    implant layer at all (a documented simplification -- see
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

    draw(32, 0, _box_um(-0.5, -0.5, 2.5, 1.5))  # Nplus (N+ implant)
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


@_SKIP_NO_GF180MCU_LVS_CROSS_CHECK
def test_nfet_l_w_matches_native_deck(tmp_path: Path) -> None:
    """A drawn 0.4um-gate / 1um-active-width NMOS extracts with identical
    `L`/`W` under both engines -- the compiled deck's `nfet` recognition
    (`EXTRACTION_DECK.nfet_provenance`, `gf180mcu_fd_pr__nfet_03v3`) agrees
    exactly with the real `gf180mcu.lvs`'s own `mos4(...)` extraction.
    Unlike sky130's `sky130.lvs` (whose own `mos4(...)` device-class
    strings already carry the full `sky130_fd_pr__...` prefix), gf180mcu's
    `mos4('nfet_03v3', ...)` call names the *bare* device -- the native
    deck's own extracted class comes back as plain `nfet_03v3`, without the
    `gf180mcu_fd_pr__` prefix `RuleProvenance.rule_id` records (verified
    against a real `gf180mcu.lvs`)."""
    assert EXTRACTION_DECK.nfet_provenance.rule_id == "gf180mcu_fd_pr__nfet_03v3"

    path = _write_gds(_make_nfet_layout(), tmp_path / "nfet.gds")

    compiled = run_extract(path, "gf180mcu", output=str(tmp_path / "nfet.spice"))
    assert compiled["device_counts"] == {"nfet": 1}
    compiled_device = compiled["devices"][0]

    native_device = _one_native_device(_run_native(path))
    assert native_device["class"].lower() == "nfet_03v3"
    assert native_device["params"]["L"] == pytest.approx(
        compiled_device["params"]["l_um"]
    )
    assert native_device["params"]["W"] == pytest.approx(
        compiled_device["params"]["w_um"]
    )


def _make_pfet_layout() -> kdb.Layout:
    """`_make_nfet_layout`'s identical geometry wrapped in `Nwell.drawing`
    plus a `Pplus.drawing` (31/0) P+ implant box -- the native deck's PMOS
    counterpart of `_make_nfet_layout`'s `Nplus` addition
    (`pgate = pplus.and(tgate)`, verified against a real `gf180mcu.lvs`)."""
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
    draw(31, 0, _box_um(-1.5, -1.5, 3.5, 2.5))  # Pplus (P+ implant)

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


@_SKIP_NO_GF180MCU_LVS_CROSS_CHECK
def test_pfet_l_w_matches_native_deck(tmp_path: Path) -> None:
    """`test_nfet_l_w_matches_native_deck`'s PMOS sibling
    (`EXTRACTION_DECK.pfet_provenance`, `gf180mcu_fd_pr__pfet_03v3`) --
    same bare-vs-prefixed device-class-name distinction noted there."""
    assert EXTRACTION_DECK.pfet_provenance.rule_id == "gf180mcu_fd_pr__pfet_03v3"

    path = _write_gds(_make_pfet_layout(), tmp_path / "pfet.gds")

    compiled = run_extract(path, "gf180mcu", output=str(tmp_path / "pfet.spice"))
    assert compiled["device_counts"] == {"pfet": 1}
    compiled_device = compiled["devices"][0]

    native_device = _one_native_device(_run_native(path))
    assert native_device["class"].lower() == "pfet_03v3"
    assert native_device["params"]["L"] == pytest.approx(
        compiled_device["params"]["l_um"]
    )
    assert native_device["params"]["W"] == pytest.approx(
        compiled_device["params"]["w_um"]
    )


# --------------------------------------------------------------------------- #
# Resistors (exact agreement, no `extra_rd` override needed -- the native
# deck's own `$poly_res` default already matches the compiled deck's own
# default flavour)
# --------------------------------------------------------------------------- #

_RES_SQUARES = 6.0  # 6um marked segment / 1um width


def _make_resistor_layout(extra_layers: tuple[tuple[int, int], ...]) -> kdb.Layout:
    """`tests/test_lvs_device_provenance.py`'s own
    `_make_gf180mcu_resistor_layout` geometry (a 12x1um `Poly2` bar with a
    6um-long `RES_MK`-marked segment, 6.0 squares), narrowed by
    `extra_layers` to a specific flavour."""
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


@_SKIP_NO_GF180MCU_LVS_CROSS_CHECK
def test_ppolyf_u_r_matches_native_deck(tmp_path: Path) -> None:
    """A 6-square `Poly2` bar narrowed by `Pplus`+`SAB` extracts as
    `ppolyf_u` with identical `R` under both engines -- the native deck's
    own `resistor_with_bulk('ppolyf_u', 350, ...)` coefficient (350
    ohm/square) matches `EXTRACTION_DECK`'s own provenance-cited
    `sheet_rho_ohm_sq=350.0` exactly."""
    resistor = next(r for r in EXTRACTION_DECK.resistors if r.name == "ppolyf_u")
    assert resistor.provenance.rule_id == "gf180mcu_fd_pr__ppolyf_u"

    layout = _make_resistor_layout(((31, 0), (49, 0)))  # Pplus, SAB
    path = _write_gds(layout, tmp_path / "ppolyf_u.gds")

    compiled = run_extract(path, "gf180mcu", output=str(tmp_path / "ppolyf_u.spice"))
    assert compiled["device_counts"] == {"ppolyf_u": 1}
    compiled_r_ohm = compiled["devices"][0]["params"]["r_ohm"]

    native_device = _one_native_device(_run_native(path))
    assert native_device["class"].lower() == "ppolyf_u"
    assert native_device["params"]["R"] == pytest.approx(compiled_r_ohm)
    assert native_device["params"]["R"] == pytest.approx(
        _RES_SQUARES * resistor.sheet_rho_ohm_sq
    )


@_SKIP_NO_GF180MCU_LVS_CROSS_CHECK
def test_ppolyf_u_1k_r_matches_native_deck(tmp_path: Path) -> None:
    """`test_ppolyf_u_r_matches_native_deck`'s high-sheet-rho sibling
    (`SAB`+`Resistor` narrowing instead of `Pplus`+`SAB`) -- the native
    deck's default `$poly_res` (`'1k'`) selects the same
    `resistor_with_bulk('ppolyf_u_1k', 1000, ...)` branch
    `EXTRACTION_DECK`'s own default `ppolyf_u_1k` flavour cites (`1000`
    ohm/square), so no `extra_rd` override is needed."""
    resistor = next(r for r in EXTRACTION_DECK.resistors if r.name == "ppolyf_u_1k")
    assert resistor.provenance.rule_id == "gf180mcu_fd_pr__ppolyf_u_1k"

    layout = _make_resistor_layout(((49, 0), (62, 0)))  # SAB, Resistor
    path = _write_gds(layout, tmp_path / "ppolyf_u_1k.gds")

    compiled = run_extract(path, "gf180mcu", output=str(tmp_path / "ppolyf_u_1k.spice"))
    assert compiled["device_counts"] == {"ppolyf_u_1k": 1}
    compiled_r_ohm = compiled["devices"][0]["params"]["r_ohm"]

    native_device = _one_native_device(_run_native(path))
    assert native_device["class"].lower() == "ppolyf_u_1k"
    assert native_device["params"]["R"] == pytest.approx(compiled_r_ohm)
    assert native_device["params"]["R"] == pytest.approx(
        _RES_SQUARES * resistor.sheet_rho_ohm_sq
    )


# --------------------------------------------------------------------------- #
# MiM capacitor -- documented disagreement (issue #512's own perimeter-term
# refinement), needs `extra_rd={"metal_level": "5LM"}` to point the native
# deck at the same stack the compiled deck models
# --------------------------------------------------------------------------- #


@_SKIP_NO_GF180MCU_LVS_CROSS_CHECK
def test_cap_mim_c_disagrees_with_native_deck_by_a_documented_refinement(
    tmp_path: Path,
) -> None:
    """A drawn 10x10um `FuseTop` top plate over a `Metal4` bottom plate
    (`decks/gf180mcu.py`'s modeled 5LM variant) extracts under both
    engines, but the two `C` values genuinely disagree: the native deck's
    raw single-term area coefficient (`2.0e-15` F/um^2, verified against a
    real `gf180mcu.lvs`: `capacitor('cap_mim_2f0_m4m5_noshield', 2.0e-15,
    ...)`) vs. the compiled deck's own refined two-term fit
    (`area_cap_f_um2=1.99e-15`, `perim_cap_f_um=2.383e-16`, issue #512) --
    the gf180mcu counterpart of issue #869's own sky130 `cap_mim`/
    `cap_mim_m4` finding. Needs `extra_rd={"metal_level": "5LM"}`: the
    native deck's own `$metal_level` global defaults to `'6LM'`
    (`topmin1_metal` = `Metal5`), which would not recognise a `Metal4`
    bottom plate as the MiM stack's `topmin1_metal` at all."""
    (capacitor,) = EXTRACTION_DECK.capacitors
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

    compiled = run_extract(path, "gf180mcu", output=str(tmp_path / "cap.spice"))
    assert compiled["device_counts"] == {"cap_mim_2f0_m4m5_noshield": 1}
    compiled_c_f = compiled["devices"][0]["params"]["c_f"]

    native_device = _one_native_device(
        _run_native(path, extra_rd={"metal_level": "5LM"})
    )
    assert native_device["class"].lower() == "cap_mim_2f0_m4m5_noshield"
    native_c_f = native_device["params"]["C"]

    area_um2 = 100.0
    assert native_c_f == pytest.approx(area_um2 * capacitor.area_cap_f_um2, rel=1e-6)
    assert compiled_c_f != pytest.approx(native_c_f, rel=0.01, abs=0.0)


# --------------------------------------------------------------------------- #
# Bipolar (bjt) and diodes -- investigated, deferred (not executed)
# --------------------------------------------------------------------------- #


def test_bjt_native_cross_check_deferred() -> None:
    """`EXTRACTION_DECK.bipolars[0]` (the generic `bjt` recognition,
    provenance `BJT.3`) is **not** cross-checked against the native deck by
    this module -- an investigated, documented deferral, not an oversight
    (mirrors issue #869's own `pnp` deferral for sky130, allowed by that
    issue's own acceptance criteria: "a well-reasoned, explicitly-documented
    scoping decision").

    Verified against a real `gf180mcu.lvs` (grep
    `libs.tech/klayout/lvs/rule_decks/bjt_derivations.lvs`): unlike the
    compiled deck's single, loosely-bounded generic `bjt` class (any
    `Nwell`+`DRC_BJT`+`Comp` emitter), the native deck recognises a family
    of *exact-geometry* vertical-BJT device classes gated on the emitter's
    drawn area **and** edge length simultaneously (e.g.
    `npn_10p00x10p00_e = vnpn_e.with_area(99.5.um, 100.5.um)
    .interacting(vnpn_e.edges.with_length(9.8.um, 10.2.um))` -- a drawn
    emitter within 0.5% of an exact 10x10um square, one of six discrete
    device-class sizes) -- structurally the same class of gap issue #869
    found for sky130's `pnp`, just with more device-class variants to
    choose among. Constructing (and confirming does not coincidentally trip
    a *different* one of the six size classes) a bespoke exact-size emitter
    fixture is a materially larger effort than this module's other five
    device rules needed, and was not completed within this issue's scope.
    `EXTRACTION_DECK`'s own compiled `bjt` recognition
    (`tests/test_lvs_device_provenance.py`'s
    `test_golden_pair_gf180mcu_bjt_extracts_with_provenance_cited_device`)
    remains covered in isolation; only the second-source cross-check is
    deferred."""
    (bjt,) = EXTRACTION_DECK.bipolars
    assert bjt.provenance is not None
    assert bjt.provenance.rule_id == "BJT.3"


def test_diode_nd2ps_06v0_native_cross_check_deferred() -> None:
    """`gf180mcu_fd_pr__diode_nd2ps_06v0` is **not** cross-checked against
    the native deck by this module -- investigated, deferred.

    Verified against a real `gf180mcu.lvs`
    (`libs.tech/klayout/lvs/rule_decks/diode_derivations.lvs`): the native
    deck's `diode_nd2ps_06v0` device is a topologically different structure
    than the compiled deck's -- its `'P'` terminal is
    `lvpwell_con` (the drawn `LVPWELL` layer, 204/0), not the general
    p-substrate net `EXTRACTION_DECK`'s own simpler `diode_nd2ps_06v0` entry
    ties its cathode-side anode to (`substrate_net`, see
    `decks/gf180mcu.py`'s own docstring). The compiled fixture that
    validates this entry in isolation
    (`tests/test_lvs_device_provenance.py`'s
    `test_golden_pair_gf180mcu_diodes_extract_with_provenance_cited_devices`)
    draws no `LVPWELL` region at all, so the native device does not
    recognise anything there (verified: zero devices extracted from that
    exact fixture). Authoring a second, `LVPWELL`-shaped fixture, and
    resolving whether that really is the same physical device the compiled
    deck's simpler substrate-tied recognition approximates, is out of this
    issue's scope."""
    by_name = {d.name: d for d in EXTRACTION_DECK.diodes}
    assert by_name["diode_nd2ps_06v0"].provenance.rule_id == (
        "gf180mcu_fd_pr__diode_nd2ps_06v0"
    )


def test_diode_pd2nw_06v0_native_cross_check_deferred() -> None:
    """`gf180mcu_fd_pr__diode_pd2nw_06v0` is **not** cross-checked against
    the native deck by this module -- investigated, deferred.

    Verified against a real `gf180mcu.lvs`: the native deck's
    `diode_pd2nw_06v0` device requires its `'N'` (well) terminal to
    *interact with an n+ diffusion region* (`nwell_mv.not(dnwell)
    .interacting(ncomp)`) -- i.e. an actual `Nwell` tap/tie region, not just
    the bare `Nwell` polygon the compiled deck's own simpler recognition
    draws. A fixture built from the compiled deck's golden-pair geometry
    (`Nwell` + `Dualgate` + `Pplus`-marked `Comp` anode, no well tap)
    extracts zero devices under the native deck (verified). Authoring a
    correctly-shaped well-tap addition, and confirming it does not also
    trip one of the native deck's other, closely-related `pd2nw`/`nw2ps`
    device-class variants, is out of this issue's scope."""
    by_name = {d.name: d for d in EXTRACTION_DECK.diodes}
    assert by_name["diode_pd2nw_06v0"].provenance.rule_id == (
        "gf180mcu_fd_pr__diode_pd2nw_06v0"
    )


# --------------------------------------------------------------------------- #
# Coverage discipline -- mirrors `test_lvs_native_extraction_cross_check.py`'s
# own `test_cross_check_reaches_a_verdict_for_every_provenanced_sky130_
# device_rule`: every provenanced gf180mcu device rule has a cross-check
# verdict in this module (run + agree, run + documented disagreement, or
# investigated + deferred) -- a future provenance-backfilled device rule
# with none of the three fails this assertion instead of silently
# under-covering.
# --------------------------------------------------------------------------- #

#: Upstream `rule_id`s this module reaches a verdict for, one way or another
#: -- hand-maintained, same discipline as the sky130 module's own
#: `_CROSS_CHECK_VERDICT_RULE_IDS`.
_CROSS_CHECK_VERDICT_RULE_IDS = frozenset(
    {
        "gf180mcu_fd_pr__nfet_03v3",  # agrees
        "gf180mcu_fd_pr__pfet_03v3",  # agrees
        "gf180mcu_fd_pr__ppolyf_u",  # agrees
        "gf180mcu_fd_pr__ppolyf_u_1k",  # agrees (native's own default flavour)
        "cap_mim_2f0_m4m5_noshield",  # documented disagreement (#512)
        "BJT.3",  # deferred (investigated)
        "gf180mcu_fd_pr__diode_nd2ps_06v0",  # deferred (investigated)
        "gf180mcu_fd_pr__diode_pd2nw_06v0",  # deferred (investigated)
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


def test_cross_check_reaches_a_verdict_for_every_provenanced_gf180mcu_device_rule() -> (
    None
):
    assert (
        _provenanced_device_rule_ids(EXTRACTION_DECK) == _CROSS_CHECK_VERDICT_RULE_IDS
    )
