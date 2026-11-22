"""Primary ideal-clock transition in the generated STA session and report."""

import json
from pathlib import Path

import pytest

from klayout_tools.cli import main
from klayout_tools.post_route_sta import run_sta
from test_post_route_sta import (
    _live_sky130_env,
    _setup_multi_corner_env,
    _setup_success_env,
    _stub_openroad_clock_capture,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


@pytest.mark.parametrize("multi_corner", [False, True])
@pytest.mark.parametrize("verilog", [False, True])
@pytest.mark.parametrize("transition", [0, 0.15, None])
def test_primary_clock_transition(
    tmp_path, monkeypatch, multi_corner, verilog, transition
):
    setup = _setup_multi_corner_env if multi_corner else _setup_success_env
    request = Path(setup(tmp_path, monkeypatch))
    data = json.loads(request.read_text())
    clock = "clk$x[0]"
    constraints = data["constraints"]
    del constraints["clock_port"]
    period = constraints.pop("clock_period_ns")
    constraints.update(
        clock_transition_ns=transition,
        clocks=[
            {"port": clock, "period_ns": period},
            {"port": "other", "period_ns": 3.0},
        ],
    )
    if verilog:
        del data["def"]
        (tmp_path / "top.v").write_text("module top(); endmodule\n")
        data.update(verilog="top.v", hdl_toplevel="top")
    request.write_text(json.dumps(data))
    scripts = _stub_openroad_clock_capture(monkeypatch, {None: "propagated_buffered"})
    result = run_sta(str(request))
    expected = (
        {"applied": False}
        if transition is None
        else {"applied": True, "ns": transition}
    )
    assert result["clock_transition"] == expected
    assert all("clock_transition" not in row for row in result.get("corners", []))
    assert len(scripts) == (2 if multi_corner else 1)
    for script in scripts:
        lines = script.splitlines()
        commands = [line for line in lines if line.startswith("set_clock_transition ")]
        if transition is None:
            assert commands == []
        else:
            assert commands == [
                f"set_clock_transition {float(transition)} "
                + r"[get_clocks clk\$x\[0\]]"
            ]
            primary = next(
                i for i, line in enumerate(lines) if line.startswith("create_clock ")
            )
            assert lines[primary + 1] == commands[0]


@pytest.mark.parametrize(
    "value", [False, True, -0.5, float("nan"), float("inf"), "0.15", 10**400]
)
def test_invalid_transition_is_a_request_error(tmp_path, monkeypatch, capsys, value):
    path = Path(_setup_success_env(tmp_path, monkeypatch))
    data = json.loads(path.read_text())
    data["constraints"]["clock_transition_ns"] = value
    path.write_text(json.dumps(data))
    scripts = _stub_openroad_clock_capture(monkeypatch, {None: "propagated_buffered"})
    assert main(["sta", str(path), "--format", "json"]) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert "constraints.clock_transition_ns" in error["message"]
    assert not scripts


def test_propagated_clock_rejects_transition(tmp_path, monkeypatch, capsys):
    path = Path(_setup_success_env(tmp_path, monkeypatch))
    data = json.loads(path.read_text())
    data["constraints"].update(propagated_clock=True, clock_transition_ns=0.15)
    path.write_text(json.dumps(data))
    scripts = _stub_openroad_clock_capture(monkeypatch, {None: "propagated_buffered"})
    assert main(["sta", str(path), "--format", "json"]) == 1
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert all(
        name in message
        for name in ["clock_transition_ns", "propagated_clock", "set_clock_transition"]
    )
    assert not scripts


@pytest.mark.skipif(_live_sky130_env() is None, reason="requires OpenROAD and sky130A")
def test_live_ideal_clock_transition_reduces_setup_slack(tmp_path):
    (tmp_path / "top.v").write_text(
        "module top(input clk, input d, output q); wire ck, fq;\n"
        "sky130_fd_sc_hd__clkbuf_4 b (.A(clk),.X(ck));\n"
        "sky130_fd_sc_hd__dfxtp_1 f (.CLK(ck),.D(d),.Q(fq));\n"
        "sky130_fd_sc_hd__buf_1 o (.A(fq),.X(q)); endmodule\n"
    )
    constraints = {
        "clock_port": "clk",
        "clock_period_ns": 5,
        "input_delay_ns": 0,
        "output_delay_ns": 0,
    }
    request = {
        "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
        "verilog": "top.v",
        "hdl_toplevel": "top",
        "constraints": constraints,
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    ideal = run_sta(str(path), pdk_variant="sky130A")
    constraints["clock_transition_ns"] = 0.5
    path.write_text(json.dumps(request))
    slow = run_sta(str(path), pdk_variant="sky130A")
    assert ideal["clock_transition"] == {"applied": False}
    assert slow["clock_transition"] == {"applied": True, "ns": 0.5}
    assert slow["worst_slack_ns"] < ideal["worst_slack_ns"]
