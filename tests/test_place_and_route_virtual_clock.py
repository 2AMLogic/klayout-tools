"""Virtual clock (issue #4913): ``constraints.clock_period_ns`` without
``clock_port`` lets a purely combinational block reach ``route`` with a real
timing number."""

from __future__ import annotations

import pytest

from klayout_tools.place_and_route import PlaceAndRouteError, run_place_and_route
from test_place_and_route import (
    _REAL_SKY130_PNR_VARIANT,
    HAVE_OPENROAD,
    _base_request,
    _setup_success_env,
    _stub_openroad_success,
    _synth_netlist_path,
    _write,
    _write_request,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


@pytest.mark.skipif(
    not HAVE_OPENROAD, reason="openroad is not installed on this machine"
)
@pytest.mark.skipif(
    _REAL_SKY130_PNR_VARIANT is None,
    reason="no real sky130_fd_sc_hd LEF/liberty/GDS set resolves via list_pdks()",
)
def test_integration_real_openroad_combinational_virtual_clock(tmp_path, monkeypatch):
    """Issue #4913: a purely combinational adder reaches `route` with only
    `clock_period_ns` (a virtual clock, no `clock_port`) and reports a real,
    finite worst slack instead of the unconstrained sentinel."""
    root, variant = _REAL_SKY130_PNR_VARIANT
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)

    from klayout_tools.synthesize import run_synthesize

    (tmp_path / "add4.v").write_text(
        "module add4(input [3:0] a, input [3:0] b, output [4:0] s);\n"
        "  assign s = a + b;\nendmodule\n",
        encoding="utf-8",
    )
    synth_request = _write_request(
        tmp_path / "synth_request.json",
        {
            "engine": "yosys",
            "sources": ["add4.v"],
            "hdl_toplevel": "add4",
            "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
        },
    )
    synth_report = run_synthesize(synth_request)
    request_path = _write_request(
        tmp_path / "pnr_request.json",
        _base_request(
            netlist=_synth_netlist_path(synth_request, "add4", synth_report["run_id"]),
            hdl_toplevel="add4",
            constraints={
                "clock_period_ns": 10.0,
                "input_delay_ns": 1.0,
                "output_delay_ns": 1.0,
            },
        ),
    )

    report = run_place_and_route(request_path)

    assert report["status"] == "ok"
    assert report["stage_reached"] == "route"
    route = next(s for s in report["stages"] if s["name"] == "route")
    assert abs(route["worst_slack_ns"]) < 1e30, route
    assert route["timing_status"] == "constrained", route
    # Every swept corner sees the virtual clock too (fresh OpenSTA sessions).
    assert all(c["timing_status"] == "constrained" for c in route["corners"]), route


# Issue #4913: `constraints.clock_period_ns` without `clock_port` is a
# *virtual clock* so a purely combinational block can reach `route` with a
# real timing number.
def test_virtual_clock_lines_and_io_delays():
    from klayout_tools.place_and_route_script import (
        _clock_lines,
        _io_delay_lines,
    )

    assert (
        _clock_lines(None, 5.0)[-1] == "create_clock -name klt_virtual_clk -period 5.0"
    )
    assert _clock_lines(None, None) == []
    assert _io_delay_lines(None, 0.5, 0.5) == [
        "set_input_delay 0.5 -clock klt_virtual_clk [all_inputs]",
        "set_output_delay 0.5 -clock klt_virtual_clk [all_outputs]",
    ]
    # No period, no port: nothing (unchanged behavior).
    assert _io_delay_lines(None, None, None) == []
    # Clocked flows keep the pre-existing Tcl.
    assert _clock_lines("clk", 5.0)[-1] == (
        "create_clock -name clk -period 5.0 [get_ports clk]"
    )


def test_run_constraints_period_alone_is_accepted(tmp_path, monkeypatch):
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        target_stage="floorplan",
        constraints={"clock_period_ns": 5.0, "input_delay_ns": 0.5},
    )
    _stub_openroad_success(monkeypatch, stages=("floorplan",))
    assert run_place_and_route(request_path)["status"] == "ok"


@pytest.mark.parametrize("period", [0, -1.0, "fast", True])
def test_run_constraints_virtual_period_must_be_positive(tmp_path, period):
    _write(tmp_path / "gcd_synth.v", "// netlist\n")
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(constraints={"clock_period_ns": period}),
    )
    with pytest.raises(PlaceAndRouteError, match="clock_period_ns must be"):
        run_place_and_route(request_path)


def test_run_target_stage_place_error_mentions_virtual_clock(tmp_path, monkeypatch):
    request_path = _setup_success_env(
        tmp_path, monkeypatch, target_stage="place", constraints=None
    )
    with pytest.raises(PlaceAndRouteError, match="accepted as a virtual clock"):
        run_place_and_route(request_path)
