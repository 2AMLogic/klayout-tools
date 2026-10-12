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

from klayout_tools import pdk_capabilities
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


def test_asap7_capabilities_advertise_extraction_only():
    decisions = pdk_capabilities.DECISIONS["asap7"]
    supported = {name for name, d in decisions.items() if d.status == "supported"}
    assert supported == {"extraction"}
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
    [(LVT, "nmos_lvt", "pmos_lvt"), (SLVT, "nmos_slvt", "pmos_slvt")],
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
        {"parasitics": True},
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


def test_klt_lvs_refuses_finfet_deck_instead_of_planar_compare(tmp_path):
    path = _write(_draw_device(), tmp_path)
    ref = tmp_path / "ref.cdl"
    ref.write_text(
        ".SUBCKT TOP S G D\nM0 D G S vsubs nmos_rvt w=81n l=20n nfin=3\n.ENDS\n"
    )
    request = {
        "layout": {"file": path, "deck": "asap7", "top": "TOP"},
        "reference": {"netlist": str(ref), "top": "TOP"},
    }
    with pytest.raises(LvsError, match="#2813"):
        run_lvs(json.dumps(request))


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
