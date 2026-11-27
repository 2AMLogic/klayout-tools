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


_UNCHECKED_SDC = (
    "create_clock -name clk -period 20.0000 [get_ports {clk}]\n"
    "set_input_delay 4.0000 -clock [get_clocks {clk}] -add_delay [get_ports {ena}]\n"
    "set_output_delay 4.0000 -clock [get_clocks {clk}] "
    "-add_delay [get_ports {uo_out[0]}]\n"
)


@pytest.mark.parametrize("parsed_period", [None, 21.0])
def test_unchecked_forms_only_suppress_inferred_absence(tmp_path, parsed_period):
    sdc = _UNCHECKED_SDC
    if parsed_period is not None:
        sdc += f"create_clock -period {parsed_period} [get_ports clk]\n"
    result = _block(
        tmp_path,
        sdc,
        clocks=[{"port": "clk", "period_ns": 20.0}],
        input_delay_ns=4.0,
        output_delay_ns=4.0,
    )
    assert result["unchecked"] == [
        {"command": cmd, "line": line, "reason": "unsupported option or argument form"}
        for line, cmd in enumerate(
            ("create_clock", "set_input_delay", "set_output_delay"), 1
        )
    ]
    if parsed_period is None:
        assert result["mismatches"] == []
        assert result["status"] == "match"
    else:
        assert result["mismatches"] == [
            {
                "command": "create_clock",
                "subject": "clk",
                "sdc_value": 21.0,
                "request_value": 20.0,
                "line": 4,
            }
        ]
        assert result["status"] == "drift"


@pytest.mark.parametrize("unchecked_input", [False, True])
def test_missing_clock_and_delays_are_command_specific(tmp_path, unchecked_input):
    sdc = "set_input_delay $value -clock clk [all_inputs]\n" if unchecked_input else ""
    result = _block(tmp_path, sdc, input_delay_ns=4.0, output_delay_ns=4.0)
    missing = [("create_clock", None)]
    if not unchecked_input:
        missing.extend([("set_input_delay", "min"), ("set_input_delay", "max")])
    missing.extend([("set_output_delay", "min"), ("set_output_delay", "max")])
    assert [(m["command"], m.get("bound")) for m in result["mismatches"]] == missing
    assert all(m["sdc_value"] is None for m in result["mismatches"])
    assert result["status"] == "drift"


def test_unchecked_uncertainty_keeps_parsed_sdc_only_value(tmp_path):
    sdc = (
        "create_clock -period 2 [get_ports clk]\n"
        "set_clock_uncertainty -setup $jitter [get_clocks clk]\n"
        "set_clock_uncertainty -hold 0.3 [get_clocks clk]\n"
    )
    result = _block(tmp_path, sdc, clock_uncertainty={"setup_ns": 0.1})
    assert [u["line"] for u in result["unchecked"]] == [2]
    assert result["mismatches"] == [
        {
            "command": "set_clock_uncertainty",
            "subject": "clk",
            "sdc_value": 0.3,
            "request_value": None,
            "line": 3,
            "bound": "hold",
        }
    ]
    assert result["status"] == "drift"


def test_unchecked_input_keeps_wildcard_comparison_with_port_override(tmp_path):
    sdc = (
        "create_clock -period 2 [get_ports clk]\n"
        "set_input_delay 0.5 -clock clk [all_inputs]\n"
        "set_input_delay $unresolved -clock clk [get_ports b]\n"
    )
    result = _block(
        tmp_path, sdc, input_delay_ns=0.5, input_delays={"a": {"min_ns": 0.4}}
    )
    assert [
        (m["subject"], m["bound"], m["sdc_value"], m["request_value"])
        for m in result["mismatches"]
    ] == [("a", "min", 0.5, 0.4)]


@pytest.mark.parametrize("direction", ["input", "output"])
@pytest.mark.parametrize(
    "entry_clock,explicit,expected",
    [
        ("dqs", None, [("min", None), ("max", None)]),
        ("dqs", "min_only", [("max", None)]),
        ("dqs", "correct", []),
        ("dqs", "wrong", [("min", -0.1), ("max", 0.1)]),
        (None, None, [("min", 0.2), ("max", 0.2)]),
    ],
)
def test_per_port_delay_coverage_is_specific_to_port_bound_and_clock(
    tmp_path, direction, entry_clock, explicit, expected
):
    command = f"set_{direction}_delay"
    sdc = (
        "create_clock -period 2 [get_ports clk]\n"
        "create_clock -period 2 [get_ports dqs]\n"
        f"{command} 0.2 -clock clk [all_{direction}s]\n"
    )
    entry = {"min_ns": -0.1, "max_ns": 0.1}
    if entry_clock:
        entry["clock"] = entry_clock
    if explicit:
        reference = "clk" if explicit == "wrong" else "dqs"
        sdc += f"{command} -min -0.1 -clock {reference} [get_ports d0]\n"
        if explicit != "min_only":
            sdc += f"{command} -max 0.1 -clock {reference} [get_ports d0]\n"
    out = _block(
        tmp_path,
        sdc,
        clocks=[*CLOCKS, {"port": "dqs", "period_ns": 2.0}],
        **{f"{direction}_delay_ns": 0.2, f"{direction}_delays": {"d0": entry}},
    )
    assert out["status"] == ("drift" if expected else "match")
    assert [(m["bound"], m["sdc_value"]) for m in out["mismatches"]] == expected
    for m in out["mismatches"]:
        assert (m["command"], m["subject"]) == (command, "d0")
        assert m["request_value"] == entry[f"{m['bound']}_ns"]
        if explicit == "wrong":
            assert (m["sdc_clock"], m["request_clock"]) == ("clk", "dqs")
