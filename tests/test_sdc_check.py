"""``request.sdc_check`` drift reader for ``klt sta`` (issue #5236)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import post_route_sta
from klayout_tools._sta_sdc_check import parse_sdc, sdc_drift_block
from klayout_tools.post_route_sta import PostRouteStaError, run_sta

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

CLOCKS = [{"port": "clk", "period_ns": 2.0}]


def _block(tmp_path: Path, sdc: str, **kw):
    path = tmp_path / "c.sdc"
    path.write_text(sdc, encoding="utf-8")
    args = dict(
        request_dir=str(tmp_path),
        clocks=CLOCKS,
        clock_port="clk",
        input_delay_ns=None,
        output_delay_ns=None,
        input_delays=None,
        output_delays=None,
        clock_uncertainty=None,
    )
    args.update(kw)
    return sdc_drift_block(str(path), **args)


def test_match_with_hash_and_comments(tmp_path):
    out = _block(
        tmp_path,
        "# header\n\ncreate_clock -name clk -period 2.0 \\\n  [get_ports clk]\n",
    )
    assert out["status"] == "match"
    assert out["mismatches"] == [] and out["unchecked"] == []
    assert out["path"]["content_hash"].startswith("sha256:")


def test_period_drift_and_missing_clock(tmp_path):
    out = _block(tmp_path, "create_clock -name clk -period 2.5 [get_ports clk]\n")
    assert out["status"] == "drift"
    (m,) = out["mismatches"]
    assert (m["command"], m["subject"], m["sdc_value"], m["request_value"]) == (
        "create_clock",
        "clk",
        2.5,
        2.0,
    )
    assert _block(tmp_path, "")["mismatches"][0]["sdc_value"] is None


def test_scalar_and_per_port_delays(tmp_path):
    sdc = (
        "set_input_delay 0.5 -clock clk [all_inputs]\n"
        "set_input_delay 0.9 -max -clock clk [get_ports a]\n"
        "set_output_delay 0.2 -clock clk [all_outputs]\n"
    )
    ok = _block(
        tmp_path,
        "create_clock -period 2.0 [get_ports clk]\n" + sdc,
        input_delay_ns=0.5,
        output_delay_ns=0.2,
        input_delays={"a": {"max_ns": 0.9, "min_ns": 0.5}},
    )
    assert ok["status"] == "match"  # a.min is covered by the all_inputs line
    drift = _block(
        tmp_path,
        "create_clock -period 2.0 [get_ports clk]\n" + sdc,
        input_delay_ns=0.5,
        output_delay_ns=0.2,
        input_delays={"a": {"max_ns": 0.9, "min_ns": 0.4}},
    )
    assert [(m["subject"], m["bound"]) for m in drift["mismatches"]] == [("a", "min")]
    bad = _block(
        tmp_path,
        "create_clock -period 2.0 [get_ports clk]\n" + sdc,
        input_delay_ns=0.5,
        output_delay_ns=0.3,
        input_delays={"a": {"max_ns": 0.9}},
    )
    assert [(m["command"], m["request_value"]) for m in bad["mismatches"]] == [
        ("set_output_delay", 0.3),
        ("set_output_delay", 0.3),
    ]


def test_uncertainty(tmp_path):
    base = "create_clock -period 2.0 [get_ports clk]\n"
    ok = _block(
        tmp_path,
        base + "set_clock_uncertainty 0.1 [get_clocks clk]\n",
        clock_uncertainty={"setup_ns": 0.1, "hold_ns": 0.1},
    )
    assert ok["status"] == "match"
    bad = _block(
        tmp_path,
        base + "set_clock_uncertainty -setup 0.2 clk\n",
        clock_uncertainty={"setup_ns": 0.1, "hold_ns": 0.1},
    )
    assert {m["bound"] for m in bad["mismatches"]} == {"setup", "hold"}


def test_unchecked_commands_listed_by_name():
    cmds, unchecked = parse_sdc(
        "set_multicycle_path 2 -to x\n"
        "set_input_delay 1 -add_delay -clock clk [all_inputs]\n"
        "set_input_delay $v -clock clk [all_inputs]\n"
        "create_clock -period 1 -waveform {0 0.5} [get_ports c]\n"
    )
    assert cmds == []
    assert [u["command"] for u in unchecked] == [
        "set_multicycle_path",
        "set_input_delay",
        "set_input_delay",
        "create_clock",
    ]


def _request(tmp_path: Path, **extra) -> str:
    (tmp_path / "top.def").write_text("# def\n", encoding="utf-8")
    req = {
        "def": "top.def",
        "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
        "constraints": {"clock_port": "clk", "clock_period_ns": 2.0},
        **extra,
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(req), encoding="utf-8")
    return str(path)


def test_run_sta_missing_or_malformed_field_is_request_error(tmp_path):
    with pytest.raises(PostRouteStaError, match="sdc_check file not found"):
        run_sta(_request(tmp_path, sdc_check={"path": "nope.sdc"}))
    with pytest.raises(PostRouteStaError, match="sdc_check must be an object"):
        run_sta(_request(tmp_path, sdc_check="a.sdc"))


def test_run_sta_attaches_block_only_when_requested(tmp_path, monkeypatch):
    monkeypatch.setattr(post_route_sta, "_dispatch_sta", lambda **_: {"status": "ok"})
    assert "sdc_drift" not in run_sta(_request(tmp_path))
    (tmp_path / "c.sdc").write_text(
        "create_clock -name clk -period 2.0 [get_ports clk]\n", encoding="utf-8"
    )
    out = run_sta(_request(tmp_path, sdc_check={"path": "c.sdc"}))
    assert out["sdc_drift"]["status"] == "match"
