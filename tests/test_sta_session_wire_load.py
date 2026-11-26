"""A netlist timing report records what its own OpenROAD session established."""

import json
import re
import subprocess
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed, patch_openroad_run
from klayout_tools import post_route_sta as sta
from test_post_route_sta import (
    _LIVE_ONLY,
    _STA_METRICS,
    _live_verilog_request,
    _make_pdk_install,
    _setup_success_env,
    _setup_verilog_success_env,
)

PINNED = "26Q3-1510-g6cb3f2b704"


def _engine(monkeypatch, versions):
    sessions = iter(versions)

    def run(argv, **kwargs):
        if argv[1] == "-version":
            return fake_completed(stdout=PINNED)
        script = Path(argv[-1]).read_text()
        Path(argv[4]).write_text(json.dumps(_STA_METRICS))
        version = next(sessions)
        marker = (
            f"KLT_WIRE_LOAD_SESSION {version}"
            if version and "KLT_WIRE_LOAD_SESSION" in script
            else ""
        )
        return fake_completed(stdout=marker)

    patch_openroad_run(monkeypatch, run)


@pytest.mark.parametrize(
    ("model", "mode", "version", "sources"),
    [
        (None, None, PINNED, ("none", "none")),
        ("Small", None, PINNED, ("request", "none")),
        ("Small", "top", PINNED, ("request", "request")),
        (None, None, "another_build", ("unknown", "unknown")),
        ("Small", "top", None, ("unknown", "unknown")),
    ],
)
def test_scalar_session_evidence(tmp_path, monkeypatch, model, mode, version, sources):
    path = _setup_verilog_success_env(tmp_path, monkeypatch)
    req = json.loads(Path(path).read_text())
    if model:
        req["constraints"]["wire_load_model"] = model
    if mode:
        req["constraints"]["wire_load_mode"] = mode
    Path(path).write_text(json.dumps(req))
    _engine(monkeypatch, [version])
    report = sta.run_sta(path)
    assert (report["wire_load_model"], report["wire_load_mode"]) == (model, mode)
    block = report["wire_load"]
    assert (block["model_source"], block["mode_source"]) == sources
    assert block["model"] == (model if version else None)
    assert block["mode"] == (mode if version else None)


def test_wire_load_evidence_belongs_to_each_corner(tmp_path, monkeypatch):
    path = Path(_setup_verilog_success_env(tmp_path, monkeypatch))
    req = json.loads(path.read_text())
    second = "ss_100C_1v60"
    _make_pdk_install(tmp_path / "install", "sky130A", corner=second)
    req["pdk"]["corners"] = [req["pdk"].pop("corner"), second]
    path.write_text(json.dumps(req))
    _engine(monkeypatch, [PINNED, None])
    report = sta.run_sta(str(path))
    assert report["wire_load_model"] is None and report["wire_load_mode"] is None
    assert "wire_load" not in report
    assert [entry["wire_load"]["model_source"] for entry in report["corners"]] == [
        "none",
        "unknown",
    ]


def test_def_has_no_netlist_wire_load_evidence(tmp_path, monkeypatch):
    path = _setup_success_env(tmp_path, monkeypatch)
    _engine(monkeypatch, [PINNED])
    report = sta.run_sta(path)
    assert report["wire_load_model"] is None and report["wire_load_mode"] is None
    assert "wire_load" not in report


@_LIVE_ONLY
def test_live_wire_load_selection(tmp_path):
    for constraints in (
        {},
        {"wire_load_model": "Small"},
        {"wire_load_model": "Small", "wire_load_mode": "top"},
    ):
        report = sta.run_sta(
            _live_verilog_request(tmp_path, **constraints), pdk_variant="sky130A"
        )
        block = report["wire_load"]
        for key in ("model", "mode"):
            expected = constraints.get(f"wire_load_{key}")
            assert block[key] == expected
            if expected:
                assert block[f"{key}_source"] == "request"
            elif report["engine_version"] == PINNED:
                assert block[f"{key}_source"] == "none"


@_LIVE_ONLY
def test_pinned_engine_wire_capacitance_with_and_without_liberty_default(
    tmp_path, monkeypatch
):
    library = "sky130_fd_sc_hd"
    liberty, corner, pdk = sta._resolve_liberty(
        library, "tt_025C_1v80", variant="sky130A"
    )
    tech_lef, cell_lef = sta._resolve_lef(library, pdk)
    contents = Path(liberty).read_text()
    without_defaults, count = re.subn(
        r"^\s*default_wire_load(?:_mode)?\s*:[^;]+;", "", contents, flags=re.MULTILINE
    )
    assert count == 2
    stripped = tmp_path / "no_defaults.lib"
    stripped.write_text(without_defaults)
    netlist = tmp_path / "fan.v"
    netlist.write_text(
        "module fan(input clk, input a, output y1, output y2); wire n;\n"
        "sky130_fd_sc_hd__buf_1 drive(.A(a), .X(n));\n"
        "sky130_fd_sc_hd__buf_1 sink1(.A(n), .X(y1));\n"
        "sky130_fd_sc_hd__buf_1 sink2(.A(n), .X(y2));\n"
        "endmodule\n"
    )
    caps = []
    for lib, setter in (
        (liberty, ""),
        (stripped, ""),
        (liberty, "set_wire_load_model -name Small"),
    ):
        tcl = tmp_path / "probe.tcl"
        tcl.write_text(
            f"read_liberty {{{lib}}}\n"
            f"read_lef {{{tech_lef}}}\nread_lef {{{cell_lef}}}\n"
            f"read_verilog {{{netlist}}}\nlink_design fan\n"
            "create_clock -name ck -period 10 [get_ports clk]\n"
            "set_input_delay 0 -clock ck [get_ports a]\n"
            "set_output_delay 0 -clock ck [get_ports y*]\n"
            f"{setter}\n"
            'puts "ENGINE [ord::openroad_version]"\n'
            "report_checks\nreport_net -digits 8 n\n"
        )
        proc = subprocess.run(
            ["openroad", "-no_init", "-exit", str(tcl)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        output = proc.stdout
        if f"ENGINE {PINNED}" not in output.splitlines():
            pytest.skip(
                "wire capacitance was characterized on the pinned OpenROAD build"
            )
        match = re.search(r"Wire capacitance:\s*([\d.]+)", output)
        assert match, output
        caps.append(float(match[1]))
    declared, removed, selected = caps
    assert declared == removed == 0.0
    assert selected > 0.0

    # Point the public timing session at the same Liberty copy as the raw probe.
    monkeypatch.setattr(
        sta, "_resolve_liberty", lambda *a, **kw: (str(stripped), corner, pdk)
    )
    report = sta.run_sta(_live_verilog_request(tmp_path), pdk_variant="sky130A")
    assert report["wire_load"] == {
        "model": None,
        "model_source": "none",
        "mode": None,
        "mode_source": "none",
    }
