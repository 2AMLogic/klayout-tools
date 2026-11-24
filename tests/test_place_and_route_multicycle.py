"""Multicycle exceptions in fresh, resumed and post-route OpenROAD sessions."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from klayout_tools.cli import main
from klayout_tools.place_and_route import PlaceAndRouteError, run_place_and_route
from klayout_tools.post_route_sta import run_sta
from test_place_and_route import (
    _base_request,
    _make_pdk_install,
    _setup_success_env,
    _stub_merge_def_to_gds,
    _stub_openroad_success_with_post_route_spef,
    _stub_run_extract_for_post_route_spef,
)
from test_place_and_route_resume import STAGES, _env, _record_stage_runs, _strip
from test_post_route_sta import _LIVE_ONLY
from test_sta_multicycle_run import _feedback_netlist

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

CLOCK = {"clock_port": "clk", "clock_period_ns": 2}
FEEDBACK = {"from": "ff_fb", "to": "ff_fb", "setup": 4, "hold": 3}
ADJACENT = {
    "from": "ff_fb",
    "through": "ff_adj/D",
    "to": "ff_adj",
    "setup": 1,
    "hold": 1,
}


def test_each_session_applies_paired_exceptions_after_clock_creation(
    tmp_path, monkeypatch
):
    entries = [FEEDBACK, ADJACENT]
    request = _setup_success_env(
        tmp_path,
        monkeypatch,
        constraints={**CLOCK, "multicycle_paths": entries},
        post_route_spef=True,
    )
    _make_pdk_install(tmp_path / "install", "sky130A", corner="ss_100C_1v60")
    _stub_openroad_success_with_post_route_spef(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)
    _stub_run_extract_for_post_route_spef(monkeypatch)
    report = run_place_and_route(request)
    assert report["multicycle_paths"] == [
        {**FEEDBACK, "index": 0, "through": None},
        {**ADJACENT, "index": 1},
    ]
    scripts = sorted((tmp_path / ".klt/place-and-route").glob("pnr_*.tcl"))
    assert len(report["engine_logs"]) == len(scripts) == 8
    for script in scripts:
        text = script.read_text()
        floorplan = script.stem.endswith("_floorplan")
        start = "link_design" if floorplan else "read_db"
        assert (
            text.index(start)
            < text.index("create_clock")
            < text.index("set_multicycle_path")
        )
        statements = [
            line
            for line in text.splitlines()
            if line.startswith("set_multicycle_path ")
        ]
        assert len(statements) == 4
        for i, entry in enumerate(entries):
            setup, hold = statements[i * 2 : i * 2 + 2]
            first = f"set_multicycle_path {entry['setup']} -setup "
            second = f"set_multicycle_path {entry['hold']} -hold "
            assert setup.startswith(first) and hold.startswith(second)
            assert setup[len(first) :] == hold[len(second) :]
        assert ("no timed path connects the selection" in text) == floorplan
        assert "===KLT_STA_MCP_BEGIN===" not in text
        for op in (
            "initialize_floorplan",
            "global_placement",
            "repair_timing",
            "clock_tree_synthesis",
            "global_route ",
            "read_spef",
            "report_worst_slack_metric",
        ):
            if op in text:
                assert text.index(statements[-1]) < text.index(op)


def test_partial_resume_reapplies_and_multiplier_change_invalidates(
    tmp_path, monkeypatch
):
    path = Path(
        _env(
            tmp_path,
            monkeypatch,
            target_stage="place",
            constraints={**CLOCK, "multicycle_paths": [FEEDBACK]},
        )
    )
    run_place_and_route(str(path))
    doc = json.loads(path.read_text())
    doc["target_stage"] = "route"
    path.write_text(json.dumps(doc))
    ran = _record_stage_runs(monkeypatch)
    extended = run_place_and_route(str(path), resume=True)
    assert extended["resume"]["resumed_stages"] == ["floorplan", "place"]
    assert ran == ["cts", "route"]
    assert (
        "set_multicycle_path 4 -setup"
        in (tmp_path / "artifacts/pnr_gcd_cts.tcl").read_text()
    )

    ran.clear()
    doc["constraints"]["multicycle_paths"][0]["setup"] = 3
    path.write_text(json.dumps(doc))
    changed = run_place_and_route(str(path), resume=True)
    assert changed["resume"]["resumed_stages"] == []
    assert ran == STAGES
    assert changed["multicycle_paths"][0]["setup"] == 3


def test_empty_entries_preserve_scripts_and_report(tmp_path, monkeypatch):
    path = Path(_env(tmp_path, monkeypatch, constraints=CLOCK))
    first = run_place_and_route(str(path))
    scripts = {p: p.read_bytes() for p in (tmp_path / "artifacts").glob("pnr_*.tcl")}
    doc = json.loads(path.read_text())
    doc["constraints"]["multicycle_paths"] = []
    path.write_text(json.dumps(doc))
    empty = run_place_and_route(str(path), resume=True)
    assert _strip(first) == _strip(empty)
    assert all(p.read_bytes() == value for p, value in scripts.items())


@pytest.mark.parametrize(
    ("constraints", "message"),
    [
        ({"multicycle_paths": [FEEDBACK]}, "requires constraints.clock_period_ns"),
        (
            {**CLOCK, "multicycle_paths": [FEEDBACK, {**FEEDBACK, "hold": False}]},
            r"multicycle_paths\[1\]\.hold",
        ),
    ],
)
def test_invalid_entries_fail_before_a_stage_runs(
    tmp_path, monkeypatch, constraints, message
):
    path = _env(tmp_path, monkeypatch, constraints=constraints)
    ran = _record_stage_runs(monkeypatch)
    with pytest.raises(PlaceAndRouteError, match=message):
        run_place_and_route(path)
    assert ran == []


@_LIVE_ONLY
@pytest.mark.parametrize(
    ("launch", "capture", "expected"),
    [
        ("no_such_*", "ff_fb", "matches no register or pin"),
        ("ff_u", "ff_fb", "unclocked register"),
        ("ff_adj", "ff_fb", "no timed path"),
    ],
)
def test_public_cli_rejects_unusable_paths_at_floorplan(
    tmp_path, monkeypatch, capsys, launch, capture, expected
):
    (tmp_path / "top.v").write_text(_feedback_netlist())
    monkeypatch.setenv("PDK", "sky130A")
    request = _base_request(
        netlist="top.v",
        hdl_toplevel="top",
        constraints={
            **CLOCK,
            "multicycle_paths": [FEEDBACK, {**FEEDBACK, "from": launch, "to": capture}],
        },
    )
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    assert main(["place-and-route", str(path), "--format", "json"]) == 1
    captured = capsys.readouterr()
    message = json.loads(captured.err or captured.out)["error"]["message"]
    assert "constraints.multicycle_paths[1]" in message and expected in message
    assert not list(tmp_path.rglob("*_place.tcl"))


def _checkpoint_slacks(tmp_path, script, *, apply_exception):
    lines = script.read_text().splitlines()
    if not apply_exception:
        lines = [line for line in lines if not line.startswith("set_multicycle_path ")]
    # Query only the two paths, after the original checkpoint, constraints
    # and SPEF have been loaded. Both runs reuse the same physical artifacts.
    lines += [
        "foreach dest {ff_fb ff_adj} {",
        "    foreach side {max min} {",
        "        set pe [lindex [find_timing_paths -from [get_cells ff_fb]"
        " -to [get_cells $dest] -path_delay $side -group_path_count 1] 0]",
        '        puts "PATH_SLACK $dest $side [expr {[$pe slack] * 1e9}]"',
        "    }",
        "}",
    ]
    path = tmp_path / f"probe_{apply_exception}.tcl"
    path.write_text("\n".join(lines) + "\n")
    ran = subprocess.run(
        ["openroad", "-no_init", "-exit", str(path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    slacks = {}
    for line in ran.stdout.splitlines():
        if line.startswith("PATH_SLACK "):
            _, register, side, value = line.split()
            slacks[register, side] = float(value)
    return slacks


@_LIVE_ONLY
def test_routed_feedback_and_adjacent_path_match_standalone_sta(tmp_path, monkeypatch):
    monkeypatch.setenv("PDK", "sky130A")
    (tmp_path / "top.v").write_text(_feedback_netlist())
    request = _base_request(
        netlist="top.v",
        hdl_toplevel="top",
        post_route_spef=True,
        constraints={**CLOCK, "multicycle_paths": [FEEDBACK]},
    )
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    routed = run_place_and_route(str(path))
    assert routed["status"] == "ok"
    script = Path(routed["def_path"]).parent / "pnr_top_route_spef.tcl"
    results = {}
    for enabled in (False, True):
        observed = _checkpoint_slacks(tmp_path, script, apply_exception=enabled)
        constraints = {**CLOCK, "report_instances": ["ff_fb", "ff_adj"]}
        if enabled:
            constraints["multicycle_paths"] = [FEEDBACK]
        sta = {
            "def": routed["def_path"],
            "spef": routed["spef_sta"]["spef_path"],
            "pdk": request["pdk"],
            "constraints": constraints,
        }
        comparison = tmp_path / f"sta_{enabled}.json"
        comparison.write_text(json.dumps(sta))
        paths = run_sta(str(comparison), pdk_variant="sky130A")["instance_paths"]
        for dest in ("ff_fb", "ff_adj"):
            for side, kind in (("max", "setup"), ("min", "hold")):
                path_record = paths[dest]["into"][kind]
                assert path_record["startpoint"] == "ff_fb/Q"
                assert path_record["endpoint"] == dest + "/D"
                assert path_record["slack_ns"] == pytest.approx(
                    observed[dest, side], abs=1e-5
                )
        results[enabled] = observed
    plain, constrained = results[False], results[True]
    assert plain["ff_fb", "max"] < 0 <= constrained["ff_fb", "max"]
    assert constrained["ff_fb", "max"] - plain["ff_fb", "max"] == pytest.approx(
        6.0, abs=1e-5
    )
    for unchanged in (("ff_fb", "min"), ("ff_adj", "max"), ("ff_adj", "min")):
        assert plain[unchanged] == pytest.approx(constrained[unchanged], abs=1e-5)
