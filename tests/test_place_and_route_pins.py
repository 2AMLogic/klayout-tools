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
from test_place_and_route import (
    _GCD_RTL,
    _REAL_SKY130_PNR_VARIANT,
    HAVE_OPENROAD,
    _base_request,
    _synth_netlist_path,
    _write_request,
    run_place_and_route,
)

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


def test_netlist_ports_parse_comma_separated_name_lists(tmp_path):
    path = tmp_path / "n.v"
    path.write_text(
        "module top(clk, rst, a, y); input clk, rst; input [1:0] a; output y;\n"
        "endmodule\n"
        "module t2(input clk, rst, input [1:0] b, output y);\n"
        "endmodule\n"
        "module t3(input \\a.b , output reg [1:0] q, z);\n"
        "endmodule\n"
    )
    assert netlist_top_ports(str(path), "top") == ["clk", "rst", "a[1]", "a[0]", "y"]
    assert netlist_top_ports(str(path), "t2") == ["clk", "rst", "b[1]", "b[0]", "y"]
    assert netlist_top_ports(str(path), "t3") == [
        "a.b",
        "q[1]",
        "q[0]",
        "z[1]",
        "z[0]",
    ]


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


@pytest.mark.skipif(
    not HAVE_OPENROAD, reason="openroad is not installed on this machine"
)
@pytest.mark.skipif(
    _REAL_SKY130_PNR_VARIANT is None,
    reason="no real sky130_fd_sc_hd LEF/liberty/GDS set resolves via list_pdks()",
)
def test_integration_real_openroad_pin_constraints_land_on_requested_edges(
    tmp_path, monkeypatch
):
    """`io.pin_constraints` end to end against a real `openroad` (issue #3885
    AC3): ports constrained to distinct edges come back on those edges in the
    response's `pins[]`, read from the DEF OpenROAD wrote -- so the glob and
    bus-bit handling of `set_io_pin_constraint -pin_names` is exercised for
    real, not against a hand-written DEF."""
    root, variant = _REAL_SKY130_PNR_VARIANT
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)

    from klayout_tools.synthesize import run_synthesize

    (tmp_path / "gcd.v").write_text(_GCD_RTL, encoding="utf-8")
    synth_request = _write_request(
        tmp_path / "synth_request.json",
        {
            "engine": "yosys",
            "sources": ["gcd.v"],
            "hdl_toplevel": "gcd",
            "pdk": {"cell_library": "sky130_fd_sc_hd"},
        },
    )
    synth_report = run_synthesize(synth_request)

    request_path = _write_request(
        tmp_path / "pnr_request.json",
        _base_request(
            netlist=_synth_netlist_path(synth_request, "gcd", synth_report["run_id"]),
            pdk={"cell_library": "sky130_fd_sc_hd"},
            floorplan={
                "method": "utilization",
                "utilization_pct": 38,
                "aspect_ratio": 1.0,
                "core_margin_um": 2.0,
                "site": "unithd",
            },
            io={
                **IO,
                "pin_constraints": [
                    {"pins": ["a_in[*]"], "edge": "west"},
                    {"pins": ["b_in[*]"], "edge": "south"},
                    {"pins": ["result[*]"], "edge": "east"},
                    {"pins": ["clk", "rst_n", "start", "done"], "edge": "north"},
                ],
            },
            constraints={"clock_port": "clk", "clock_period_ns": 1.1},
        ),
    )
    report = run_place_and_route(request_path)

    assert report["status"] == "ok"
    edge_of = {pin["name"]: pin["edge"] for pin in report["pins"]}
    expected = {"clk": "north", "rst_n": "north", "start": "north", "done": "north"}
    for bit in range(16):
        expected[f"a_in[{bit}]"] = "west"
        expected[f"b_in[{bit}]"] = "south"
        expected[f"result[{bit}]"] = "east"
    assert edge_of == expected
