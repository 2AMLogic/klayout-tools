"""Unnamed device classes in the ``klayout-runset`` census (issue #5407)."""

from __future__ import annotations

import klayout.db as kdb

from klayout_tools import lvs_runset_census
from klayout_tools.coverage import coverage_validation_error


def _netlist_with_classes(*names: str) -> kdb.Netlist:
    nl = kdb.Netlist()
    circuit = kdb.Circuit()
    circuit.name = "TOP"
    nl.add(circuit)
    for name in names:
        dc = kdb.DeviceClassMOS4Transistor()
        dc.name = name
        nl.add(dc)
        circuit.create_device(dc, "D")
    return nl


def test_unnamed_device_class_is_reported_not_raised():
    layout = _netlist_with_classes("NMOS", "")
    ref = _netlist_with_classes("NMOS")
    ld, lc, lu = lvs_runset_census._class_census(layout)
    rd, rc, ru = lvs_runset_census._class_census(ref)
    assert (lc, lu, ru) == ({"NMOS": 1}, 1, 0)
    assert "" not in ld
    cov = lvs_runset_census._build_coverage(ld, rd, lc, rc, lu + ru)
    assert coverage_validation_error(cov) in (None, "")
    assert cov["checked"] == ["NMOS"]
    assert {"id": "<unnamed>", "reason": "unnamed_device_class"} in cov["skipped"]
    assert cov["unnamed_device_instances"] == 1


def test_all_unnamed_classes_still_nothing_checked():
    nl = _netlist_with_classes("")
    d, c, u = lvs_runset_census._class_census(nl)
    assert (d, c, u) == ([], {}, 1)
    cov = lvs_runset_census._build_coverage(d, d, c, c, u)
    assert cov["nothing_checked"] is True
    assert coverage_validation_error(cov) in (None, "")
