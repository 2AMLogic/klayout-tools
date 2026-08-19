"""End-to-end test for issue #437 (Epic #391 Phase 5): a full digital-flow
candidate evaluation -- ``klt synthesize`` -> ``klt functional-verification``
-> ``klt place-and-route`` -> ``klt drc``/``klt layout-metrics`` on the
resulting GDS -- wired into one ``klt eval`` scored-gate verdict (#387) and
one ``klt trajectory`` log entry (#388), for the epic's own gcd reference
fixture.

Every slow external tool invocation (Yosys, OpenROAD, cocotb/Icarus) is
stubbed, mirroring the exact monkeypatch conventions each verb's own test
file already establishes:

- ``subprocess.run`` (the single stdlib module both ``synthesize.py`` and
  ``place_and_route.py`` import -- one dispatcher, keyed on ``cmd[0]``,
  patched once; see ``_stub_engines``) is stubbed the same way
  ``tests/test_synthesize.py``'s ``_stub_yosys_success`` /
  ``tests/test_place_and_route.py``'s ``_stub_openroad_success`` each do: a
  fake ``yosys -s``/``-V`` invocation that writes the real, live-captured GCD
  ``stat -liberty ... -json`` numbers from
  ``docs/design/yosys-synthesis-spike.md`` section 4, and a fake
  ``openroad`` invocation that writes the real, live-captured GCD per-stage
  ``-metrics`` numbers from issue #425's own worked example.
  ``place_and_route._merge_def_to_gds`` is separately replaced by a fake
  that writes a real, small, DRC-checkable ``klayout.db`` layout (never a
  placeholder string) -- so the downstream ``drc``/``layout-metrics`` gates
  below run against genuine layout content, exactly as they would against a
  real OpenROAD-merged GDS.
- ``functional_verification._import_runner`` stubbed the same way
  ``tests/test_functional_verification.py``'s ``_stub_runner`` does (a fake
  cocotb ``Runner`` that writes a real ``results.xml`` fragment).

This test's own job is the *wiring* (issue #437's scope) -- each verb's own
engine-invocation correctness is already covered by its own test file; see
those files' docstrings for that tier.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import types
from pathlib import Path

import klayout.db as kdb

from helpers.cocotb_fakes import FakeCocotbRunner as _FakeRunner
from helpers.subprocess_fakes import fake_completed
from klayout_tools import functional_verification as fv
from klayout_tools import pdk as pdk_module
from klayout_tools import place_and_route, synthesize
from klayout_tools.eval import run_eval
from klayout_tools.trajectory import (
    append_record,
    build_trajectory,
    read_log,
    record_from_eval,
)

# --------------------------------------------------------------------------- #
# RTL / testbench fixtures (trimmed from tests/test_synthesize.py /
# tests/test_functional_verification.py)
# --------------------------------------------------------------------------- #

_GCD_RTL = """\
module gcd #(
    parameter WIDTH = 16
) (
    input  wire             clk,
    input  wire             rst_n,
    input  wire              start,
    input  wire [WIDTH-1:0] a_in,
    input  wire [WIDTH-1:0] b_in,
    output reg              done,
    output reg  [WIDTH-1:0] result
);
    reg [WIDTH-1:0] a, b;
    reg             busy;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            busy   <= 1'b0;
            done   <= 1'b0;
        end else if (start && !busy) begin
            a      <= a_in;
            b      <= b_in;
            busy   <= 1'b1;
            done   <= 1'b0;
        end else if (busy) begin
            if (b == {WIDTH{1'b0}}) begin
                busy   <= 1'b0;
                done   <= 1'b1;
                result <= a;
            end else if (a > b) begin
                a <= a - b;
            end else begin
                b <= b - a;
            end
        end
    end
endmodule
"""

_TESTBENCH = "# stub testbench module -- never imported by these tests\n"

_RESULTS_XML_ALL_PASS = """\
<testsuites name="results">
  <testsuite name="all" package="all">
    <property name="random_seed" value="1785780800" />
    <testcase name="test_gcd_known_pairs" classname="test_gcd" \
time="0.01" sim_time_ns="10.0" />
    <testcase name="test_gcd_random_pairs" classname="test_gcd" \
time="0.02" sim_time_ns="20.0" />
  </testsuite>
</testsuites>
"""

#: Trimmed, but numerically real, GCD worked-example numbers --
#: docs/design/yosys-synthesis-spike.md section 4.
_GCD_MODULE_STATS = {
    "num_cells": 335,
    "area": 2951.5808,
    "sequential_area": 1251.2,
    "num_cells_by_type": {"sky130_fd_sc_hd__dfrtp_1": 50},
}

#: Trimmed, but numerically real, per-stage GCD ``-metrics`` numbers --
#: issue #425's own worked example (tests/test_place_and_route.py's own
#: ``_STAGE_METRICS`` fixture).
_STAGE_METRICS = {
    "floorplan": {
        "timing__setup__ws": -3.71641,
        "timing__setup__tns": -143.072,
        "design__die__area": 8487.94,
        "design__core__area": 7607.3,
        "design__instance__utilization": 0.387993,
    },
    "place": {
        "route__wirelength__estimated": 7852.26,
        "timing__setup__ws": -1.9939,
        "timing__setup__tns": -75.8268,
        "timing__fmax": 3.23217e08,
        "design__die__area": 8487.94,
        "design__core__area": 7607.3,
        "design__instance__utilization": 0.439638,
        "power__total": 0.0115449,
    },
    "cts": {
        "route__wirelength__estimated": 8095.6,
        "timing__setup__ws": -2.05,
        "timing__setup__tns": -78.1,
        "timing__fmax": 3.1e08,
        "design__die__area": 8487.94,
        "design__core__area": 7607.3,
        "design__instance__utilization": 0.446546,
        "power__total": 0.0116,
    },
    "route": {
        "route__wirelength": 9616,
        "timing__setup__ws": -2.18828,
        "timing__setup__tns": -82.8171,
        "timing__fmax": 3.0411e08,
        "design__die__area": 8487.94,
        "design__core__area": 7607.3,
        "design__instance__utilization": 0.446546,
        "power__total": 0.0116,
    },
}


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def _write_json(path: Path, data: dict) -> str:
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def _write_descriptor(tmp_path: Path, descriptor: dict) -> str:
    return _write_json(tmp_path / "descriptor.json", descriptor)


# --------------------------------------------------------------------------- #
# PDK fixture (liberty for synthesize + place-and-route; tech/cell LEF for
# place-and-route -- mirrors tests/test_synthesize.py's/
# tests/test_place_and_route.py's own `_isolate_pdk`/`_make_pdk_install`)
# --------------------------------------------------------------------------- #


def _isolate_pdk(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(pdk_module, "STORE_DIRS", [])
    monkeypatch.setattr(pdk_module, "CONVENTIONAL_PREFIXES", [])


def _make_pdk_install(
    root: Path,
    variant: str,
    *,
    cell_library: str = "sky130_fd_sc_hd",
    corner: str = "tt_025C_1v80",
) -> Path:
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True, exist_ok=True)
    lib_dir = variant_dir / "libs.ref" / cell_library

    lib_views_dir = lib_dir / "lib"
    lib_views_dir.mkdir(parents=True, exist_ok=True)
    content = (
        f'    default_operating_conditions : "{corner}";\n'
        "    nom_process : 1.0;\n"
        "    nom_temperature : 25.0;\n"
        "    nom_voltage : 1.8;\n"
    )
    (lib_views_dir / f"{cell_library}__{corner}.lib").write_text(
        content, encoding="utf-8"
    )

    techlef_dir = lib_dir / "techlef"
    techlef_dir.mkdir(parents=True, exist_ok=True)
    (techlef_dir / f"{cell_library}__nom.tlef").write_text("# tech lef\n")
    lef_dir = lib_dir / "lef"
    lef_dir.mkdir(parents=True, exist_ok=True)
    (lef_dir / f"{cell_library}.lef").write_text("# merged cell lef\n")

    return variant_dir


# --------------------------------------------------------------------------- #
# Stubbed Yosys (tests/test_synthesize.py's own pattern)
# --------------------------------------------------------------------------- #

_TEE_RE = re.compile(r"^tee -q -o (\S+) stat ")
_ABC_TEE_RE = re.compile(r"^tee -q -o (\S+) abc ")
_WRITE_VERILOG_RE = re.compile(r"^write_verilog -noattr (\S+)$")

#: A real `abc -constr` run's own `stime -p` summary line (issue #807) --
#: the same fixture `tests/test_synthesize.py` uses, kept here so this
#: file's stub produces the ABC log `run_synthesize` now parses.
_ABC_STIME_LINE = (
    'ABC: WireLoad = "none"  Gates =    297 (  5.7 %)   Cap =  6.2 ff ( 12.4 %)'
    "   Area =     1986.91 ( 66.3 %)   Delay =  2485.93 ps  ( 32.3 %)"
)

#: Yosys 0.68's own `help abc` wording for the option the capability probe
#: (issue #807) looks for.
_ABC_HELP_WITH_DONT_USE = """
    -dont_use <cell_name>
        avoid usage of the technology cell <cell_name> when mapping the design.
"""


def _script_output_paths(script_path: str) -> tuple[str, str]:
    stats_path = None
    netlist_path = None
    with open(script_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            match = _TEE_RE.match(line)
            if match:
                stats_path = match.group(1)
            match = _WRITE_VERILOG_RE.match(line)
            if match:
                netlist_path = match.group(1)
    assert stats_path is not None and netlist_path is not None
    return stats_path, netlist_path


def _fake_yosys_run(cmd, **kwargs) -> subprocess.CompletedProcess:
    if cmd[:2] == ["yosys", "-V"]:
        return fake_completed(stdout="Yosys 0.67+post (git sha1 deadbeef)\n")
    if cmd[:3] == ["yosys", "-p", "help abc"]:
        return fake_completed(stdout=_ABC_HELP_WITH_DONT_USE)
    assert cmd[:2] == ["yosys", "-s"]
    stats_path, netlist_path = _script_output_paths(cmd[2])
    with open(stats_path, "w", encoding="utf-8") as handle:
        json.dump({"modules": {"\\gcd": _GCD_MODULE_STATS}}, handle)
    with open(netlist_path, "w", encoding="utf-8") as handle:
        handle.write("// fake mapped netlist\n")
    for line in open(cmd[2], encoding="utf-8"):
        match = _ABC_TEE_RE.match(line.rstrip("\n"))
        if match:
            with open(match.group(1), "w", encoding="utf-8") as handle:
                handle.write(_ABC_STIME_LINE + "\n")
    return fake_completed(returncode=0)


# --------------------------------------------------------------------------- #
# Stubbed OpenROAD + DEF->GDS merge (tests/test_place_and_route.py's own
# pattern) -- the merge writes a REAL, small, DRC-checkable klayout.db
# layout (poly.width.1-style geometry, matching tests/test_eval.py's own
# `_make_layout`), never a placeholder string, so the downstream drc/
# layout-metrics gates below run against genuine layout content.
# --------------------------------------------------------------------------- #

_WRITE_DB_RE = re.compile(r"^write_db (\S+)$")


def _script_write_db_path(script_path: str) -> str:
    with open(script_path, encoding="utf-8") as handle:
        for line in handle:
            match = _WRITE_DB_RE.match(line.rstrip("\n"))
            if match:
                return match.group(1)
    raise AssertionError(f"no write_db line in {script_path}")


def _stage_from_script_path(script_path: str) -> str:
    stem = os.path.basename(script_path)[: -len(".tcl")]
    for stage in place_and_route.STAGE_ORDER:
        if stem.endswith(f"_{stage}"):
            return stage
    raise AssertionError(f"could not determine stage from {script_path}")


def _fake_openroad_run(cmd, **kwargs) -> subprocess.CompletedProcess:
    if cmd[:2] == ["openroad", "-version"]:
        return fake_completed(stdout="26Q3-771-gdeadbeef \n")
    assert cmd[0] == "openroad"
    metrics_path = cmd[4]
    script_path = cmd[5]
    if script_path.endswith("_route_corners.tcl"):
        # The post-route corner-sweep invocation (issue #949,
        # `place_and_route._corner_sweep_script_lines`) -- a second,
        # standalone OpenROAD call that never reaches `STAGE_ORDER`'s own
        # `_stage_from_script_path` lookup below; see
        # `tests/test_place_and_route.py`'s own identically-shaped stub
        # branch for the canonical version of this fixture.
        with open(metrics_path, "w", encoding="utf-8") as handle:
            json.dump(
                {"timing__setup__ws": -2.18828, "timing__hold__ws": 0.05},
                handle,
            )
        return fake_completed(returncode=0, stdout="")
    stage = _stage_from_script_path(script_path)

    Path(_script_write_db_path(script_path)).write_text("fake odb checkpoint\n")
    with open(metrics_path, "w", encoding="utf-8") as handle:
        json.dump(_STAGE_METRICS[stage], handle)

    if stage == "route":
        def_path = os.path.join(os.path.dirname(script_path), "gcd.def")
        Path(def_path).write_text("fake def\n")

    return fake_completed(returncode=0, stdout="")


def _stub_engines(monkeypatch, *, engines: tuple[str, ...]) -> None:
    """Stub the shared stdlib ``subprocess`` module's ``run`` -- both
    ``synthesize.subprocess`` and ``place_and_route.subprocess`` are the
    *same* module object (each just does ``import subprocess``), so
    monkeypatching one in isolation would silently clobber the other's
    stub. One dispatcher, keyed on ``cmd[0]``, patched once."""

    def fake_run(cmd, **kwargs):
        if "yosys" in engines and cmd[0] == "yosys":
            return _fake_yosys_run(cmd, **kwargs)
        if "openroad" in engines and cmd[0] == "openroad":
            return _fake_openroad_run(cmd, **kwargs)
        raise AssertionError(f"unexpected subprocess invocation: {cmd!r}")

    assert synthesize.subprocess is place_and_route.subprocess
    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)


def _make_layout(clean: bool) -> kdb.Layout:
    """A minimal layout with one poly shape -- clean or violating the sky130
    deck's `poly.width.1` rule (minimum poly width 150 dbu), mirroring
    `tests/test_eval.py`'s own `_make_layout` helper."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    poly = layout.layer(66, 20)
    layout.set_info(poly, kdb.LayerInfo(66, 20, "poly.drawing"))
    width = 200 if clean else 50
    top.shapes(poly).insert(kdb.Box(0, 0, width, 2000))
    return layout


def _stub_merge_def_to_gds(
    monkeypatch, block_layout_path: Path, *, clean: bool
) -> None:
    def fake_merge(**kwargs):
        layout = _make_layout(clean)
        layout.write(kwargs["out_path"])
        layout.write(str(block_layout_path))
        return {"path": None, "resolution": "none"}

    monkeypatch.setattr(place_and_route, "_merge_def_to_gds", fake_merge)


# --------------------------------------------------------------------------- #
# Stubbed cocotb runner (tests/helpers/cocotb_fakes.py's shared
# FakeCocotbRunner -- see tests/test_functional_verification.py for the
# canonical usage pattern)
# --------------------------------------------------------------------------- #


def _stub_functional_verification(monkeypatch) -> None:
    runner = _FakeRunner(_RESULTS_XML_ALL_PASS)

    def get_runner(name):
        assert name == "icarus"
        return runner

    monkeypatch.setattr(
        fv, "_import_runner", lambda: types.SimpleNamespace(get_runner=get_runner)
    )
    monkeypatch.setattr(fv, "_engine_version", lambda engine: "13.0")
    monkeypatch.setattr(fv, "_cocotb_version", lambda: "2.0.1")


# --------------------------------------------------------------------------- #
# The end-to-end test
# --------------------------------------------------------------------------- #


def test_full_digital_flow_produces_one_verdict_and_one_trajectory_entry(
    tmp_path, monkeypatch
):
    """synthesize -> functional-verification -> place-and-route ->
    drc/layout-metrics on the resulting GDS, all through one `klt eval`
    descriptor, produces one scored-gate verdict; that verdict is then
    recorded as one `klt trajectory` log entry -- the acceptance criterion
    issue #437 exists for."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))

    _write(tmp_path / "gcd.v", _GCD_RTL)
    _write(tmp_path / "test_gcd.py", _TESTBENCH)

    block_dir = tmp_path / "block"
    block_dir.mkdir()
    block_layout_path = block_dir / "layout.gds"

    _stub_engines(monkeypatch, engines=("yosys", "openroad"))
    _stub_merge_def_to_gds(monkeypatch, block_layout_path, clean=True)
    _stub_functional_verification(monkeypatch)

    _write_json(
        tmp_path / "synth_request.json",
        {
            "engine": "yosys",
            "sources": ["gcd.v"],
            "hdl_toplevel": "gcd",
            "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
        },
    )

    # `run_place_and_route`'s own `netlist_path` output convention (verified
    # against synthesize.py's `run_synthesize`) -- the synthesize *gate*
    # below runs first (declared first in the descriptor's `gates` list),
    # so this file exists by the time the place-and-route gate resolves it.
    synth_netlist_path = tmp_path / ".klt" / "synthesize" / "gcd_synth.v"

    _write_json(
        tmp_path / "fv_request.json",
        {
            "engine": "icarus",
            "sources": ["gcd.v"],
            "hdl_toplevel": "gcd",
            "testbench": {"module": "test_gcd", "testcase": None},
        },
    )

    _write_json(
        tmp_path / "pnr_request.json",
        {
            "engine": "openroad",
            "netlist": str(synth_netlist_path),
            "hdl_toplevel": "gcd",
            "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
            "floorplan": {
                "method": "utilization",
                "utilization_pct": 38,
                "aspect_ratio": 1.0,
                "core_margin_um": 2.0,
                "site": "unithd",
            },
            "io": {"layer_h": "met3", "layer_v": "met2"},
            "constraints": {"clock_port": "clk", "clock_period_ns": 1.1},
            "seed": 1,
            "target_stage": "route",
        },
    )

    descriptor = {
        "gates": [
            {
                "check": "synthesize",
                "name": "synthesize_area",
                "args": {"request": "synth_request.json"},
                "threshold": {"metric": "area_um2", "max": 5000},
            },
            {
                "check": "place-and-route",
                "name": "timing_closure",
                "args": {"request": "pnr_request.json"},
                # Matches the stock gcd example's own persistently negative
                # slack (docs/design/digital-flow-contracts-spike.md
                # section 5) -- a generous threshold that still passes.
                "threshold": {"metric": "worst_slack_ns", "min": -10},
            },
            {
                "check": "functional-verification",
                "args": {"request": "fv_request.json"},
            },
            {
                "check": "drc",
                "args": {"file": str(block_layout_path), "deck": "sky130"},
            },
            {
                "check": "layout-metrics",
                "args": {"block": str(block_dir)},
                "threshold": {"metric": "cell_count", "max": 10},
            },
        ],
        "objective": {
            "check": "place-and-route",
            "metric": "wirelength_um",
            "polarity": "minimize",
            "args": {"request": "pnr_request.json"},
        },
        "metrics": [
            {
                "name": "synthesize",
                "check": "synthesize",
                "args": {"request": "synth_request.json"},
            }
        ],
    }
    descriptor_path = _write_descriptor(tmp_path, descriptor)

    # --- one scored-gate verdict (#387) -------------------------------- #
    report = run_eval(descriptor_path)

    assert report["valid"] is True
    assert [gate["check"] for gate in report["gates"]] == [
        "synthesize",
        "place-and-route",
        "functional-verification",
        "drc",
        "layout-metrics",
    ]
    assert all(gate["status"] == "pass" for gate in report["gates"])
    synthesize_gate = report["gates"][0]
    assert synthesize_gate["count"] == 2951.5808
    place_and_route_gate = report["gates"][1]
    assert place_and_route_gate["count"] == -2.18828
    assert report["objective"] == {
        "name": "wirelength_um",
        "value": 9616,
        "polarity": "minimize",
    }
    assert report["metrics"]["synthesize"]["instance_count"] == 335
    assert report["metrics"]["synthesize"]["area_um2"] == 2951.5808

    # --- one trajectory-log entry (#388) -------------------------------- #
    log_path = tmp_path / "run.jsonl"
    record = record_from_eval(
        report,
        turn=0,
        candidate_ref=str(block_layout_path),
        description="gcd digital flow: synthesize -> functional-verification "
        "-> place-and-route -> drc/layout-metrics",
    )
    append_record(str(log_path), record)

    records = read_log(str(log_path))
    assert len(records) == 1
    assert records[0]["turn"] == 0
    assert records[0]["objective"] == report["objective"]
    assert records[0]["gate_results"] == [
        {"check": "synthesize", "status": "pass"},
        {"check": "place-and-route", "status": "pass"},
        {"check": "functional-verification", "status": "pass"},
        {"check": "drc", "status": "pass"},
        {"check": "layout-metrics", "status": "pass"},
    ]

    trajectory = build_trajectory(str(log_path))
    assert trajectory["record_count"] == 1
    assert trajectory["milestone_count"] == 0
    assert trajectory["baseline"]["objective"] == report["objective"]["value"]


def test_full_digital_flow_invalid_candidate_still_produces_one_record(
    tmp_path, monkeypatch
):
    """A candidate with a real DRC violation on the OpenROAD-merged GDS:
    `valid: false`, and the trajectory record still carries the failing
    gate -- an optimizer must be able to log a bad turn, not just a good
    one."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd.v", _GCD_RTL)

    block_dir = tmp_path / "block"
    block_dir.mkdir()
    block_layout_path = block_dir / "layout.gds"

    _stub_engines(monkeypatch, engines=("openroad",))
    _stub_merge_def_to_gds(monkeypatch, block_layout_path, clean=False)

    synth_netlist_path = tmp_path / "gcd_synth.v"
    _write(synth_netlist_path, "// pre-synthesized netlist\n")

    _write_json(
        tmp_path / "pnr_request.json",
        {
            "engine": "openroad",
            "netlist": str(synth_netlist_path),
            "hdl_toplevel": "gcd",
            "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
            "floorplan": {
                "method": "utilization",
                "utilization_pct": 38,
                "aspect_ratio": 1.0,
                "core_margin_um": 2.0,
                "site": "unithd",
            },
            "io": {"layer_h": "met3", "layer_v": "met2"},
            "constraints": {"clock_port": "clk", "clock_period_ns": 1.1},
            "seed": 1,
            "target_stage": "route",
        },
    )

    descriptor = {
        "gates": [
            {
                "check": "place-and-route",
                "args": {"request": "pnr_request.json"},
                "threshold": {"metric": "worst_slack_ns", "min": -10},
            },
            {
                "check": "drc",
                "args": {"file": str(block_layout_path), "deck": "sky130"},
            },
        ],
        "objective": {
            "check": "place-and-route",
            "metric": "wirelength_um",
            "polarity": "minimize",
            "args": {"request": "pnr_request.json"},
        },
    }
    descriptor_path = _write_descriptor(tmp_path, descriptor)

    report = run_eval(descriptor_path)

    assert report["valid"] is False
    drc_gate = report["gates"][1]
    assert drc_gate["check"] == "drc"
    assert drc_gate["status"] == "fail"
    assert drc_gate["count"] == 1

    log_path = tmp_path / "run.jsonl"
    record = record_from_eval(report, turn=0, candidate_ref=str(block_layout_path))
    append_record(str(log_path), record)

    records = read_log(str(log_path))
    assert len(records) == 1
    assert records[0]["gate_results"][1] == {"check": "drc", "status": "fail"}
