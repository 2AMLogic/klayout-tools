"""Tests for `io.pin_constraints` and the response's `pins[]` (issue #3885)."""

from __future__ import annotations

import pytest

from klayout_tools.place_and_route import _stage_script_lines
from klayout_tools.place_and_route_pins import (
    check_pin_constraints,
    netlist_top_ports,
    pin_constraint_lines,
    read_def_pins,
)
from klayout_tools.place_and_route_request import PlaceAndRouteError, _validate_io

IO = {"layer_h": "met3", "layer_v": "met2"}
NETLIST = """\
module top(clk, a, y);
  input clk;
  input [2:0] a;
  output \\y ;
  wire n1;
endmodule
module other(z);
  input z;
endmodule
"""


def _io(*constraints):
    return {**IO, "pin_constraints": list(constraints)}


def test_validate_io_unchanged_without_constraints():
    assert _validate_io(IO, "sky130_fd_sc_hd") == IO
    assert _validate_io({**IO, "pin_constraints": []}, "sky130_fd_sc_hd") == IO


@pytest.mark.parametrize(
    "bad",
    [
        {"pins": ["a[0]"], "edge": "up"},
        {"pins": [], "edge": "north"},
        {"pins": ["a[0]"], "edge": "north", "range_um": [5, 1]},
        {"pins": ["a[0]"], "edge": "north", "range_um": [1]},
        {"pins": ["a[0]"], "edge": "north", "ordered": "yes"},
        {"pins": ["a[0]"], "edge": "north", "bogus": 1},
    ],
)
def test_validate_io_rejects_bad_constraint(bad):
    with pytest.raises(PlaceAndRouteError, match="pin_constraints"):
        _validate_io(_io(bad), "sky130_fd_sc_hd")


def test_netlist_ports_expand_buses_and_scope_to_module(tmp_path):
    path = tmp_path / "n.v"
    path.write_text(NETLIST)
    assert netlist_top_ports(str(path), "top") == ["clk", "a[2]", "a[1]", "a[0]", "y"]
    assert netlist_top_ports(str(path), "missing") is None


def test_unmatched_and_double_matched_patterns_rejected():
    ports = ["clk", "a[1]", "a[0]"]
    check_pin_constraints([{"pins": ["a[*]"], "edge": "west"}], ports)
    with pytest.raises(PlaceAndRouteError, match="matches no top-level port"):
        check_pin_constraints([{"pins": ["nope*"], "edge": "west"}], ports)
    with pytest.raises(PlaceAndRouteError, match="matched by entries 0 and 1"):
        check_pin_constraints(
            [
                {"pins": ["a[0]"], "edge": "west"},
                {"pins": ["a*"], "edge": "east"},
            ],
            ports,
        )
    check_pin_constraints([{"pins": ["whatever"], "edge": "west"}], None)


def test_constraint_lines():
    lines = pin_constraint_lines(
        [
            {"pins": ["a[*]"], "edge": "west"},
            {"pins": ["y"], "edge": "north", "range_um": [1, 20.5], "ordered": True},
        ]
    )
    assert lines == [
        "set_io_pin_constraint -pin_names {a[*]} -region left:*",
        "set_io_pin_constraint -pin_names {y} -region top:1-20.5 -group -order",
    ]
    assert pin_constraint_lines([]) == []


def _place_script(io_spec):
    return _stage_script_lines(
        stage="place",
        checkpoint_in="in.odb",
        checkpoint_out="out.odb",
        tech_lef="t.lef",
        cell_lef="c.lef",
        liberty_path="l.lib",
        netlist_path="n.v",
        hdl_toplevel="top",
        floorplan={},
        io_spec=io_spec,
        macros=[],
        power=None,
        clock_port="clk",
        clock_period_ns=1.0,
        max_transition_ns=None,
        max_capacitance_pf=None,
        max_fanout=None,
        input_delay_ns=None,
        output_delay_ns=None,
        cell_library="sky130_fd_sc_hd",
        seed=1,
        output_dir="/tmp/x",
        route_critical_nets_percentage=0,
        max_antenna_repair_iterations=0,
    )


def test_constraints_emitted_before_place_pins_and_default_unchanged():
    plain = _place_script(dict(IO))
    constrained = _place_script(_io({"pins": ["a[*]"], "edge": "south"}))
    assert not any("set_io_pin_constraint" in line for line in plain)
    extra = [line for line in constrained if line not in plain]
    assert extra == ["set_io_pin_constraint -pin_names {a[*]} -region bottom:*"]
    assert [line for line in constrained if line in plain] == plain
    assert constrained.index(extra[0]) + 1 == next(
        i for i, line in enumerate(constrained) if line.startswith("place_pins")
    )


def test_read_def_pins(tmp_path):
    path = tmp_path / "d.def"
    path.write_text(
        "UNITS DISTANCE MICRONS 1000 ;\n"
        "DIEAREA ( 0 0 ) ( 100000 50000 ) ;\n"
        "PINS 2 ;\n"
        "  - a[0] + NET a[0] + DIRECTION INPUT + USE SIGNAL\n"
        "    + LAYER met3 ( -150 -150 ) ( 150 150 )\n"
        "    + PLACED ( 0 20000 ) N ;\n"
        "  - y + NET y + DIRECTION OUTPUT + USE SIGNAL\n"
        "    + LAYER met2 ( -70 -70 ) ( 70 70 )\n"
        "    + PLACED ( 40000 50000 ) N ;\n"
        "END PINS\n"
    )
    pins = read_def_pins(str(path))
    assert pins == [
        {"name": "a[0]", "edge": "west", "x_um": 0.0, "y_um": 20.0, "layer": "met3"},
        {"name": "y", "edge": "north", "x_um": 40.0, "y_um": 50.0, "layer": "met2"},
    ]
    assert read_def_pins(str(tmp_path / "missing.def")) is None
