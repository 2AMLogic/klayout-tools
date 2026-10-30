"""Routed multi-cell ASAP7 block through extract / LVS / ERC (issue #4610).

Two pinned ``INVx1`` cells are abutted in a ``TOP`` and wired ``Y -> A`` with
hand-drawn M1 (or M2 + V1) geometry, then taken through the same public
paths a routed block takes. The fixture is generated from the pinned lambdapdk
corpus; every test skips with a reason until ``scripts/fetch-pdks.sh`` has
fetched it.
"""

from __future__ import annotations

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.decks.asap7 import EXTRACTION_DECK
from klayout_tools.erc import run_erc
from klayout_tools.extract_finfet import extract_finfet_netlist
from klayout_tools.lvs import run_lvs

_RVT = (
    Path(__file__).resolve().parent.parent
    / "pdks/lambdapdk/lambdapdk/asap7/libs/asap7sc7p5t_rvt"
)
_GDS = _RVT / "gds" / "asap7sc7p5t_28_R.gds.gz"
_CELL = "INVx1_ASAP7_75t_R"
_PITCH = 648  # the cell's 100/0 boundary width in dbu: abutting rails touch

pytestmark = pytest.mark.skipif(
    not _GDS.is_file(),
    reason="pinned lambdapdk ASAP7 libraries not found under pdks/lambdapdk -- "
    "run scripts/fetch-pdks.sh to enable this test",
)

_REF = (
    ".SUBCKT INV A VDD VSS Y\n"
    "M0 Y A VSS VSS nmos_rvt w=81n l=20n nfin=3\n"
    "M1 Y A VDD VDD pmos_rvt w=81n l=20n nfin=3\n.ENDS\n"
    ".SUBCKT TOP IN OUT VDD VSS\n"
    "X1 IN VDD VSS N INV\nX2 N VDD VSS OUT INV\n.ENDS\n"
)
_OPTIONS = {
    "finfet_bulk": {"nmos": "VSS", "pmos": "VDD"},
    "flatten_layout": True,
    "flatten_reference": True,
}


def _routed(tmp_path: Path, *, route: str, top_labels: bool = True) -> str:
    """``route``: ``m1`` (one M1 bar), ``m2`` (M2 bar on V1s) or ``none``."""
    layout = kdb.Layout()
    layout.read(str(_GDS))
    inv = layout.cell(_CELL)
    top = layout.create_cell("TOP")
    for k in range(2):
        top.insert(kdb.CellInstArray(inv.cell_index(), kdb.Trans(k * _PITCH, 0)))
    # U1.Y (M1 x 376..576) to U2.A (M1 x 720..960), both at y 504..576.
    if route == "m1":
        top.shapes(layout.layer(19, 0)).insert(kdb.Box(500, 504, 800, 576))
    elif route == "m2":
        top.shapes(layout.layer(21, 0)).insert(kdb.Box(440, 504, 512, 576))
        top.shapes(layout.layer(21, 0)).insert(kdb.Box(760, 504, 832, 576))
        top.shapes(layout.layer(20, 0)).insert(kdb.Box(430, 504, 842, 576))
    if top_labels:
        label = layout.layer(19, 251)
        top.shapes(label).insert(kdb.Text("IN", 188, 546))
        top.shapes(label).insert(kdb.Text("OUT", _PITCH + 542, 458))
    path = tmp_path / f"routed_{route}.gds"
    layout.write(str(path))
    return str(path)


def _lvs(tmp_path: Path, gds: str) -> dict:
    ref = tmp_path / "ref.cdl"
    ref.write_text(_REF)
    return run_lvs(
        json.dumps(
            {
                "layout": {"file": gds, "deck": "asap7", "top": "TOP"},
                "reference": {"netlist": str(ref), "top": "TOP"},
                "options": _OPTIONS,
            }
        )
    )


@pytest.mark.parametrize("route", ["m1", "m2"])
@pytest.mark.parametrize("top_labels", [True, False])
def test_routed_block_reaches_lvs_match(tmp_path, route, top_labels):
    report = _lvs(tmp_path, _routed(tmp_path, route=route, top_labels=top_labels))
    assert report["status"] == "match", report["mismatches"]
    # The tie and the flatten are disclosed, never silent.
    assert report["category_counts"]["device.finfet_bulk_tied"] == 2
    assert report["category_counts"]["topology.flattened"] == 2


def test_unrouted_block_is_a_real_mismatch(tmp_path):
    """The first failing step of the bisect: with no Y -> A wire the compare
    fails on the devices, so the tooling reports the layout's fault."""
    report = _lvs(tmp_path, _routed(tmp_path, route="none"))
    assert report["status"] == "mismatch"
    assert report["category_counts"].get("device.unmatched", 0) > 0


def _top_pins(gds: str, extra_well_label: bool = False) -> list[str]:
    layout = kdb.Layout()
    layout.read(gds)
    if extra_well_label:
        top = layout.cell("TOP")
        top.shapes(layout.layer(1, 251)).insert(kdb.Text("VDD", 616, 748))
        top.shapes(layout.layer(19, 251)).insert(kdb.Text("VDD", 144, 1070))
    netlist, _ = extract_finfet_netlist(layout, layout.cell("TOP"), EXTRACTION_DECK)
    return sorted(p.name() for p in netlist.circuit_by_name("TOP").each_pin())


def test_pins_come_only_from_labels_on_the_deck_label_layers(tmp_path):
    """No label layer, no pin: a routed stream whose pin text is not on
    ``<metal>/251`` extracts a TOP with only the cell-internal rail labels."""
    labelled = _top_pins(_routed(tmp_path, route="m1"))
    bare = _top_pins(_routed(tmp_path, route="m1", top_labels=False))
    assert {"IN", "OUT"} <= set(labelled)
    assert "IN" not in bare and "OUT" not in bare


def test_well_and_rail_with_one_name_stay_separate_nets(tmp_path):
    """Same-name labels on the well (1/251) and an M1 rail do not join at
    extraction; ``options.finfet_bulk`` is what ties them for the compare."""
    pins = _top_pins(_routed(tmp_path, route="m1"), extra_well_label=True)
    assert pins.count("VDD") == 2


_ERC_SPEC = {
    "stackup": [
        {"name": "gate", "layer": "7/0", "role": "gate"},
        {"name": "lig", "layer": "16/0"},
        {"name": "m1", "layer": "19/0", "label_layer": "19/251"},
        {"name": "m2", "layer": "20/0"},
    ],
    "vias": [
        {"name": "v0", "layer": "18/0", "between": ["lig", "m1"]},
        {"name": "v1", "layer": "21/0", "between": ["m1", "m2"]},
    ],
}


def _erc(tmp_path: Path, spec: dict) -> dict:
    gds = _routed(tmp_path, route="m1")
    spec_path = tmp_path / "erc.json"
    spec_path.write_text(json.dumps(spec))
    return run_erc(gds, str(spec_path), top="TOP", findings_only=True)


def test_erc_floating_gate_is_not_meaningful_on_a_stackup_only_spec(tmp_path):
    """The spec has no way to say 'LIG lands on the gate' (no via layer), so
    every gate poly reads floating -- a spec limitation, not a layout fault."""
    report = _erc(tmp_path, _ERC_SPEC)
    rules = [f["rule"] for f in report["erc_findings"]]
    assert rules and set(rules) == {"erc.floating_gate"}
    assert report["gate_count"] == len(rules) == 6


def test_erc_gate_contact_workaround_shorts_all_gates_through_lig_rails(tmp_path):
    """Declaring LIG as its own gate-to-LIG 'via' clears floating_gate, but
    the dummy poly (no GCut model in the spec) bridges the LIG rails, so every
    gate collapses to one net carrying VDD, VSS and the signals."""
    spec = json.loads(json.dumps(_ERC_SPEC))
    spec["vias"].insert(
        0, {"name": "gate_contact", "layer": "16/0", "between": ["gate", "lig"]}
    )
    report = _erc(tmp_path, spec)
    assert report["gate_count"] == 1
    assert {"VDD", "VSS"} <= set(report["gates"][0]["net"].split(","))
