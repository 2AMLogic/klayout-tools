"""Literal path/name words at the remaining P&R Tcl sites (#6552)."""

import os
import shutil
import subprocess

import pytest

from klayout_tools import place_and_route_script as script
from klayout_tools import place_and_route_sta as sta


@pytest.fixture(
    params=[
        ("/design/top", "/design/top"),
        ("/tmp/a b", r"/tmp/a\ b"),
        (r"/design/$top[0];{x}\out", r"/design/\$top\[0\]\;\{x\}\\out"),
        ("/design/a\nb", r"/design/a\nb"),
        ("", "{}"),
    ],
    ids=["ordinary", "space", "metacharacters", "newline", "empty"],
)
def commands(request):
    value, quoted = request.param
    floorplan = script._floorplan_init_lines({"method": "def", "def_path": value})
    route = script._stage_script_lines(
        stage="route",
        checkpoint_in="/design/in.odb",
        checkpoint_out="/design/out.odb",
        tech_lef="/pdk/tech.lef",
        cell_lef="/pdk/cells.lef",
        liberty_path="/pdk/lib.lib",
        netlist_path="/design/top.v",
        hdl_toplevel="top",
        floorplan={"method": "def", "def_path": "/design/floorplan.def"},
        io_spec={"layer_h": "met3", "layer_v": "met2"},
        macros=[],
        power=None,
        clock_port="clk",
        clock_period_ns=10.0,
        max_transition_ns=None,
        max_capacitance_pf=None,
        max_fanout=None,
        input_delay_ns=None,
        output_delay_ns=None,
        cell_library="sky130_fd_sc_hd",
        seed=1,
        output_dir=value,
        route_critical_nets_percentage=0,
        max_antenna_repair_iterations=0,
        library={"filler": ["FILL1", "FILL2"]},
    )
    corners = sta._corner_sweep_script_lines(
        checkpoint_in=value,
        corners=[{"name": value, "path": value}, {"name": "tt", "path": "/pdk/tt.lib"}],
        io_spec={"layer_h": "met3", "layer_v": "met2"},
        clock_port="clk",
        clock_period_ns=10.0,
    )
    spef = sta._spef_sta_script_lines(
        checkpoint_in=value,
        liberty_path=value,
        clock_port="clk",
        clock_period_ns=10.0,
        spef_path=value,
        net_names=["net_a"],
    )
    # Output filenames are rooted at the caller's directory; an empty
    # directory produces a relative filename, not an empty output argument.
    prefix = quoted + "/" if value else ""
    return [
        (
            floorplan[0],
            ["-floorplan_initialize", value],
            f"read_def -floorplan_initialize {quoted}",
        ),
        (
            next(line for line in route if line.startswith("write_def ")),
            [os.path.join(value, "top.def")],
            f"write_def {prefix}top.def",
        ),
        (
            next(line for line in route if line.startswith("write_verilog ")),
            [os.path.join(value, "top.v"), "-remove_cells", "FILL1 FILL2"],
            f"write_verilog {prefix}top.v -remove_cells {{FILL1 FILL2}}",
        ),
        (corners[0], [value], f"read_db {quoted}"),
        (corners[1], [value, "tt"], f"define_corners {quoted} tt"),
        (
            corners[2],
            ["-corner", value, value],
            f"read_liberty -corner {quoted} {quoted}",
        ),
        (spef[0], [value], f"read_db {quoted}"),
        (spef[1], [value], f"read_liberty {quoted}"),
        (
            next(line for line in spef if line.startswith("read_spef ")),
            [value],
            f"read_spef {quoted}",
        ),
    ]


@pytest.fixture(
    params=range(9),
    ids=[
        "floorplan-def",
        "route-def",
        "route-verilog",
        "corner-db",
        "corner-names",
        "corner-liberty",
        "spef-db",
        "spef-liberty",
        "spef",
    ],
)
def command(commands, request):
    return commands[request.param]


def test_paths_are_literal_words(command):
    line, _, expected = command
    assert line == expected


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="requires a Tcl interpreter")
def test_paths_round_trip_through_tcl(command):
    line, args, _ = command
    name = line.split(" ", 1)[0]
    # Exercise Tcl's parser with argument-capture procs, without OpenROAD.
    capture = (
        f"proc {name} {{args}} {{\n"
        "puts [llength $args]\n"
        "foreach arg $args {puts [binary encode hex [encoding convertto utf-8 $arg]]}\n"
        "}\n"
    )
    result = subprocess.run(
        ["tclsh"],
        input=capture + line + "\n",
        text=True,
        capture_output=True,
        check=True,
        timeout=5,
    )
    assert result.stderr == ""
    assert result.stdout.splitlines() == [
        str(len(args)),
        *(arg.encode("utf-8").hex() for arg in args),
    ]
