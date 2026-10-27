"""Interchangeable same-cell instances on dangling outputs (issue #4296)."""

from __future__ import annotations

import pytest

from klayout_tools import lvs
from klayout_tools.verilog_netlist import VerilogNetlistError, _parse_header

CATEGORY = lvs.CATEGORY_TOPOLOGY_DANGLING_OUTPUT_AMBIGUOUS


class _Net:
    def __init__(self, name, connections=1, pins=0):
        self._name = name
        self._connections = connections
        self._pins = pins

    def expanded_name(self):
        return self._name

    def terminal_count(self):
        return 0

    def subcircuit_pin_count(self):
        return self._connections

    def pin_count(self):
        return self._pins


class _Pin:
    def id(self):
        return 1


class _Named:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name

    def expanded_name(self):
        return self._name

    def each_pin(self):
        return iter([_Pin()])


class _Instance:
    def __init__(self, name, parent, cell, net):
        self._name = name
        self._parent = _Named(parent)
        self._cell = _Named(cell)
        self._net = net

    def expanded_name(self):
        return self._name

    def circuit(self):
        return self._parent

    def circuit_ref(self):
        return self._cell

    def net_for_pin(self, _pin_id):
        return self._net


class _Logger:
    def __init__(self):
        self.net_mismatches = []
        self.device_mismatches = []
        self.param_mismatches = []
        self.class_mismatches = []
        self.pin_mismatches = []
        self.circuit_mismatches = []
        self.subcircuit_mismatches = []
        self.device_class_mismatches = []
        self.ambiguous_net_matches = []


def _dangling_logger(layout_cells=("BUF", "BUF"), reference_cells=("BUF", "BUF")):
    logger = _Logger()
    for i, cell in enumerate(layout_cells):
        net = _Net(f"$n{i}")
        logger.net_mismatches.append((net, None))
        logger.subcircuit_mismatches.append(
            (_Instance(f"X{i}", "TOP", cell, net), None)
        )
    for i, cell in enumerate(reference_cells):
        net = _Net(f"__NC_X{i}_Y__")
        logger.net_mismatches.append((None, net))
        logger.subcircuit_mismatches.append(
            (None, _Instance(f"X{i}", "TOP", cell, net))
        )
    return logger


def test_balanced_dangling_instances_become_one_warning():
    entries = lvs._build_mismatches(_dangling_logger())
    assert {e["severity"] for e in entries} == {"warning"}
    assert {e["category"] for e in entries} == {CATEGORY}
    (instances,) = [e for e in entries if e["subcircuit"]]
    assert instances["details"]["cell_type"] == "BUF"
    assert instances["details"]["instance_count"] == 2


def test_different_cell_types_stay_errors():
    entries = lvs._build_mismatches(_dangling_logger(reference_cells=("BUF", "INV")))
    assert any(e["severity"] == "error" for e in entries)


def test_one_sided_dangling_stays_error():
    logger = _dangling_logger()
    # The reference side's net is connected elsewhere too.
    logger.net_mismatches[-1] = (None, _Net("real", connections=3))
    entries = lvs._build_mismatches(logger)
    assert any(e["severity"] == "error" for e in entries)


def test_connected_net_mismatch_beside_dangling_stays_error():
    logger = _dangling_logger()
    logger.net_mismatches.append((_Net("a", connections=3), None))
    entries = lvs._build_mismatches(logger)
    assert any(e["severity"] == "error" for e in entries)


def test_unbalanced_counts_stay_errors():
    entries = lvs._build_mismatches(_dangling_logger(reference_cells=("BUF",)))
    assert any(e["severity"] == "error" for e in entries)


def test_escaped_module_name_gets_a_clear_error():
    with pytest.raises(VerilogNetlistError, match="escaped identifier"):
        _parse_header("module \\$paramod$abc\\top (a, b)")
