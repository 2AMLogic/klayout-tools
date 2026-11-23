"""Single-top PEX disclosure of the substrate ties in the extracted netlist."""

import pytest

from helpers.tool_probes import SKIP_NO_NGSPICE
from klayout_tools import pex
from test_extract import _make_floating_substrate_layout
from test_pex import (
    _make_sky130_poly_resistor_layout,
    _write_gds,
    _write_request,
    _write_schematic_dut,
    _write_testbench,
)


@pytest.mark.parametrize(
    ("requested", "resistance", "mode"),
    [
        (None, 1e12, "shunt"),
        (1200, 1200, "shunt"),
        (0.001, 0.001, "shunt"),
        (0, 0.001, "hard"),
    ],
)
def test_extract_only_discloses_written_spice_card(
    tmp_path, requested, resistance, mode
):
    layout = _write_gds(_make_floating_substrate_layout(), tmp_path / "floating.gds")
    netlist = tmp_path / "extracted.spice"
    result = pex.run_pex(
        layout,
        [],
        "sky130",
        extract_only=True,
        output=str(netlist),
        substrate_tie_ohms=requested,
    )
    tie = result["extraction"]["substrate_dc_tie"]
    assert tie["nets"] == [{"net": "vsubs", "device": "Rvsubs_dctie"}]
    assert tie["resistance_ohm"] == resistance and tie["mode"] == mode
    assert tie["reference_node"] == "0"
    entry = tie["nets"][0]
    card = f"{entry['device']} {entry['net']} {tie['reference_node']} {resistance:g}"
    assert card in netlist.read_text().splitlines()


@SKIP_NO_NGSPICE
def test_comparison_reports_actual_ties(tmp_path):
    layout = _write_gds(_make_sky130_poly_resistor_layout(), tmp_path / "res.gds")
    dut = _write_schematic_dut(tmp_path / "dut.spice")
    bench = _write_testbench(tmp_path / "bench.spice", dut)
    request = _write_request(tmp_path / "request.json", bench)
    netlist = tmp_path / "extracted.spice"
    result = pex.run_pex(layout, [str(request)], "sky130", output=str(netlist))
    assert result["status"] == "pass"
    tie = result["extraction"]["substrate_dc_tie"]
    assert tie["nets"]
    cards = netlist.read_text().splitlines()
    reference = tie["reference_node"]
    for net in tie["nets"]:
        pins = f"{net['net']} {reference}"
        assert f"{net['device']} {pins} {tie['resistance_ohm']:g}" in cards


@pytest.mark.parametrize(
    "metadata", [None, {"nets": [], "resistance_ohm": 1e12, "mode": "shunt"}]
)
def test_empty_and_absent_metadata_remain_distinct(tmp_path, monkeypatch, metadata):
    layout = _write_gds(_make_floating_substrate_layout(), tmp_path / "floating.gds")
    extract = pex.pex_runset.run_extract

    def override_metadata(*args, **kwargs):
        report = extract(*args, **kwargs)
        parasitics = report["parasitics"]
        if metadata is None:
            parasitics.pop("substrate_dc_tie")
        else:
            parasitics["substrate_dc_tie"] = metadata
        return report

    monkeypatch.setattr(pex.pex_runset, "run_extract", override_metadata)
    report = pex.run_pex(
        layout, [], "sky130", extract_only=True, output=str(tmp_path / "ext.spice")
    )
    if metadata is None:
        assert "substrate_dc_tie" not in report["extraction"]
    else:
        assert report["extraction"]["substrate_dc_tie"] == {
            **metadata,
            "reference_node": "0",
        }
