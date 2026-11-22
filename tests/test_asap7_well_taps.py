"""Physical n-well taps in the ASAP7 extraction stack."""

import klayout.db as kdb
import pytest

from klayout_tools.decks.asap7 import EXTRACTION_DECK
from klayout_tools.extract_finfet import extract_finfet_netlist
from test_asap7_finfet import (
    ACTIVE,
    LISD,
    M1,
    M1_PIN,
    NSEL,
    SDT,
    V0,
    WELL,
    WELL_PIN,
    _draw_device,
)
from test_asap7_routed_block import _GDS


def _pmos(circuit):
    return [
        d for d in circuit.each_device() if d.device_class().name.startswith("pmos")
    ]


def _terminal(device, name):
    return device.net_for_terminal(device.device_class().terminal_id(name))


@pytest.mark.parametrize("cut_second", [False, True])
def test_two_well_islands_need_individual_contacts(cut_second):
    layout = _draw_device(polarity="p", fins=2)
    device = layout.cell("TOP")
    device.name = "PMOS"
    well_labels = device.shapes(layout.layer(*WELL_PIN))
    well_labels.clear()
    well_labels.insert(kdb.Text("VDD", 10, 600))
    top = layout.create_cell("PAIR")
    for n, x in enumerate((0, 2000)):
        top.insert(kdb.CellInstArray(device.cell_index(), kdb.Trans(x, 0)))
        # Extend this device's own VDD-labelled well to a distinct N+ active tap.
        top.shapes(layout.layer(*WELL)).insert(kdb.Box(x + 700, 0, x + 1120, 650))
        contact = kdb.Box(x + 910, 100, x + 970, 180)
        top.shapes(layout.layer(*ACTIVE)).insert(contact)
        top.shapes(layout.layer(*NSEL)).insert(kdb.Box(x + 880, 70, x + 1000, 210))
        if not (cut_second and n == 1):
            top.shapes(layout.layer(*SDT)).insert(contact)
        top.shapes(layout.layer(*LISD)).insert(kdb.Box(x + 910, 100, x + 970, 1030))
        top.shapes(layout.layer(*V0)).insert(kdb.Box(x + 920, 975, x + 960, 1015))
    top.shapes(layout.layer(*M1)).insert(kdb.Box(880, 960, 3000, 1030))
    top.shapes(layout.layer(*M1_PIN)).insert(kdb.Text("VDD", 1500, 990))
    netlist, _ = extract_finfet_netlist(layout, top, EXTRACTION_DECK)
    netlist.flatten()
    circuit = netlist.circuit_by_name("PAIR")
    rail = circuit.net_by_name("VDD")
    assert rail is not None
    devices = _pmos(circuit)
    assert len(devices) == 2
    left, right = sorted(devices, key=lambda d: d.trans.disp.x)
    assert rail.cluster_id > 0
    assert _terminal(left, "B").cluster_id == rail.cluster_id
    assert (_terminal(right, "B").cluster_id == rail.cluster_id) == (not cut_second)
    for d in devices:
        assert _terminal(d, "B").cluster_id > 0
        assert _terminal(d, "S").cluster_id != _terminal(d, "B").cluster_id
        assert _terminal(d, "D").cluster_id != _terminal(d, "B").cluster_id


@pytest.mark.skipif(
    not _GDS.is_file(), reason="requires the pinned lambdapdk ASAP7 GDS"
)
@pytest.mark.parametrize("cut_tap", [False, True])
def test_pinned_tapcell_connects_logic_bulk_to_routed_vdd(cut_tap):
    layout = kdb.Layout()
    layout.read(str(_GDS))
    tap = layout.cell("TAPCELL_ASAP7_75t_R")
    gate = layout.cell("AND2x2_ASAP7_75t_R")
    if cut_tap:
        tap.shapes(layout.layer(*SDT)).clear()
    top = layout.create_cell("WELLTAP_PROBE")
    width = tap.bbox(layout.layer(100, 0)).width()
    top.insert(kdb.CellInstArray(tap.cell_index(), kdb.Trans()))
    top.insert(kdb.CellInstArray(gate.cell_index(), kdb.Trans(width, 0)))
    rail_width = width + gate.bbox(layout.layer(100, 0)).width()
    for name, y in (("VSS", 0), ("VDD", 1080)):
        top.shapes(layout.layer(*M1)).insert(kdb.Box(0, y - 36, rail_width, y + 36))
        top.shapes(layout.layer(*M1_PIN)).insert(kdb.Text(name, width // 2, y))
    netlist, _ = extract_finfet_netlist(layout, top, EXTRACTION_DECK)
    tap_circuit = netlist.circuit_by_name(tap.name)
    assert tap_circuit is None or not list(tap_circuit.each_device())
    netlist.flatten()
    circuit = netlist.circuit_by_name(top.name)
    rail = circuit.net_by_name("VDD")
    assert rail is not None
    devices = _pmos(circuit)
    assert len(devices) == 3
    assert rail.cluster_id > 0
    assert all(
        (_terminal(d, "B").cluster_id == rail.cluster_id) == (not cut_tap)
        for d in devices
    )
