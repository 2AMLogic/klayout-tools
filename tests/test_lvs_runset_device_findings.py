"""Device-pair diagnostics from the KLayout runset cross-reference (#7856)."""

from unittest.mock import Mock

import klayout.db as kdb

from helpers import lvs_runset_fixtures as fx
from klayout_tools.lvs import run_lvs
from klayout_tools.lvs_runset_findings import _device_findings


def test_shorted_gate_with_equal_dimensions_reports_connectivity(tmp_path, monkeypatch):
    fx.install_fake_klayout(monkeypatch, tmp_path)
    # Connect gate to drain in the reference only. The fixture extracts the
    # transistor from GDS, compares it, writes a real .lvsdb and reads it back.
    shorted = fx.SPICE_ONE.replace("M1 c b a d", "M1 c c a d")
    report = run_lvs(fx.request(tmp_path, 1, spice=shorted))

    assert report["status"] == "mismatch"
    assert report["error_count"] == 8
    assert report["category_error_counts"] == {
        "device.connectivity": 1,
        "net.unmatched": 7,
    }
    (finding,) = (
        m for m in report["mismatches"] if m["category"] == "device.connectivity"
    )
    assert finding["side"] == "both"
    assert "terminal connectivity" in finding["description"]
    assert finding["details"] == {
        "layout_parameters": {"L": 0.2, "W": 0.4},
        "reference_parameters": {"L": 0.2, "W": 0.4},
    }


def test_paired_devices_with_different_primary_parameters_keep_property_finding():
    nl = kdb.Netlist()
    dc = kdb.DeviceClassMOS4Transistor()
    dc.name = "NMOS"
    nl.add(dc)
    circuit = kdb.Circuit()
    nl.add(circuit)
    layout = circuit.create_device(dc, "layout_mos")
    reference = circuit.create_device(dc, "reference_mos")
    layout.set_parameter("W", 1.0)
    reference.set_parameter("W", 2.0)
    pair = Mock()
    pair.first.return_value = layout
    pair.second.return_value = reference
    pair.status.return_value = kdb.NetlistCrossReference.Mismatch
    xref = Mock()
    xref.each_device_pair.return_value = [pair]

    (finding,) = _device_findings(xref, object(), {"layout": "TOP", "reference": "TOP"})
    assert finding["category"] == "device.property"
    assert finding["severity"] == "error"
    assert finding["side"] == "both"
    assert finding["description"] == (
        "devices 'layout_mos' / 'reference_mos' were paired but do not compare equal"
    )
    assert finding["details"] == {
        "layout_parameters": {"L": 0.0, "W": 1.0},
        "reference_parameters": {"L": 0.0, "W": 2.0},
    }
