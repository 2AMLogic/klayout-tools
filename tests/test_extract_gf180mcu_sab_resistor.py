"""An SB.10-compliant (SAB-enclosed) gf180mcu poly resistor extracts as a
two-terminal device (issue #5889).

gf180mcu's DRM mandates an *enclosure* rule on every SAB-blocked poly
resistor: SB.10 (``poly2.enclosing.sab.1``) requires Poly2 to extend
>= 220 nm beyond every SAB edge. The recognised resistor body is
``Poly2 & SAB & RES_MK (& implant)``, so a DRC-clean drawing surrounds the
body with an unmarked 220 nm Poly2 band on every side -- and that band
wraps the body and touches it along its whole perimeter, merging both
contacted leads into ONE terminal component. KLayout's
``DeviceExtractorResistorWithBulk`` then finds a single contact region
("Expected two polygons on contacts interacting with one resistor shape")
and forms **no device**: the resistor silently vanishes from the extracted
netlist while ``klt drc`` reports it clean. Before issue #5889 a user had
to choose between DRC-clean and LVS-extractable.

The fix (declared on the deck's ``ppolyf_u``/``ppolyf_u_1k`` entries via
``ResistorDevice.terminal_enclosure_um``) narrows the terminal region to
the conductor within the SB.10 distance of conductor drawn *beyond* the
mandated band -- the two leads and their channels through the band -- so
the band's side flanks no longer conduct as a terminal and each lead lands
on its own net.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.decks.gf180mcu import EXTRACTION_DECK
from klayout_tools.drc import run_drc
from klayout_tools.extract import run_extract
from klayout_tools.extract_passive_regions import _sever_terminal_enclosure_bands

_DBU_UM = 0.001
_HALO_UM = 0.22  # SB.10 / poly2.enclosing.sab.1, 220 nm
_HALO_DBU = round(_HALO_UM / _DBU_UM)

_POLY2 = (30, 0)
_RES_MK = (110, 5)
_SAB = (49, 0)

# Every recognised body is a 6um x 1um segment (L=6um, W=1um -> 6.0
# squares) between x=3 and x=9, matching the geometry convention of
# `tests/test_lvs_device_provenance.py`'s `_make_gf180mcu_resistor_layout`.
_BODY = (3.0, 0.0, 9.0, 1.0)
_SQUARES = 6.0


def _box_um(x0: float, y0: float, x1: float, y1: float) -> kdb.Box:
    return kdb.Box(
        round(x0 / _DBU_UM),
        round(y0 / _DBU_UM),
        round(x1 / _DBU_UM),
        round(y1 / _DBU_UM),
    )


def _new_layout() -> tuple[kdb.Layout, kdb.Cell]:
    layout = kdb.Layout()
    layout.dbu = _DBU_UM
    return layout, layout.create_cell("TOP")


def _draw(top: kdb.Cell, layout: kdb.Layout, layer: tuple[int, int], box: kdb.Box):
    top.shapes(layout.layer(*layer)).insert(box)


def _label(
    top: kdb.Cell,
    layout: kdb.Layout,
    layer: tuple[int, int],
    text: str,
    x: float,
    y: float,
) -> None:
    top.shapes(layout.layer(*layer)).insert(
        kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
    )


def _add_contacted_leads(
    top: kdb.Cell, layout: kdb.Layout, left_x_um: float = 0.0, right_x_um: float = 12.0
) -> None:
    """Two contacted, labelled heads at either end of the resistor -- the
    same Contact/Metal1 pad + pin-label convention the existing gf180mcu
    resistor fixtures use, so each lead resolves to a distinct named net.
    ``left_x_um``/``right_x_um`` name where each lead's outer end sits."""
    _draw(
        top,
        layout,
        (33, 0),
        _box_um(left_x_um + 0.1, 0.3, left_x_um + 0.32, 0.7),  # Contact (head A)
    )
    _draw(
        top,
        layout,
        (33, 0),
        _box_um(right_x_um - 0.32, 0.3, right_x_um - 0.1, 0.7),  # Contact (head B)
    )
    _draw(
        top,
        layout,
        (34, 0),
        _box_um(left_x_um, 0.2, left_x_um + 0.4, 0.8),  # Metal1 (head A pad)
    )
    _draw(
        top,
        layout,
        (34, 0),
        _box_um(right_x_um - 0.4, 0.2, right_x_um, 0.8),  # Metal1 (head B pad)
    )
    _label(top, layout, (34, 10), "RA", left_x_um + 0.2, 0.5)
    _label(top, layout, (34, 10), "RB", right_x_um - 0.2, 0.5)


def _make_sb10_compliant_layout(
    narrow_layers: tuple[tuple[int, int], ...],
    lead_height_um: float = 0.6,
) -> kdb.Layout:
    """A DRC-clean (SB.10-compliant) poly resistor: the marked region is
    the exact 6um x 1um body, and Poly2 extends 220 nm beyond every SAB
    edge -- the minimum the DRM's own ``poly2.enclosing.sab.1`` accepts --
    via one halo block plus two leads that cross the band and continue
    outward.

    ``narrow_layers`` selects the flavour: ``(31, 0), (49, 0)``
    (Pplus + SAB) -> ``ppolyf_u``; ``(49, 0), (62, 0)`` (SAB + Resistor)
    -> ``ppolyf_u_1k``."""
    layout, top = _new_layout()
    body = _box_um(*_BODY)
    block = kdb.Box(
        body.left - _HALO_DBU,
        body.bottom - _HALO_DBU,
        body.right + _HALO_DBU,
        body.top + _HALO_DBU,
    )
    # Poly2 = the halo block (SB.10's mandated band) plus two leads that
    # cross the band and continue outward, contacted outside SAB (SB.4).
    lead_half = round(lead_height_um / 2 / _DBU_UM)
    mid_y = (body.bottom + body.top) // 2
    _draw(top, layout, _POLY2, block)
    _draw(
        top,
        layout,
        _POLY2,
        kdb.Box(0, mid_y - lead_half, block.left, mid_y + lead_half),
    )
    _draw(
        top,
        layout,
        _POLY2,
        kdb.Box(block.right, mid_y - lead_half, 12000, mid_y + lead_half),
    )
    # The marker and SAB cover exactly the body: the marked region's own
    # area/geometry (and so the extracted L/W) is the exact drawn body.
    _draw(top, layout, _RES_MK, body)
    _draw(top, layout, _SAB, body)
    for layer in narrow_layers:
        _draw(top, layout, layer, _box_um(2.0, -1.0, 10.0, 2.0))
    _add_contacted_leads(top, layout)
    return layout


def _make_exact_body_marker_layout(
    narrow_layers: tuple[tuple[int, int], ...],
) -> kdb.Layout:
    """The pre-#5889 fixture convention: a 12x1um Poly2 bar whose SAB
    *overhangs* the poly sideways (SB.10 violated -- Poly2 does not extend
    past the SAB edge), so no unmarked band wraps the body. Extraction
    must keep working identically here."""
    layout, top = _new_layout()
    _draw(top, layout, _POLY2, _box_um(0, 0, 12, 1))
    _draw(top, layout, _RES_MK, _box_um(*_BODY))
    for layer in narrow_layers:
        _draw(top, layout, layer, _box_um(2.8, -0.2, 9.2, 1.2))
    _add_contacted_leads(top, layout)
    return layout


def _extract_one(layout: kdb.Layout, tmp_path: Path, name: str) -> dict:
    layout.write(str(tmp_path / f"{name}.gds"))
    return run_extract(
        str(tmp_path / f"{name}.gds"),
        "gf180mcu",
        output=str(tmp_path / f"{name}.spice"),
    )


def _sheet_rho(name: str) -> float:
    return next(r for r in EXTRACTION_DECK.resistors if r.name == name).sheet_rho_ohm_sq


# --------------------------------------------------------------------------- #
# The SB.10-compliant resistor itself
# --------------------------------------------------------------------------- #


def test_sb10_compliant_ppolyf_u_extracts_one_two_terminal_device(
    tmp_path: Path,
) -> None:
    """AC: a compliant halo + contacted leads extracts as exactly one
    two-terminal ``ppolyf_u`` with the L/W-derived value and distinct
    terminal nets. Before the fix this exact layout produced zero devices."""
    report = _extract_one(
        _make_sb10_compliant_layout(((31, 0), (49, 0))), tmp_path, "compliant_u"
    )

    assert report["device_counts"] == {"ppolyf_u": 1}
    (device,) = report["devices"]
    assert device["class"] == "ppolyf_u"
    assert device["params"]["l_um"] == pytest.approx(6.0)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert device["params"]["r_ohm"] == pytest.approx(_SQUARES * _sheet_rho("ppolyf_u"))
    assert {device["nets"]["a"], device["nets"]["b"]} == {"RA", "RB"}


def test_sb10_compliant_ppolyf_u_1k_flavour_extracts_identically(
    tmp_path: Path,
) -> None:
    """AC: the ``ppolyf_u_1k``/``_2k``/``_3k`` entry shares the same
    terminal derivation (the field is declared on both entries), so the
    SAB+Resistor-narrowed flavour of the same compliant geometry extracts
    the same two-terminal device at its own sheet rho."""
    report = _extract_one(
        _make_sb10_compliant_layout(((49, 0), (62, 0))), tmp_path, "compliant_1k"
    )

    assert report["device_counts"] == {"ppolyf_u_1k": 1}
    (device,) = report["devices"]
    assert device["params"]["l_um"] == pytest.approx(6.0)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert device["params"]["r_ohm"] == pytest.approx(
        _SQUARES * _sheet_rho("ppolyf_u_1k")
    )
    assert {device["nets"]["a"], device["nets"]["b"]} == {"RA", "RB"}


def test_sb10_compliant_layout_has_no_poly2_enclosing_sab_violation(
    tmp_path: Path,
) -> None:
    """AC: ``klt drc`` on the compliant layout reports no
    ``poly2.enclosing.sab.1`` violation, so the extraction above is of a
    layout the DRM's own enclosure rule accepts (that the rule itself is
    live and flags short extensions is already covered by
    ``tests/test_drc_gf180mcu_offgrid_sab.py::test_poly_extension_beyond_sab_sb10``)."""
    compliant = _make_sb10_compliant_layout(((31, 0), (49, 0)))
    compliant.write(str(tmp_path / "drc_compliant.gds"))
    report = run_drc(str(tmp_path / "drc_compliant.gds"), "gf180mcu")
    assert not report["rule_counts"].get("poly2.enclosing.sab.1")
    assert "poly2.enclosing.sab.1" in report["coverage"]["rules_checked"]


# --------------------------------------------------------------------------- #
# No regression on the SB.10-violating exact-body convention
# --------------------------------------------------------------------------- #


def test_exact_body_marker_ppolyf_u_extracts_identically(tmp_path: Path) -> None:
    """AC: the same layout drawn with exact-body markers (SB.10 violated,
    no band) still extracts identically -- the narrowed terminal derivation
    degenerates to today's whole-conductor terminal when no band exists."""
    report = _extract_one(
        _make_exact_body_marker_layout(((31, 0), (49, 0))), tmp_path, "exact_body_u"
    )

    assert report["device_counts"] == {"ppolyf_u": 1}
    (device,) = report["devices"]
    assert device["params"]["l_um"] == pytest.approx(6.0)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert device["params"]["r_ohm"] == pytest.approx(_SQUARES * _sheet_rho("ppolyf_u"))
    assert {device["nets"]["a"], device["nets"]["b"]} == {"RA", "RB"}


# --------------------------------------------------------------------------- #
# Edge cases from the issue's test plan
# --------------------------------------------------------------------------- #


def test_sb10_compliant_lead_wider_than_body(tmp_path: Path) -> None:
    """A lead wider than the body still extracts one device at the body's
    own L/W: the channel through the band scales with the lead, and the
    body region itself is what measures L/W."""
    report = _extract_one(
        _make_sb10_compliant_layout(((31, 0), (49, 0)), lead_height_um=2.0),
        tmp_path,
        "compliant_wide_lead",
    )

    assert report["device_counts"] == {"ppolyf_u": 1}
    (device,) = report["devices"]
    assert device["params"]["l_um"] == pytest.approx(6.0)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert device["params"]["r_ohm"] == pytest.approx(_SQUARES * _sheet_rho("ppolyf_u"))
    assert {device["nets"]["a"], device["nets"]["b"]} == {"RA", "RB"}


def test_sb10_compliant_two_resistors_sharing_one_halo(tmp_path: Path) -> None:
    """Two SAB bodies 0.42um apart (SB.2's minimum) share one poly block:
    the gap's salicided poly sits inside both mandated bands, and each
    resistor still extracts its own two-terminal device in series with the
    other -- the shared strip is the middle net, not a severed flank."""
    layout, top = _new_layout()
    body1 = _box_um(3.0, 0.0, 9.0, 1.0)
    body2 = _box_um(9.42, 0.0, 15.42, 1.0)
    block = kdb.Box(
        body1.left - _HALO_DBU,
        body1.bottom - _HALO_DBU,
        body2.right + _HALO_DBU,
        body1.top + _HALO_DBU,
    )
    _draw(top, layout, _POLY2, block)
    _draw(top, layout, _POLY2, _box_um(0, 0.2, block.left / 1000, 0.8))
    _draw(top, layout, _POLY2, _box_um(block.right / 1000, 0.2, 18.0, 0.8))
    for body in (body1, body2):
        _draw(top, layout, _RES_MK, body)
        _draw(top, layout, _SAB, body)
    _draw(top, layout, (31, 0), _box_um(2.0, -1.0, 16.5, 2.0))
    _add_contacted_leads(top, layout, right_x_um=18.0)

    report = _extract_one(layout, tmp_path, "compliant_series")

    assert report["device_counts"] == {"ppolyf_u": 2}
    values = sorted(d["params"]["r_ohm"] for d in report["devices"])
    assert values == [pytest.approx(_SQUARES * _sheet_rho("ppolyf_u"))] * 2
    # Three distinct a/b nets across the two series devices: RA, RB, and
    # the shared middle node (anonymous -- nothing contacts it); each
    # device also carries its own bulk "w" terminal on the substrate
    # global, excluded here.
    ab_nets = [{d["nets"]["a"], d["nets"]["b"]} for d in report["devices"]]
    all_nets = set().union(*ab_nets)
    assert "RA" in all_nets and "RB" in all_nets
    assert len(all_nets) == 3
    # The anonymous middle node is shared by exactly the two devices, in
    # series: each device's two terminals are {RA, mid} and {mid, RB}.
    mid = (all_nets - {"RA", "RB"}).pop()
    assert {frozenset(nets) for nets in ab_nets} == {
        frozenset({"RA", mid}),
        frozenset({mid, "RB"}),
    }


# --------------------------------------------------------------------------- #
# Local-window severing: geometry equivalence and boundary cases (#6493)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _EncSpec:
    """The one ``ResistorDevice`` field ``_sever_terminal_enclosure_bands`` reads."""

    terminal_enclosure_um: float


@dataclass(frozen=True)
class _DbuStub:
    dbu: float


def _reference_sever(
    layout: _DbuStub,
    recognised: list[tuple[_EncSpec, kdb.Region, tuple[int, int]]],
    bases: dict[tuple[int, int], kdb.Region],
) -> None:
    """The pre-#6493 full-layer loop, verbatim: the optimised helper must
    leave its output identical on every fixture below."""
    severed_by_layer: dict[tuple[int, int], kdb.Region] = {}
    for spec, body, terminal_layer in recognised:
        if spec.terminal_enclosure_um <= 0.0:
            continue
        conductor = bases[terminal_layer]
        enc_dbu = max(1, int(round(spec.terminal_enclosure_um / layout.dbu)))
        layer_severed = severed_by_layer.setdefault(terminal_layer, kdb.Region())
        for component in body.merged().each():
            ring = kdb.Region(component).sized(enc_dbu)
            band = conductor & ring
            if band.is_empty():
                continue
            outer = conductor - ring
            layer_severed += band - outer.sized(enc_dbu)
    for layer, severed in severed_by_layer.items():
        if not severed.is_empty():
            bases[layer] = bases[layer] - severed


def _windowed_sever(
    layout: _DbuStub,
    recognised: list[tuple[_EncSpec, kdb.Region, tuple[int, int]]],
    bases: dict[tuple[int, int], kdb.Region],
    slack_dbu: int,
) -> None:
    """The production algorithm with the window shrunk by ``slack_dbu``:
    an undersized window stands in for a wrong clipping-bound proof."""
    severed_by_layer: dict[tuple[int, int], kdb.Region] = {}
    for spec, body, terminal_layer in recognised:
        if spec.terminal_enclosure_um <= 0.0:
            continue
        conductor = bases[terminal_layer]
        enc_dbu = max(1, int(round(spec.terminal_enclosure_um / layout.dbu)))
        layer_severed = severed_by_layer.setdefault(terminal_layer, kdb.Region())
        for component in body.merged().each():
            ring = kdb.Region(component).sized(enc_dbu)
            local = conductor & ring.sized(max(0, enc_dbu - slack_dbu))
            band = local & ring
            if band.is_empty():
                continue
            layer_severed += band - (local - ring).sized(enc_dbu)
    for layer, severed in severed_by_layer.items():
        if not severed.is_empty():
            bases[layer] = bases[layer] - severed


def _severed_by(fn, specs_bodies, conductor, **kwargs):
    bases = {_POLY2: conductor.dup()}
    fn(_DbuStub(_DBU_UM), list(specs_bodies), bases, **kwargs)
    return bases[_POLY2]


def _same_geometry(a: kdb.Region, b: kdb.Region) -> bool:
    """Region ``==`` is representation-strict; identical area is ``^`` empty."""
    return (a ^ b).is_empty()


def _assert_matches_reference(specs_bodies, conductor) -> None:
    reference = _severed_by(_reference_sever, specs_bodies, conductor)
    optimized = _severed_by(_sever_terminal_enclosure_bands, specs_bodies, conductor)
    assert _same_geometry(optimized, reference)


def _halo_only_conductor(body: kdb.Box) -> kdb.Region:
    """A floating SB.10 band: halo block, no leads -- nothing outside it."""
    return kdb.Region(body.enlarged(_HALO_DBU, _HALO_DBU)) - kdb.Region(body)


def test_windowed_severing_matches_reference_on_a_randomised_fixture() -> None:
    """AC (#6493): the local-window helper severs the identical conductor
    the full-layer reference loop does, across overlapping neighbourhoods
    (random bodies on one layer -> rings and windows overlap), conductor
    crossing window boundaries (halo blocks straddling several rings),
    entries sharing a terminal layer with different enclosures (including
    one rounding to a single DBU and one disabled), and pre-cut views."""
    rng = random.Random(6493)
    conductor, specs_bodies = kdb.Region(), []
    for enc_um in (0.22, 0.1, 0.0005, 0.0):
        body = kdb.Region()
        for _ in range(7):
            x, y = rng.randrange(0, 40), rng.randrange(0, 40)
            w, h = rng.randrange(2, 9), rng.randrange(2, 7)
            body.insert(_box_um(x, y, x + w, y + h))
            if rng.random() < 0.8:  # halo-ish conductor crossing the rings
                conductor.insert(_box_um(x - 0.3, y - 0.3, x + w + 0.3, y + h + 0.3))
            if rng.random() < 0.6:  # stray conductor at varying distances
                sx, sy = x + rng.uniform(-3, w + 2), y + rng.uniform(-3, h + 2)
                conductor.insert(_box_um(sx, sy, sx + rng.uniform(0.05, 1.0), sy + 0.2))
        specs_bodies.append((_EncSpec(enc_um), body.merged(), _POLY2))
    for _, body, _ in specs_bodies:  # caller contract: bodies pre-removed
        conductor -= body
    conductor = conductor.merged()
    _assert_matches_reference(specs_bodies, conductor)


def test_conductor_just_outside_the_ring_influences_the_cut() -> None:
    """AC (#6493): a conductor strip 50 nm beyond the ring -- inside the
    ring.sized(enc) window, well inside ``outer.sized(enc_dbu)``'s reach --
    must still keep its nearby band alive, and an equivalence harness that
    lost it would sever the whole floating band instead."""
    body = _box_um(*_BODY)
    strip = _box_um(4.0, 1.27, 6.0, 1.35)  # ring top is y=1.22: gap 0.05um
    conductor = _halo_only_conductor(body) + kdb.Region(strip)
    specs = [(_EncSpec(_HALO_UM), kdb.Region(body), _POLY2)]
    severed = _halo_only_conductor(body) - kdb.Region(strip).sized(_HALO_DBU)
    expected = conductor - severed
    remaining = _severed_by(_sever_terminal_enclosure_bands, specs, conductor)
    assert _same_geometry(remaining, expected) and not severed.is_empty()
    # The deliberately undersized window (ring.sized(20nm) excludes the
    # strip) fails this boundary regression -- the harness is not vacuous.
    undersized = _severed_by(_windowed_sever, specs, conductor, slack_dbu=200)
    assert not _same_geometry(undersized, expected)
    assert _same_geometry(undersized, kdb.Region(strip))


def test_absent_band_is_untouched_and_a_floating_band_is_fully_severed() -> None:
    """AC (#6493): conductor beyond every window passes through unchanged,
    and a band with no outer conductor at all (no leads) is severed whole."""
    body = _box_um(*_BODY)
    specs = [(_EncSpec(_HALO_UM), kdb.Region(body), _POLY2)]
    far = kdb.Region(_box_um(20.0, 20.0, 26.0, 21.0))
    untouched = _severed_by(_sever_terminal_enclosure_bands, specs, far)
    assert _same_geometry(untouched, far)
    floating = _halo_only_conductor(body)
    assert _severed_by(_sever_terminal_enclosure_bands, specs, floating).is_empty()
