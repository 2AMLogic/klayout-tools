"""ASAP7 FinFET extraction (issue #2761): geometry-counted ``nfin``.

Two groups:

- **Synthetic fixtures** (always run, no PDK): hand-drawn ASAP7-layer
  nFET/pFET devices at the pinned technology's true scale (dbu 0.00025 um,
  fin pitch 108 dbu = 27 nm, fin width 28 dbu, gate 80 dbu = 20 nm, contacted
  poly pitch 216 dbu) -- fin counts, fingers, VT classes, the exported SPICE
  card, and every refused-geometry diagnostic.
- **Pinned corpus** (skip with a reason unless ``scripts/fetch-pdks.sh`` has
  fetched lambdapdk v0.2.17): real ``asap7sc7p5t`` cells against their CDL.

Every output goes to ``tmp_path``.
"""

from __future__ import annotations

import collections
import json
import re
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools import asap7_bsimcmg, pdk_capabilities
from klayout_tools.cli import main
from klayout_tools.decks import (
    FinFETExtractionDeck,
    get_extraction_deck,
    known_extraction_deck_names,
)
from klayout_tools.decks.asap7 import EXTRACTION_DECK
from klayout_tools.extract import ExtractError, run_extract
from klayout_tools.extract_finfet import (
    CODE_DBU_MISMATCH,
    CODE_MALFORMED,
    FinFETGeometryError,
    _ordered_source_drain,
    extract_finfet_netlist,
    finfet_device_class,
)
from klayout_tools.lvs import LvsError, run_lvs
from klayout_tools.pdk_families import KNOWN_PDK_FAMILIES, pdk_variant_family

DBU = 0.00025
WELL, FIN, GATE, GCUT, ACTIVE = (1, 0), (2, 0), (7, 0), (10, 0), (11, 0)
NSEL, PSEL, LIG, LISD, V0, M1, SDT = (
    (12, 0),
    (13, 0),
    (16, 0),
    (17, 0),
    (18, 0),
    (19, 0),
    (88, 0),
)
M1_PIN, WELL_PIN, GATE_PIN = (19, 251), (1, 251), (7, 251)
LVT, SLVT = (98, 0), (97, 0)
THICKOX = (111, 0)


# --------------------------------------------------------------------------- #
# Synthetic fixture builder
# --------------------------------------------------------------------------- #


class _Canvas:
    def __init__(self, dbu: float) -> None:
        self.layout = kdb.Layout()
        self.layout.dbu = dbu
        self.top = self.layout.create_cell("TOP")

    def box(self, ld, left, bottom, right, top):
        self.top.shapes(self.layout.layer(*ld)).insert(
            kdb.Box(left, bottom, right, top)
        )

    def text(self, ld, string, x, y):
        self.top.shapes(self.layout.layer(*ld)).insert(
            kdb.Text(string, kdb.Trans(x, y))
        )


def _draw_implants(c, *, polarity, select, well, vt, width, y1):
    """Select, well (labelled ``VB``) and VT markers over the whole device."""
    if select:
        c.box(NSEL if polarity == "n" else PSEL, 0, -300, width, y1 + 500)
    if well if well is not None else polarity == "p":
        c.box(WELL, 0, -300, width, y1 + 500)
        c.text(WELL_PIN, "VB", 10, y1 + 400)
    markers = () if vt is None else (vt if isinstance(vt[0], tuple) else (vt,))
    for marker in markers:
        c.box(marker, 0, -300, width, y1 + 500)


def _draw_gates(c, *, fingers, gate_w, y1):
    """Gate fingers at the 216-dbu contacted pitch, joined by a gate bar
    outside the active and labelled ``G``."""
    for i in range(fingers):
        gx = 284 + i * 216
        c.box(GATE, gx, -20, gx + gate_w, y1 + 160)
    c.box(GATE, 284, y1 + 80, 284 + (fingers - 1) * 216 + gate_w, y1 + 160)
    c.text(GATE_PIN, "G", 300, y1 + 120)


def _draw_contacts(c, *, fingers, y0, y1):
    """SDT+LISD on every source/drain column. Even columns (sources) join on a
    LISD bar below the active, tapped to an M1 pad labelled ``S``; odd
    columns (drains) rise on M1 to a bar labelled ``D``."""
    for j in range(fingers + 1):
        cx = 168 + 216 * j
        c.box(SDT, cx, y0, cx + 96, y1)
        if j % 2 == 0:
            c.box(LISD, cx, -150, cx + 96, y1)
            continue
        c.box(LISD, cx, y0, cx + 96, y1)
        c.box(V0, cx + 12, y1 - 80, cx + 84, y1 - 8)
        c.box(M1, cx + 12, y1 - 80, cx + 84, y1 + 400)
    c.box(LISD, 168, -150, 168 + 216 * fingers + 96, -100)
    c.box(V0, 180, -150, 252, -100)
    c.box(M1, 170, -160, 262, -90)
    c.text(M1_PIN, "S", 200, -120)
    c.box(M1, 168, y1 + 330, 168 + 216 * fingers + 96, y1 + 400)
    c.text(M1_PIN, "D", 400, y1 + 360)


def _draw_device(
    *,
    polarity: str = "n",
    fins: int = 3,
    fingers: int = 1,
    gate_w: int = 80,
    vt: tuple[int, int] | tuple[tuple[int, int], ...] | None = None,
    select: bool = True,
    well: bool | None = None,
    draw_fins: bool = True,
    active_y0: int = 108,
    stray_fin: bool = False,
    dbu: float = DBU,
) -> kdb.Layout:
    """One FinFET (``fingers`` parallel gate fingers) in cell ``TOP``, with
    nets ``S``/``G``/``D`` (and a well ``VB`` for a pFET). Fins run
    horizontally across the whole cell at the 108-dbu pitch; the active spans
    exactly ``fins`` of them unless ``active_y0`` clips one."""
    c = _Canvas(dbu)
    y1 = 108 + fins * 108
    x_right = 464 + (fingers - 1) * 216
    width = x_right + 300
    c.box(ACTIVE, 184, active_y0, x_right, y1)
    for k in range(fins + 2 if draw_fins else 0):
        c.box(FIN, 0, 40 + k * 108, width, 68 + k * 108)
    if stray_fin:
        # A fin drawn *parallel* to the gate, inside the channel only.
        c.box(FIN, 310, 200, 338, 300)
    _draw_implants(
        c, polarity=polarity, select=select, well=well, vt=vt, width=width, y1=y1
    )
    _draw_gates(c, fingers=fingers, gate_w=gate_w, y1=y1)
    _draw_contacts(c, fingers=fingers, y0=active_y0, y1=y1)
    return c.layout


def _write(layout: kdb.Layout, tmp_path: Path, name: str = "dev.gds") -> str:
    path = tmp_path / name
    layout.write(str(path))
    return str(path)


def _extract(tmp_path: Path, **kwargs) -> dict:
    path = _write(_draw_device(**kwargs), tmp_path)
    return run_extract(path, "asap7", output=str(tmp_path / "dev.spice"))


def _only_device(report: dict) -> dict:
    assert len(report["devices"]) == 1, report["devices"]
    return report["devices"][0]


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_asap7_family_and_extraction_deck_registered():
    assert "asap7" in KNOWN_PDK_FAMILIES
    assert pdk_variant_family("asap7") == "asap7"
    assert "asap7" in known_extraction_deck_names()
    assert isinstance(get_extraction_deck("asap7"), FinFETExtractionDeck)


def test_asap7_capabilities_advertise_extraction_and_parasitics():
    decisions = pdk_capabilities.DECISIONS["asap7"]
    supported = {name for name, d in decisions.items() if d.status == "supported"}
    assert supported == {"extraction", "parasitics"}
    assert "uncalibrated" in decisions["parasitics"].reason
    # DRC stays a separate rule table (#2760).
    assert "#2760" in decisions["curated_drc"].reason


def test_asap7_deck_layers_match_pinned_lyp_roles():
    deck = EXTRACTION_DECK
    assert deck.fin == FIN and deck.gate_cut == GCUT and deck.poly == GATE
    assert (deck.nselect, deck.pselect) == (NSEL, PSEL)
    assert (deck.lisd, deck.lig, deck.contact, deck.sd_contact) == (LISD, LIG, V0, SDT)
    assert deck.metals[0] == M1 and deck.nominal_dbu_um == DBU
    assert {f.marker: f.nfet_class for f in deck.vt_flavours} == {
        (98, 0): "nmos_lvt",
        (97, 0): "nmos_slvt",
        (110, 0): "nmos_sram",
        (111, 0): "nmos_io",
    }


# --------------------------------------------------------------------------- #
# Fin counting, L, fingers, VT
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("polarity", ["n", "p"])
@pytest.mark.parametrize("fins", [1, 3, 4])
def test_fin_count_and_gate_length_recovered_exactly(tmp_path, polarity, fins):
    device = _only_device(_extract(tmp_path, polarity=polarity, fins=fins))
    assert device["class"] == ("nmos_rvt" if polarity == "n" else "pmos_rvt")
    assert device["params"]["nfin"] == fins
    assert device["params"]["fingers"] == 1
    assert device["params"]["l_um"] == pytest.approx(0.020)
    # Drawn active width under the gate -- reported, but never the source
    # of nfin.
    assert device["params"]["w_um"] == pytest.approx(fins * 0.027)
    # FinFET classes do not measure junction geometry; no fake zeros.
    assert "as_um2" not in device["params"]
    assert device["nets"] == {
        "s": "S",
        "g": "G",
        "d": "D",
        "b": "vsubs" if polarity == "n" else "VB",
    }


def test_source_drain_order_is_geometric_not_iteration_order():
    """Issue #3154: S/D used to follow a merged Region's iteration order,
    which differs between the macOS and Linux klayout wheels. The
    assignment must be the same whichever order the neighbours arrive in."""
    left = kdb.Polygon(kdb.Box(0, 0, 50, 100))
    right = kdb.Polygon(kdb.Box(100, 0, 150, 100))
    assert _ordered_source_drain([left, right]) == (left, right)
    assert _ordered_source_drain([right, left]) == (left, right)
    # Vertically stacked neighbours (a rotated device) tie on `left`; the
    # lower one is the source.
    low = kdb.Polygon(kdb.Box(0, 0, 100, 50))
    high = kdb.Polygon(kdb.Box(0, 100, 100, 150))
    assert _ordered_source_drain([high, low]) == (low, high)


def test_parallel_fingers_merge_with_nfin_summed(tmp_path):
    report = _extract(tmp_path, fins=3, fingers=2)
    device = _only_device(report)
    assert device["params"]["nfin"] == 6
    assert device["params"]["fingers"] == 2
    assert device["params"]["l_um"] == pytest.approx(0.020)


def test_three_fingers_merge(tmp_path):
    device = _only_device(_extract(tmp_path, fins=2, fingers=3))
    assert (device["params"]["nfin"], device["params"]["fingers"]) == (6, 3)


@pytest.mark.parametrize(
    ("marker", "nclass", "pclass"),
    [
        (LVT, "nmos_lvt", "pmos_lvt"),
        (SLVT, "nmos_slvt", "pmos_slvt"),
        (THICKOX, "nmos_io", "pmos_io"),
    ],
)
def test_vt_marker_selects_model_class(tmp_path, marker, nclass, pclass):
    assert _only_device(_extract(tmp_path, vt=marker))["class"] == nclass
    p_dir = tmp_path / "p"
    p_dir.mkdir()
    assert _only_device(_extract(p_dir, polarity="p", vt=marker))["class"] == pclass


def test_exported_spice_card_carries_nfin_and_cdl_parameter_set(tmp_path):
    _extract(tmp_path, fins=3, fingers=2)
    cards = [
        line
        for line in (tmp_path / "dev.spice").read_text().splitlines()
        if line.startswith("M")
    ]
    assert len(cards) == 1
    card = cards[0]
    assert re.search(r"\snmos_rvt L=0\.02U W=0\.162U NFIN=6$", card), card
    # Unmeasured junction geometry and the merged-finger count never reach
    # the simulator (BSIM-CMG's NF would multiply NFIN).
    for forbidden in ("AS=", "AD=", "PS=", "PD=", "FINGERS", " NF="):
        assert forbidden not in card


# --------------------------------------------------------------------------- #
# BSIM-CMG / ngspice OSDI adaptation (issue #2814)
# --------------------------------------------------------------------------- #

_HSPICE_CARDS = """\
* header comment
.model nmos_rvt nmos level = 72
+version = 107             bulkmod = 1               igcmod  = 1
+l       = 2.1e-008        tfin    = 6.5e-009
.model pmos_rvt pmos level = 72
+version = 107             bulkmod = 1
"""


def test_adapt_model_cards_targets_osdi_module_and_polarity():
    text = asap7_bsimcmg.adapt_model_cards(_HSPICE_CARDS)
    assert ".model nmos_rvt bsimcmg_va type=1" in text
    assert ".model pmos_rvt bsimcmg_va type=-1" in text
    # level/version have no counterpart in the Verilog-A module.
    assert "level" not in text.split("\n", 1)[1]
    assert "version" not in text.split("\n", 1)[1]
    # Everything else is carried over verbatim.
    assert "bulkmod=1" in text and "tfin=6.5e-009" in text and "l=2.1e-008" in text


def test_adapt_model_cards_refuses_non_bsimcmg_cards():
    with pytest.raises(ValueError, match="level = 72"):
        asap7_bsimcmg.adapt_model_cards(".model nmos_x nmos level = 54\n")
    with pytest.raises(ValueError, match="no BSIM-CMG"):
        asap7_bsimcmg.adapt_model_cards("* nothing\n")


def test_adapt_extracted_netlist_card_contract(tmp_path):
    _extract(tmp_path, fins=3, fingers=2)
    adapted = asap7_bsimcmg.adapt_extracted_netlist(
        (tmp_path / "dev.spice").read_text()
    )
    cards = [ln for ln in adapted.splitlines() if ln.startswith("N")]
    assert len(cards) == 1
    # OSDI element, L kept, W dropped, NFIN the *total* fin count (3 x 2).
    assert re.search(r"\snmos_rvt L=0\.02U NFIN=6$", cards[0]), cards[0]
    for forbidden in ("W=", "AS=", "AD=", "PS=", "PD=", "FINGERS", " NF="):
        assert forbidden not in cards[0]
    assert not any(ln.startswith("M") for ln in adapted.splitlines())


def test_adapt_extracted_netlist_sanitizes_klayout_names_consistently():
    text = (
        ".SUBCKT X A VDD VDD$1 Y\n"
        "M$1 \\$11 A Y VDD$1 nmos_rvt L=0.02U W=0.081U NFIN=3\n"
        ".ENDS\n"
    )
    out = asap7_bsimcmg.adapt_extracted_netlist(text)
    assert ".SUBCKT X A VDD VDD_1 Y" in out
    assert "N_1 _11 A Y VDD_1 nmos_rvt L=0.02U NFIN=3" in out


@pytest.mark.parametrize(
    "card, fragment",
    [
        ("M1 d g s b nmos_rvt L=0.02U W=0.1U", "no NFIN"),
        ("M1 d g s b nmos_rvt L=0.02U W=0.1U NFIN=3 NF=2", "NF="),
        ("M1 d g s b nmos_rvt L=0.02U W=0.1U NFIN=3 FINGERS=2", "FINGERS="),
        ("M1 d g s nmos_rvt L=0.02U NFIN=3", "unexpected FinFET card shape"),
    ],
)
def test_adapt_extracted_netlist_refuses_cards_it_would_mis_scale(card, fragment):
    with pytest.raises(ValueError, match=fragment):
        asap7_bsimcmg.adapt_extracted_netlist(card + "\n")


def test_different_fin_counts_stay_distinguishable(tmp_path):
    """Same connectivity, different fin count: the exported netlists differ
    and an NFIN-exact comparison of the two extractions fails."""
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    _extract(a_dir, fins=2)
    _extract(b_dir, fins=3)
    a_text = (a_dir / "dev.spice").read_text()
    b_text = (b_dir / "dev.spice").read_text()
    assert "NFIN=2" in a_text and "NFIN=3" in b_text

    def netlist(fins: int, where: Path) -> kdb.Netlist:
        layout = _draw_device(fins=fins)
        nl, _ = extract_finfet_netlist(layout, layout.top_cell(), EXTRACTION_DECK)
        for device_class in nl.each_device_class():
            device_class.equal_parameters = kdb.EqualDeviceParameters(
                device_class.parameter_id("NFIN"), 0.0, 0.0
            )
        return nl

    comparer = kdb.NetlistComparer()
    assert comparer.compare(netlist(3, a_dir), netlist(3, b_dir))
    assert not comparer.compare(netlist(2, a_dir), netlist(3, b_dir))


def test_device_class_contract_compares_nfin_exactly():
    cls = finfet_device_class("nmos_rvt")
    names = [p.name for p in cls.parameter_definitions()]
    assert {"L", "W", "NFIN", "FINGERS"} <= set(names)


# --------------------------------------------------------------------------- #
# Refused geometry: fail visibly, never approximate a planar device
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"draw_fins": False}, "no fin crossing it"),
        ({"active_y0": 160}, "clipped by the active edge"),
        ({"draw_fins": False, "stray_fin": True}, "do not cross from source to drain"),
        ({"gate_w": 96}, "gate length 24.000 nm is not a declared gate length"),
        ({"select": False}, "neither Nselect nor Pselect"),
        ({"polarity": "p", "well": False}, "PMOS channel not inside the well"),
        ({"well": True}, "NMOS channel inside the well"),
        ({"vt": (LVT, SLVT)}, "channel touches two VT markers"),
        ({"vt": (LVT, THICKOX)}, "channel touches two VT markers"),
    ],
)
def test_malformed_geometry_is_refused_with_location(tmp_path, kwargs, fragment):
    with pytest.raises(FinFETGeometryError) as excinfo:
        _extract(tmp_path, **kwargs)
    assert excinfo.value.code == CODE_MALFORMED
    assert fragment in str(excinfo.value)
    assert excinfo.value.findings
    assert all(len(f["bbox_um"]) == 4 for f in excinfo.value.findings)
    assert not (tmp_path / "dev.spice").exists()


def test_dbu_mismatch_is_refused_not_rescaled(tmp_path):
    with pytest.raises(FinFETGeometryError) as excinfo:
        _extract(tmp_path, dbu=0.001)
    assert excinfo.value.code == CODE_DBU_MISMATCH
    assert "refusing to rescale" in str(excinfo.value)


def test_cli_json_error_envelope_carries_code(tmp_path, capsys):
    path = _write(_draw_device(draw_fins=False), tmp_path)
    rc = main(
        [
            "extract",
            path,
            "--deck",
            "asap7",
            "-o",
            str(tmp_path / "out.spice"),
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    envelope = json.loads(captured.err)
    assert envelope["schema_version"] == 1
    assert envelope["error"]["command"] == "extract"
    assert envelope["error"]["code"] == CODE_MALFORMED
    assert "no fin crossing it" in envelope["error"]["message"]


def test_cli_json_success_uses_shared_envelope(tmp_path, capsys):
    path = _write(_draw_device(fins=2), tmp_path)
    rc = main(
        [
            "extract",
            path,
            "--deck",
            "asap7",
            "-o",
            str(tmp_path / "o.spice"),
            "--format",
            "json",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert isinstance(report["schema_version"], int)
    assert report["device_counts"] == {"nmos_rvt": 1}
    assert report["devices"][0]["params"]["nfin"] == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"abstract_cell_patterns": ("X*",)},
        {"pdk_variant": "asap7"},
        # Issue #2722: hierarchical emission is planar-only in increment 1.
        {"hierarchical_cells": ("STAGE",)},
    ],
)
def test_planar_only_options_are_refused_by_name(tmp_path, kwargs):
    path = _write(_draw_device(), tmp_path)
    with pytest.raises(ExtractError, match="FinFET extraction deck"):
        run_extract(path, "asap7", output=str(tmp_path / "o.spice"), **kwargs)


def test_def_driven_options_are_accepted_on_a_finfet_layout(tmp_path):
    """Issue #4559: the post-route SPEF pass hands `--def-net-names` and
    `--pins` to the FinFET path. A hand-drawn layout carries no DEF net-name
    shape property, so the rename is a loud no-op, and the declared set
    demotes every undeclared promoted pin."""
    path = _write(_draw_device(fins=2), tmp_path)
    report = run_extract(
        path,
        "asap7",
        output=str(tmp_path / "p.spice"),
        parasitics=True,
        def_net_names=True,
        declared_pins=frozenset({"G"}),
    )
    warnings = " | ".join(report["warnings"])
    assert "--def-net-names found no DEF net-name shape property" in warnings
    assert "not in the declared pin set" in warnings
    assert report["device_counts"] == {"nmos_rvt": 1}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"top_cell_pins_only": True},
        {"def_pins": frozenset({"G"})},
    ],
)
def test_remaining_planar_pin_options_stay_refused(tmp_path, kwargs):
    path = _write(_draw_device(), tmp_path)
    with pytest.raises(ExtractError, match="FinFET extraction deck"):
        run_extract(path, "asap7", output=str(tmp_path / "o.spice"), **kwargs)


def test_asap7_parasitics_deck_is_registered_and_index_aligned():
    from klayout_tools.decks import get_parasitics_deck
    from klayout_tools.extract_report import _describe_parasitics_overlap_gaps

    deck = get_parasitics_deck("asap7")
    assert len(deck.metals) == len(EXTRACTION_DECK.metals) == 9
    assert all(rc is not None and rc.sheet_res_ohm_sq > 0 for rc in deck.metals)
    assert len(deck.metal_overlaps) == len(deck.metals) - 1
    assert [cut.lower_metal_index for cut in deck.cuts] == list(range(8))
    assert not _describe_parasitics_overlap_gaps(EXTRACTION_DECK, deck)


def test_parasitics_run_on_a_finfet_layout(tmp_path):
    path = _write(_draw_device(fins=2), tmp_path)
    report = run_extract(
        path, "asap7", output=str(tmp_path / "p.spice"), parasitics=True
    )
    parasitics = report["parasitics"]
    assert parasitics["nets"], parasitics
    for entry in parasitics["nets"]:
        assert entry["resistance_ohm"] >= 0
        assert entry["capacitance_ff"] >= 0
    assert report["device_counts"] == {"nmos_rvt": 1}


def test_parasitics_cli_json_shape_matches_planar(tmp_path, capsys):
    path = _write(_draw_device(fins=2), tmp_path)
    rc = main(
        [
            "extract",
            path,
            "--deck",
            "asap7",
            "--parasitics",
            "-o",
            str(tmp_path / "o.spice"),
            "--format",
            "json",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert {"nets", "r_count", "c_count", "total_capacitance_ff"} <= set(
        report["parasitics"]
    )
    assert report["parasitics"]["metals_without_coefficient"] == []


def test_parasitics_spef_is_written_for_a_finfet_layout(tmp_path):
    path = _write(_draw_device(fins=2), tmp_path)
    spef = tmp_path / "dev.spef"
    rc = main(
        [
            "extract",
            path,
            "--deck",
            "asap7",
            "--parasitics",
            "--spef",
            str(spef),
            "-o",
            str(tmp_path / "o.spice"),
        ]
    )
    assert rc == 0
    assert "*D_NET" in spef.read_text()


@pytest.fixture(autouse=True)
def _static_build_identity(monkeypatch):
    """`klt lvs` provenance probes git (seconds per call on a big checkout);
    that is irrelevant to every assertion in this module."""
    from klayout_tools import build_identity

    monkeypatch.setattr(
        build_identity,
        "_checkout_identity",
        lambda *a, **k: {
            "git_commit": None,
            "git_tag": None,
            "dirty": None,
            "is_release": False,
        },
    )


# --------------------------------------------------------------------------- #
# klt lvs: synthetic public-path tests (issue #2813) -- no PDK assets
# --------------------------------------------------------------------------- #


def _lvs(
    tmp_path: Path,
    card: str,
    *,
    polarity="n",
    fins=3,
    fingers=1,
    options=None,
    layout_extra=None,
    reference_extra=None,
    top_line=".SUBCKT TOP S G D",
    engine=None,
):
    layout = _write(
        _draw_device(polarity=polarity, fins=fins, fingers=fingers), tmp_path
    )
    ref = tmp_path / "ref.cdl"
    ref.write_text(f"{top_line}\n{card}\n.ENDS\n")
    request = {
        "layout": {
            "file": layout,
            "deck": "asap7",
            "top": "TOP",
            **(layout_extra or {}),
        },
        "reference": {"netlist": str(ref), "top": "TOP", **(reference_extra or {})},
    }
    if options is not None:
        request["options"] = options
    if engine is not None:
        request["engine"] = engine
    return run_lvs(json.dumps(request))


def _errors(report: dict) -> list[dict]:
    return [m for m in report["mismatches"] if m["severity"] == "error"]


_NCARD = "M0 D G S vsubs nmos_rvt w=81n l=20n nfin={n}"


@pytest.mark.parametrize("fins", [1, 3])
def test_lvs_nfet_nfin_round_trips_exactly(tmp_path, fins):
    report = _lvs(tmp_path, _NCARD.format(n=fins), fins=fins)
    assert report["status"] == "match"
    assert _errors(report) == []


@pytest.mark.parametrize("fins,fingers", [(2, 2), (3, 3)])
def test_lvs_parallel_reference_cards_equal_extracted_fingers(tmp_path, fins, fingers):
    cards = "\n".join(
        f"M{k} D G S vsubs nmos_rvt w=54n l=20n nfin={fins}" for k in range(fingers)
    )
    report = _lvs(tmp_path, cards, fins=fins, fingers=fingers)
    assert report["status"] == "match"


def test_lvs_pfet_matches_with_well_bulk(tmp_path):
    report = _lvs(
        tmp_path,
        "M0 D G S VB pmos_rvt w=81n l=20n nfin=3",
        polarity="p",
        top_line=".SUBCKT TOP S G D VB",
    )
    assert report["status"] == "match"


def test_lvs_changed_nfin_is_an_actionable_mismatch(tmp_path):
    report = _lvs(tmp_path, _NCARD.format(n=4))
    assert report["status"] == "mismatch"
    assert _errors(report)


def test_lvs_different_model_class_is_a_mismatch(tmp_path):
    report = _lvs(tmp_path, "M0 D G S vsubs nmos_lvt w=81n l=20n nfin=3")
    assert report["status"] == "mismatch"
    assert _errors(report)


def _lvs_io(tmp_path, card, *, polarity="n", top_line=".SUBCKT TOP S G D"):
    layout = _write(_draw_device(polarity=polarity, vt=THICKOX), tmp_path)
    ref = tmp_path / "ref.cdl"
    ref.write_text(f"{top_line}\n{card}\n.ENDS\n")
    return run_lvs(
        json.dumps(
            {
                "layout": {"file": layout, "deck": "asap7", "top": "TOP"},
                "reference": {"netlist": str(ref), "top": "TOP"},
            }
        )
    )


def test_lvs_io_nfet_matches_nmos_io_reference(tmp_path):
    report = _lvs_io(tmp_path, "M0 D G S vsubs nmos_io w=81n l=20n nfin=3")
    assert report["status"] == "match"


def test_lvs_io_pfet_matches_pmos_io_reference(tmp_path):
    report = _lvs_io(
        tmp_path,
        "M0 D G S VB pmos_io w=81n l=20n nfin=3",
        polarity="p",
        top_line=".SUBCKT TOP S G D VB",
    )
    assert report["status"] == "match"


def test_lvs_io_device_against_core_reference_is_a_mismatch(tmp_path):
    report = _lvs_io(tmp_path, "M0 D G S vsubs nmos_rvt w=81n l=20n nfin=3")
    assert report["status"] == "mismatch"
    assert _errors(report)


def test_lvs_core_device_against_io_reference_is_a_mismatch(tmp_path):
    report = _lvs(tmp_path, "M0 D G S vsubs nmos_io w=81n l=20n nfin=3")
    assert report["status"] == "mismatch"
    assert _errors(report)


def test_core_and_io_devices_in_one_cell_keep_their_own_classes(tmp_path):
    c = _Canvas(DBU)
    for dx, vt in ((0, None), (3000, THICKOX)):
        sub = _draw_device(vt=vt)
        for li in sub.layer_indexes():
            info = sub.get_info(li)
            dst = c.layout.layer(info.layer, info.datatype)
            for sh in sub.top_cell().shapes(li).each():
                c.top.shapes(dst).insert(sh).transform(kdb.Trans(dx, 0))
    path = _write(c.layout, tmp_path)
    report = run_extract(path, "asap7", output=str(tmp_path / "dev.spice"))
    assert sorted(d["class"] for d in report["devices"]) == ["nmos_io", "nmos_rvt"]


def test_lvs_changed_gate_length_is_a_mismatch(tmp_path):
    report = _lvs(tmp_path, "M0 D G S vsubs nmos_rvt w=81n l=21n nfin=3")
    assert report["status"] == "mismatch"


def test_lvs_source_drain_interchange_is_legal(tmp_path):
    report = _lvs(tmp_path, "M0 S G D vsubs nmos_rvt w=81n l=20n nfin=3")
    assert report["status"] == "match"


def test_lvs_swapped_gate_and_drain_is_a_mismatch_when_pins_are_anchored(tmp_path):
    report = _lvs(
        tmp_path,
        "M0 G D S vsubs nmos_rvt w=81n l=20n nfin=3",
        options={"anchor_top_level_pins": True},
    )
    assert report["status"] == "mismatch"
    assert _errors(report)


@pytest.mark.parametrize(
    "card,fragment",
    [
        ("M0 D G S vsubs nmos_rvt w=81n l=20n", "no nfin"),
        ("M0 D G S vsubs nmos_rvt w=81n l=20n nfin=2.5", "positive integer"),
        ("M0 D G S vsubs nmos_rvt w=81n l=20n nfin=0", "positive integer"),
        ("M0 D G S vsubs nmos_rvt w=81n l=20n nfin=-2", "positive integer"),
    ],
)
def test_lvs_malformed_reference_nfin_is_refused_not_defaulted(
    tmp_path, card, fragment
):
    with pytest.raises(LvsError, match=fragment):
        _lvs(tmp_path, card)


@pytest.mark.parametrize(
    "options,fragment",
    [
        ({"parameter_tolerance": 0.5}, "parameter_tolerance"),
        ({"compare_parameters": {"nmos_rvt": ["L"]}}, "compare_parameters"),
        ({"combine_devices": True}, "combine_devices"),
        ({"combine_devices_per_circuit": {"*": True}}, "combine_devices_per_circuit"),
    ],
)
def test_lvs_options_that_could_relax_nfin_are_refused(tmp_path, options, fragment):
    with pytest.raises(LvsError, match=fragment):
        _lvs(tmp_path, _NCARD.format(n=3), options=options)


def test_lvs_refuses_netgen_engine_and_non_plain_reference_form(tmp_path):
    with pytest.raises(LvsError, match="engine 'klayout'"):
        _lvs(tmp_path, _NCARD.format(n=3), engine="netgen")
    with pytest.raises(LvsError, match="reference.form"):
        _lvs(tmp_path, _NCARD.format(n=3), reference_extra={"form": "subckt-call"})


def test_lvs_refuses_pre_extracted_finfet_netlist(tmp_path):
    spice = tmp_path / "l.spice"
    spice.write_text(".SUBCKT TOP S G D\n.ENDS\n")
    ref = tmp_path / "r.cdl"
    ref.write_text(".SUBCKT TOP S G D\n.ENDS\n")
    request = {
        "layout": {"netlist": str(spice), "deck": "asap7", "top": "TOP"},
        "reference": {"netlist": str(ref), "top": "TOP"},
    }
    with pytest.raises(LvsError, match="inline extraction"):
        run_lvs(json.dumps(request))


def test_lvs_bulk_policy_requires_a_finfet_deck(tmp_path):
    spice = tmp_path / "l.spice"
    spice.write_text(".SUBCKT TOP S\n.ENDS\n")
    request = {
        "layout": {"netlist": str(spice), "top": "TOP"},
        "reference": {"netlist": str(spice), "top": "TOP"},
        "options": {"finfet_bulk": {"nmos": "VSS"}},
    }
    with pytest.raises(LvsError, match="requires a FinFET"):
        run_lvs(json.dumps(request))


# Bulk policy ----------------------------------------------------------------


def test_lvs_bulk_policy_absent_net_is_a_request_error(tmp_path):
    with pytest.raises(LvsError, match="not found"):
        _lvs(tmp_path, _NCARD.format(n=3), options={"finfet_bulk": {"nmos": "VSS"}})


@pytest.mark.parametrize(
    "value",
    [{}, {"npn": "VSS"}, {"nmos": ""}, {"nmos": 3}, "VSS", ["VSS"]],
)
def test_lvs_bulk_policy_shape_is_validated(tmp_path, value):
    with pytest.raises(LvsError, match="finfet_bulk"):
        _lvs(tmp_path, _NCARD.format(n=3), options={"finfet_bulk": value})


def test_lvs_bulk_policy_default_is_off_and_echoed_null(tmp_path):
    report = _lvs(tmp_path, _NCARD.format(n=3))
    assert report["options"]["finfet_bulk"] is None
    assert not [
        m for m in report["mismatches"] if m["category"] == "device.finfet_bulk_tied"
    ]


_SOURCE_TIED_CARD = "M0 D G S S nmos_rvt w=81n l=20n nfin=3"


def test_lvs_bulk_policy_disabled_leaves_the_bulk_difference_visible(tmp_path):
    report = _lvs(tmp_path, _SOURCE_TIED_CARD)
    assert report["status"] == "mismatch"
    assert report["options"]["finfet_bulk"] is None


def test_lvs_bulk_policy_enabled_ties_discloses_and_echoes(tmp_path):
    report = _lvs(tmp_path, _SOURCE_TIED_CARD, options={"finfet_bulk": {"nmos": "S"}})
    assert report["status"] == "match"
    assert report["options"]["finfet_bulk"] == {"nmos": "S"}
    (entry,) = [
        m for m in report["mismatches"] if m["category"] == "device.finfet_bulk_tied"
    ]
    assert entry["severity"] == "warning"
    assert entry["details"]["status"] == "joined"
    assert entry["details"]["net"] == "S"
    assert entry["details"]["bulk_net"] == "vsubs"
    assert report["category_counts"]["device.finfet_bulk_tied"] == 1


def _two_cell_library(tmp_path: Path, *, with_labels: bool = True) -> str:
    """Two placed unit cells (``U1``/``U2``) in one ``TOP``; the ``S``/``G``/``D``
    labels live only in the sub-cells, never at the top."""
    out = kdb.Layout()
    out.dbu = DBU
    top = out.create_cell("TOP")
    for k, name in enumerate(("U1", "U2")):
        src = _draw_device()
        cell = out.create_cell(name)
        cell.copy_tree(src.top_cell())
        if not with_labels:
            for layer in out.layer_indexes():
                for shape in list(cell.shapes(layer).each(kdb.Shapes.STexts)):
                    cell.shapes(layer).erase(shape)
        top.insert(kdb.CellInstArray(cell.cell_index(), kdb.Trans(k * 2000, 0)))
    return _write(out, tmp_path, "lib.gds")


_LIB_REF = (
    ".SUBCKT U S G D\nM0 D G S S nmos_rvt w=81n l=20n nfin=3\n.ENDS\n"
    ".SUBCKT TOP\nX1 vss b c U\nX2 vss e f U\n.ENDS\n"
)


def _lib_request(tmp_path: Path, layout: str, options: dict) -> str:
    ref = tmp_path / "lib.cdl"
    ref.write_text(_LIB_REF)
    return json.dumps(
        {
            "layout": {"file": layout, "deck": "asap7", "top": "TOP"},
            "reference": {"netlist": str(ref), "top": "TOP"},
            "options": options,
        }
    )


def test_lvs_bulk_policy_resolves_sub_cell_rail_labels(tmp_path):
    layout = _two_cell_library(tmp_path)
    options = {
        "finfet_bulk": {"nmos": "S"},
        "flatten_layout": True,
        "flatten_reference": True,
    }
    report = run_lvs(_lib_request(tmp_path, layout, options))
    assert report["status"] == "match", _errors(report)
    tied = [
        m for m in report["mismatches"] if m["category"] == "device.finfet_bulk_tied"
    ]
    assert sorted(m["details"]["circuit"] for m in tied) == ["U1", "U2"]


def test_lvs_bulk_policy_missing_sub_cell_label_names_the_circuit(tmp_path):
    layout = _two_cell_library(tmp_path, with_labels=False)
    options = {"finfet_bulk": {"nmos": "S"}, "flatten_layout": True}
    with pytest.raises(LvsError, match=r"not found in circuit 'U[12]'"):
        run_lvs(_lib_request(tmp_path, layout, options))


def test_lvs_bulk_policy_round_trips_through_rerun_reconstruction(tmp_path):
    from klayout_tools.lvs import _reconstruct_lvs_request

    report = _lvs(tmp_path, _SOURCE_TIED_CARD, options={"finfet_bulk": {"nmos": "S"}})
    request = _reconstruct_lvs_request(report)
    assert request["options"]["finfet_bulk"] == {"nmos": "S"}


def _bulk_circuit(*, bulk_nets: int = 1, named: int = 1):
    """A circuit with ``named`` nets called ``VSS`` and an nFET (``bulk_nets``
    devices, each on its own bulk net when > 1)."""
    from klayout_tools.extract_finfet import finfet_device_class

    netlist = kdb.Netlist()
    cls = finfet_device_class("nmos_rvt")
    netlist.add(cls)
    circuit = kdb.Circuit()
    circuit.name = "T"
    netlist.add(circuit)
    for _ in range(named):
        circuit.create_net("VSS")
    sig = circuit.create_net("sig")
    for k in range(bulk_nets):
        device = circuit.create_device(cls, f"d{k}")
        bulk = circuit.create_net(f"bulk{k}")
        for terminal, net in (("S", sig), ("G", sig), ("D", sig), ("B", bulk)):
            device.connect_terminal(cls.terminal_id(terminal), net)
    return netlist, circuit


def test_apply_finfet_bulk_joins_the_single_bulk_net():
    from klayout_tools.lvs_finfet import apply_finfet_bulk

    netlist, circuit = _bulk_circuit()
    (record,) = apply_finfet_bulk(circuit, {"nmos": "VSS"})
    assert record["status"] == "joined" and record["device_count"] == 1
    device = next(circuit.each_device())
    bulk = device.net_for_terminal(device.device_class().terminal_id("B"))
    assert "VSS" in bulk.name.split(",")
    assert netlist is not None


def test_apply_finfet_bulk_missing_polarity_devices_is_disclosed_not_silent():
    from klayout_tools.lvs_finfet import apply_finfet_bulk

    _, circuit = _bulk_circuit()
    (record,) = apply_finfet_bulk(circuit, {"pmos": "VSS"})
    assert record["status"] == "no_devices" and record["device_count"] == 0


def test_apply_finfet_bulk_refuses_ambiguous_target_names():
    from klayout_tools.lvs_finfet import apply_finfet_bulk

    _, circuit = _bulk_circuit(named=2)
    with pytest.raises(LvsError, match="ambiguous"):
        apply_finfet_bulk(circuit, {"nmos": "VSS"})


def test_apply_finfet_bulk_refuses_bulk_terminals_on_several_nets():
    from klayout_tools.lvs_finfet import apply_finfet_bulk

    _, circuit = _bulk_circuit(bulk_nets=2)
    with pytest.raises(LvsError, match="different nets"):
        apply_finfet_bulk(circuit, {"nmos": "VSS"})


def test_apply_finfet_bulk_refuses_missing_target():
    from klayout_tools.lvs_finfet import apply_finfet_bulk

    _, circuit = _bulk_circuit(named=0)
    with pytest.raises(LvsError, match="not found"):
        apply_finfet_bulk(circuit, {"nmos": "VSS"})


def test_slice_spice_to_top_keeps_only_the_cell_and_its_callees():
    from klayout_tools.lvs_finfet import slice_spice_to_top

    text = (
        "* c\n.GLOBAL VDD\n.SUBCKT A x\nX1 x B\n.ENDS\n"
        ".SUBCKT B y\n.ENDS\n.SUBCKT C z\n.ENDS\n"
    )
    out = slice_spice_to_top(text, "a")
    assert ".SUBCKT A" in out and ".SUBCKT B" in out and ".SUBCKT C" not in out
    assert ".GLOBAL VDD" in out
    assert slice_spice_to_top(text, "missing") == text
    assert slice_spice_to_top(text, None) == text


def test_cli_lvs_json_envelope_for_finfet_match_and_error(tmp_path, capsys):
    layout = _write(_draw_device(), tmp_path)
    ref = tmp_path / "ref.cdl"
    ref.write_text(".SUBCKT TOP S G D\n" + _NCARD.format(n=3) + "\n.ENDS\n")
    request = tmp_path / "req.json"
    request.write_text(
        json.dumps(
            {
                "layout": {"file": layout, "deck": "asap7", "top": "TOP"},
                "reference": {"netlist": str(ref), "top": "TOP"},
            }
        )
    )
    rc = main(["lvs", str(request), "--format", "json"])
    report = json.loads(capsys.readouterr().out)
    assert rc == 0 and report["status"] == "match"
    assert isinstance(report["schema_version"], int)

    ref.write_text(".SUBCKT TOP S G D\nM0 D G S vsubs nmos_rvt w=81n l=20n\n.ENDS\n")
    rc = main(["lvs", str(request), "--format", "json"])
    captured = capsys.readouterr()
    assert rc == 1 and captured.out == ""
    assert "nfin" in json.loads(captured.err)["error"]["message"]


# --------------------------------------------------------------------------- #
# Pinned ASAP7 corpus (lambdapdk v0.2.17)
# --------------------------------------------------------------------------- #

_LIBS = (
    Path(__file__).resolve().parent.parent
    / "pdks"
    / "lambdapdk"
    / "lambdapdk"
    / "asap7"
    / "libs"
)
_RVT_GDS = _LIBS / "asap7sc7p5t_rvt" / "gds" / "asap7sc7p5t_28_R.gds.gz"
_RVT_CDL = _LIBS / "asap7sc7p5t_rvt" / "netlist" / "asap7sc7p5t_28_R.cdl"
_LVT_GDS = _LIBS / "asap7sc7p5t_lvt" / "gds" / "asap7sc7p5t_28_L.gds.gz"

_needs_corpus = pytest.mark.skipif(
    not (_RVT_GDS.is_file() and _RVT_CDL.is_file() and _LVT_GDS.is_file()),
    reason="pinned lambdapdk ASAP7 libraries not found under pdks/lambdapdk -- "
    "run scripts/fetch-pdks.sh to enable this test",
)


@pytest.fixture(scope="module")
def rvt_layout():
    layout = kdb.Layout()
    layout.read(str(_RVT_GDS))
    return layout


def _cdl_devices(cell: str) -> collections.Counter:
    text = _RVT_CDL.read_text()
    match = re.search(rf"^\.SUBCKT {re.escape(cell)} .*?^\.ENDS", text, re.S | re.M)
    assert match, cell
    found: collections.Counter = collections.Counter()
    for line in match.group(0).splitlines():
        m = re.match(r"^M\S+ \S+ \S+ \S+ \S+ (\S+) .*l=(\S+)n nfin=(\d+)", line)
        if m:
            found[(m.group(1), int(m.group(3)), float(m.group(2)))] += 1
    return found


def _cdl_combined(cell: str) -> collections.Counter:
    """The CDL's devices after merging exactly-parallel ones -- the same rule
    the extractor applies to fingers."""
    netlist = _read_cdl_netlist(cell)
    circuit = next(netlist.each_circuit())
    return collections.Counter(
        (
            d.device_class().name,
            int(d.parameter("NFIN")),
            round(d.parameter("L") * 1000, 3),
        )
        for d in circuit.each_device()
    )


class _NfinReader(kdb.NetlistSpiceReaderDelegate):
    def element(self, circuit, element, name, model, value, nets, params):
        if element != "M":
            return super().element(circuit, element, name, model, value, nets, params)
        netlist = circuit.netlist()
        cls = netlist.device_class_by_name(model.lower())
        if cls is None:
            cls = finfet_device_class(model.lower())
            netlist.add(cls)
        device = circuit.create_device(cls, name)
        for terminal, net in zip(("D", "G", "S", "B"), nets, strict=True):
            device.connect_terminal(cls.terminal_id(terminal), net)
        device.set_parameter("L", params["L"] * 1e6)
        device.set_parameter("W", params["W"] * 1e6)
        device.set_parameter("NFIN", params["NFIN"])
        device.set_parameter("FINGERS", 1.0)
        return True


def _read_cdl_netlist(cell: str) -> kdb.Netlist:
    netlist = kdb.Netlist()
    netlist.read(str(_RVT_CDL), kdb.NetlistSpiceReader(_NfinReader()))
    for circuit in list(netlist.each_circuit()):
        if circuit.name.upper() != cell.upper():
            netlist.remove(circuit)
    netlist.combine_devices()
    return netlist


def _extracted(layout: kdb.Layout, cell: str) -> kdb.Netlist:
    netlist, _ = extract_finfet_netlist(layout, layout.cell(cell), EXTRACTION_DECK)
    return netlist


@_needs_corpus
@pytest.mark.parametrize(
    "cell",
    [
        "INVx1_ASAP7_75t_R",
        "INVx2_ASAP7_75t_R",
        "INVx13_ASAP7_75t_R",
        "NAND2xp5_ASAP7_75t_R",
        "BUFx2_ASAP7_75t_R",
        "DFFHQNx1_ASAP7_75t_R",
    ],
)
def test_corpus_device_multiset_matches_cdl(rvt_layout, cell):
    netlist = _extracted(rvt_layout, cell)
    circuit = next(netlist.each_circuit())
    got = collections.Counter(
        (
            d.device_class().name,
            int(d.parameter("NFIN")),
            round(d.parameter("L") * 1000, 3),
        )
        for d in circuit.each_device()
    )
    assert got == _cdl_combined(cell)


@_needs_corpus
def test_corpus_inv_x2_is_one_device_of_two_fingers(rvt_layout):
    netlist = _extracted(rvt_layout, "INVx2_ASAP7_75t_R")
    circuit = next(netlist.each_circuit())
    devices = sorted(
        (d.device_class().name, int(d.parameter("NFIN")), int(d.parameter("FINGERS")))
        for d in circuit.each_device()
    )
    assert devices == [("nmos_rvt", 6, 2), ("pmos_rvt", 6, 2)]
    assert _cdl_devices("INVx2_ASAP7_75t_R") == collections.Counter(
        {("nmos_rvt", 6, 20.0): 1, ("pmos_rvt", 6, 20.0): 1}
    )


def _compare_with_cdl(layout: kdb.Layout, cell: str) -> bool:
    """Net-level compare, NFIN exact. The CDL ties bulk to VDD/VSS while the
    cells draw no taps, so (comparison-side only) the well net joins VDD and
    the synthesized substrate joins VSS -- the LVS policy #2813 must make
    explicit."""
    extracted = _extracted(layout, cell)
    reference = _read_cdl_netlist(cell)
    lc = next(extracted.each_circuit())
    rc = next(reference.each_circuit())
    lc.name = rc.name
    by_name: dict[str, list] = {}
    for net in list(lc.each_net()):
        by_name.setdefault(net.name, []).append(net)
    for group in (
        by_name.get("VDD", []),
        by_name.get("VSS", []) + by_name.get("vsubs", []),
    ):
        for other in group[1:]:
            lc.join_nets(group[0], other)
    for netlist in (extracted, reference):
        for device_class in netlist.each_device_class():
            device_class.equal_parameters = kdb.EqualDeviceParameters(
                device_class.parameter_id("NFIN"), 0.0, 0.0
            ) + kdb.EqualDeviceParameters(device_class.parameter_id("L"), 1e-5, 0.0)
    return kdb.NetlistComparer().compare(extracted, reference)


@_needs_corpus
@pytest.mark.parametrize(
    "cell",
    [
        "INVx1_ASAP7_75t_R",
        "NAND2xp5_ASAP7_75t_R",
        # LISD spans a gate and abuts the stack node: needs SDT as contact.
        "AND2x2_ASAP7_75t_R",
        # LIG/LISD overlap with no V0: needs lig_lisd_connected.
        "DFFHQNx2_ASAP7_75t_R",
        "ICGx2_ASAP7_75t_R",
    ],
)
def test_corpus_cells_match_cdl_at_net_level(rvt_layout, cell):
    assert _compare_with_cdl(rvt_layout, cell)


@_needs_corpus
def test_corpus_altered_nfin_fails_the_same_comparison(rvt_layout):
    """Negative control for the harness above: identical connectivity, one
    device's fin count changed on the extracted side -> no match."""
    extracted = _extracted(rvt_layout, "INVx1_ASAP7_75t_R")
    circuit = next(extracted.each_circuit())
    device = next(circuit.each_device())
    device.set_parameter("NFIN", device.parameter("NFIN") + 1)
    reference = _read_cdl_netlist("INVx1_ASAP7_75t_R")
    circuit.name = next(reference.each_circuit()).name
    for netlist in (extracted, reference):
        for device_class in netlist.each_device_class():
            device_class.equal_parameters = kdb.EqualDeviceParameters(
                device_class.parameter_id("NFIN"), 0.0, 0.0
            )
    assert not kdb.NetlistComparer().compare(extracted, reference)


@_needs_corpus
def test_corpus_lvt_library_extracts_lvt_models():
    layout = kdb.Layout()
    layout.read(str(_LVT_GDS))
    netlist = _extracted(layout, "INVx1_ASAP7_75t_L")
    circuit = next(netlist.each_circuit())
    assert sorted(d.device_class().name for d in circuit.each_device()) == [
        "nmos_lvt",
        "pmos_lvt",
    ]


@_needs_corpus
def test_corpus_cli_extract_writes_nfin_cards(tmp_path):
    out = tmp_path / "inv.spice"
    report = run_extract(
        str(_RVT_GDS), "asap7", output=str(out), top="INVx2_ASAP7_75t_R"
    )
    assert report["device_counts"] == {"nmos_rvt": 1, "pmos_rvt": 1}
    assert {d["params"]["nfin"] for d in report["devices"]} == {6}
    cards = [line for line in out.read_text().splitlines() if line.startswith("M")]
    assert len(cards) == 2 and all(card.endswith("NFIN=6") for card in cards)


# --------------------------------------------------------------------------- #
# Pinned ASAP7 corpus through the public `klt lvs` path (issue #2813)
# --------------------------------------------------------------------------- #

_RVT_GDS_SHA256 = "75a7f40e6fa06fcb3d38c4e4a14db2ab715dc6bf382c58e9252ed5f4526777a6"
_RVT_CDL_SHA256 = "3e3e2548fb8f571c2e373dcbf565ecb0288e6433e803fbf06561cb16b70caf53"

#: Cells whose layout draws parallel *series stacks* (distinct internal nodes)
#: that the CDL folds into one wider stack. Reducing one to the other needs
#: general transistor-network normalisation, which is out of scope (and
#: `NetlistComparer.join_symmetric_nets` over-merges ICG cells and segfaulted
#: KLayout 0.30.12): explicitly UNSUPPORTED, reported as mismatches.
_FOLDED_STACK_CELLS = frozenset(
    f"{name}_ASAP7_75t_R"
    for name in (
        "AND2x4 AND2x6 AOI21x1 AOI22x1 NAND3x2 NOR3x2 OAI21x1 OAI22x1 "
        "OR2x6 XNOR2x1 XOR2x1"
    ).split()
)
#: Layout and CDL differ only in series order inside a stack -- logically
#: equivalent, topologically different. Adjudication (see the tests below):
#: reference-vs-layout series-order difference, NOT an extraction defect.
_COMBINATIONAL_SERIES_ORDER_CELLS = (
    "A2O1A1O1Ixp25_ASAP7_75t_R",
    "AOI211xp5_ASAP7_75t_R",
    "OAI221xp5_ASAP7_75t_R",
)
_SCAN_FLOP_SERIES_ORDER_CELLS = (
    "SDFLx1_ASAP7_75t_R",
    "SDFLx2_ASAP7_75t_R",
    "SDFLx3_ASAP7_75t_R",
)
_SERIES_ORDER_CELLS = frozenset(
    _COMBINATIONAL_SERIES_ORDER_CELLS + _SCAN_FLOP_SERIES_ORDER_CELLS
)
#: No devices: inline extraction yields an empty netlist, no circuit to compare.
_NO_DEVICE_CELLS = frozenset({"TAPCELL_ASAP7_75t_R"})
_UNSUPPORTED_CELLS = _FOLDED_STACK_CELLS | _SERIES_ORDER_CELLS | _NO_DEVICE_CELLS

_BULK_POLICY = {"nmos": "VSS", "pmos": "VDD"}


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _corpus_request(cell: str, cdl: Path | str = _RVT_CDL, options=None) -> dict:
    return {
        "layout": {"file": str(_RVT_GDS), "deck": "asap7", "top": cell},
        "reference": {"netlist": str(cdl), "top": cell},
        "options": {"finfet_bulk": dict(_BULK_POLICY), **(options or {})},
    }


def _corpus_lvs(cell: str, cdl: Path | str = _RVT_CDL, options=None) -> dict:
    return run_lvs(json.dumps(_corpus_request(cell, cdl, options)))


def _cdl_block(cell: str) -> str:
    text = _RVT_CDL.read_text()
    match = re.search(rf"^\.SUBCKT {re.escape(cell)} .*?^\.ENDS", text, re.S | re.M)
    assert match, cell
    return match.group(0) + "\n"


@_needs_corpus
def test_corpus_asset_hashes_match_the_pinned_inputs():
    assert _sha256(_RVT_GDS) == _RVT_GDS_SHA256
    assert _sha256(_RVT_CDL) == _RVT_CDL_SHA256


@_needs_corpus
@pytest.mark.parametrize(
    "cell",
    [
        "INVx1_ASAP7_75t_R",
        "INVx2_ASAP7_75t_R",
        "NAND2xp5_ASAP7_75t_R",
        "AND2x2_ASAP7_75t_R",
        "BUFx2_ASAP7_75t_R",
        "DFFHQNx2_ASAP7_75t_R",
        "ICGx2_ASAP7_75t_R",
    ],
)
def test_corpus_public_path_matches_reference_cdl(cell):
    report = _corpus_lvs(cell)
    assert report["status"] == "match", [
        m for m in report["mismatches"] if m["severity"] == "error"
    ][:5]
    assert report["options"]["finfet_bulk"] == _BULK_POLICY
    tied = [
        m for m in report["mismatches"] if m["category"] == "device.finfet_bulk_tied"
    ]
    assert {m["details"]["polarity"] for m in tied} == {"nmos", "pmos"}
    assert all(m["severity"] == "warning" for m in tied)


@_needs_corpus
def test_corpus_without_the_bulk_policy_does_not_silently_tie_bulk():
    request = _corpus_request("INVx1_ASAP7_75t_R")
    del request["options"]["finfet_bulk"]
    report = run_lvs(json.dumps(request))
    assert report["status"] == "mismatch"
    assert report["options"]["finfet_bulk"] is None


@_needs_corpus
def test_corpus_negative_controls_are_actionable(tmp_path):
    cell = "NAND2xp5_ASAP7_75t_R"
    block = _cdl_block(cell)

    def run(text: str) -> dict:
        path = tmp_path / "ref.cdl"
        path.write_text(text)
        return _corpus_lvs(cell, path)

    assert run(block)["status"] == "match"

    nfin = run(block.replace("nfin=3", "nfin=4", 1))
    assert nfin["status"] == "mismatch"
    props = [m for m in _errors(nfin) if m["category"] == "device.property"]
    assert props and props[0]["property"]["name"] == "nfin"

    vt = run(block.replace("nmos_rvt", "nmos_lvt", 1))
    assert vt["status"] == "mismatch"
    assert any(m["category"] == "device.class" for m in _errors(vt))

    # Gate and drain exchanged on one device: not a legal source/drain swap.
    swapped = re.sub(
        r"^(MM2 )(Y) (B) (net16)", r"\1\3 \2 \4", block, count=1, flags=re.M
    )
    assert swapped != block
    result = run(swapped)
    assert result["status"] == "mismatch" and _errors(result)


@_needs_corpus
@pytest.mark.parametrize("cell", sorted(_COMBINATIONAL_SERIES_ORDER_CELLS))
def test_corpus_combinational_series_order_cells_are_logically_equivalent(
    rvt_layout, cell
):
    """Adjudication: the layout and CDL stacks order the same series devices
    differently; switch-level truth tables over every input assignment are
    identical and fully driven, so this is a reference/layout topology
    difference, not an extraction defect."""
    import itertools

    def devices(netlist):
        circuit = next(netlist.each_circuit())
        found = []
        for device in circuit.each_device():
            cls = device.device_class()
            nets = {
                t: device.net_for_terminal(cls.terminal_id(t))
                .expanded_name()
                .split(",")[0]
                .upper()
                for t in ("G", "S", "D")
            }
            found.append((cls.name.startswith("nmos"), nets["G"], nets["S"], nets["D"]))
        return found

    def find(parent, x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def table(found, inputs):
        result = {}
        for bits in itertools.product((0, 1), repeat=len(inputs)):
            env = dict(zip(inputs, bits, strict=True))
            parent: dict[str, str] = {}
            for is_n, gate, source, drain in found:
                if (env[gate] == 1) == is_n:
                    parent[find(parent, source)] = find(parent, drain)
            out = find(parent, "Y")
            result[bits] = (find(parent, "VSS") == out, find(parent, "VDD") == out)
        return result

    layout_devices = devices(_extracted(rvt_layout, cell))
    reference_devices = devices(_read_cdl_netlist(cell))
    inputs = sorted({g for _, g, _, _ in reference_devices} - {"VDD", "VSS", "Y"})
    layout_table = table(layout_devices, inputs)
    assert layout_table == table(reference_devices, inputs)
    # Every assignment drives Y to exactly one rail (no float, no contention).
    assert all(
        pulled_low != pulled_high for pulled_low, pulled_high in layout_table.values()
    )
    # ... and the public path still reports them as different topologies.
    assert _corpus_lvs(cell)["status"] == "mismatch"


@_needs_corpus
@pytest.mark.parametrize("cell", sorted(_SCAN_FLOP_SERIES_ORDER_CELLS))
def test_corpus_scan_flop_difference_is_one_series_swap_in_the_scan_mux(tmp_path, cell):
    """Adjudication: exchanging the gates of the two series nFETs (SE, SI) in
    the scan-mux stack of the reference makes the cell match -- the only
    difference is series order."""
    block = _cdl_block(cell)
    assert _corpus_lvs(cell)["status"] == "mismatch"
    lines = block.splitlines()
    swapped = 0
    for i, line in enumerate(lines):
        if re.match(r"MM(5|26) ", line) and re.search(r" (SE|SI) ", line):
            lines[i] = re.sub(
                r" (SE|SI) ",
                lambda m: " SI " if m.group(1) == "SE" else " SE ",
                line,
                count=1,
            )
            swapped += 1
    assert swapped == 2
    path = tmp_path / "ref.cdl"
    path.write_text("\n".join(lines) + "\n")
    assert _corpus_lvs(cell, path)["status"] == "match"


@_needs_corpus
@pytest.mark.parametrize("cell", ["AOI21x1_ASAP7_75t_R", "XOR2x1_ASAP7_75t_R"])
def test_corpus_folded_stack_cells_are_reported_as_mismatches_not_normalised(cell):
    report = _corpus_lvs(cell)
    assert cell in _FOLDED_STACK_CELLS
    assert report["status"] == "mismatch" and _errors(report)


_SWEEP_SCRIPT = """
import json, re, sys
from klayout_tools import build_identity
from klayout_tools.lvs import LvsError, run_lvs

build_identity._checkout_identity = lambda *a, **k: {
    "git_commit": None, "git_tag": None, "dirty": None, "is_release": False}
gds, cdl, out = sys.argv[1:4]
cells = re.findall(r"^\\.SUBCKT (\\S+)", open(cdl).read(), re.M | re.I)
results = {}
for cell in cells:
    request = {
        "layout": {"file": gds, "deck": "asap7", "top": cell},
        "reference": {"netlist": cdl, "top": cell},
        "options": {"finfet_bulk": {"nmos": "VSS", "pmos": "VDD"}},
    }
    try:
        results[cell] = run_lvs(json.dumps(request))["status"]
    except LvsError as exc:
        results[cell] = "error: " + str(exc)
json.dump(results, open(out, "w"))
"""


@_needs_corpus
def test_corpus_full_rvt_library_via_klt_lvs_in_a_checked_subprocess(tmp_path):
    """Every CDL cell through ``run_lvs`` in one subprocess (so a native
    KLayout crash fails this test instead of killing pytest): return status
    checked, timeout enforced, ICG cells included."""
    import subprocess
    import sys

    if (_sha256(_RVT_GDS), _sha256(_RVT_CDL)) != (_RVT_GDS_SHA256, _RVT_CDL_SHA256):
        pytest.skip("pinned RVT assets do not match the recorded sha256 hashes")
    script_dir = tmp_path / "script"
    script_dir.mkdir()
    script = script_dir / "sweep.py"
    script.write_text(_SWEEP_SCRIPT)
    out = tmp_path / "results.json"
    completed = subprocess.run(
        [sys.executable, "-I", str(script), str(_RVT_GDS), str(_RVT_CDL), str(out)],
        capture_output=True,
        text=True,
        timeout=3600,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    results = json.loads(out.read_text())

    assert len(results) == 209
    assert any(cell.startswith("ICG") for cell in results)
    supported = {c: r for c, r in results.items() if c not in _UNSUPPORTED_CELLS}
    assert len(supported) == 209 - len(_UNSUPPORTED_CELLS) == 191
    assert {r for r in supported.values()} == {"match"}, {
        c: r for c, r in supported.items() if r != "match"
    }
    # The unsupported list must not go stale: each of these still fails.
    for cell in _FOLDED_STACK_CELLS | _SERIES_ORDER_CELLS:
        assert results[cell] == "mismatch", cell
    assert results["TAPCELL_ASAP7_75t_R"].startswith("error: ")


# --------------------------------------------------------------------------- #
# klt lvs: gate-level-verilog reference on a FinFET deck (issue #4462)
# --------------------------------------------------------------------------- #

_CELL_CDL = ".SUBCKT MYN G D S VSS\nM0 D G S VSS nmos_rvt w=81n l=20n nfin={n}\n.ENDS\n"
_TOP_V = (
    "module TOP (S, G, D);\n input S, G; output D;\n"
    " MYN u0 (.G(G), .D(D), .S(S));\nendmodule\n"
)


def _lvs_gate_level(
    tmp_path: Path,
    *,
    cdl_fins=3,
    layout_fins=3,
    verilog=_TOP_V,
    options=None,
    extra=None,
):
    layout = _write(_draw_device(polarity="n", fins=layout_fins), tmp_path)
    cdl = tmp_path / "lib.cdl"
    cdl.write_text(_CELL_CDL.format(n=cdl_fins))
    ref = tmp_path / "ref.v"
    ref.write_text(verilog)
    request = {
        "layout": {"file": layout, "deck": "asap7", "top": "TOP"},
        "reference": {
            "netlist": str(ref),
            "top": "TOP",
            "form": "gate-level-verilog",
            "pin_order_files": [str(cdl)],
            **(extra or {}),
        },
    }
    if options is not None:
        request["options"] = options
    return run_lvs(json.dumps(request))


def test_lvs_gate_level_verilog_expands_cells_and_matches(tmp_path):
    report = _lvs_gate_level(tmp_path)
    assert report["status"] == "match"
    assert report["power_connectivity"]["status"] == "unchecked"
    assert "transistor-level" in report["power_connectivity"]["reason"]


def test_lvs_gate_level_verilog_changed_library_nfin_is_a_mismatch(tmp_path):
    report = _lvs_gate_level(tmp_path, cdl_fins=2)
    assert report["status"] != "match"
    props = [m for m in _errors(report) if m.get("category") == "device.property"]
    assert props
    assert props[0]["property"]["name"].lower() == "nfin"


def test_lvs_gate_level_verilog_missing_cell_body_is_an_error(tmp_path):
    verilog = _TOP_V.replace("MYN u0", "NOPE u0")
    with pytest.raises(LvsError, match="NOPE"):
        _lvs_gate_level(tmp_path, verilog=verilog)


def test_lvs_gate_level_verilog_still_refuses_device_map(tmp_path):
    with pytest.raises(LvsError, match="device_map"):
        _lvs_gate_level(tmp_path, extra={"device_map": {"a": "b"}})
