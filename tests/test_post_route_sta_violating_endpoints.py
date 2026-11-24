"""The bounded violating-endpoint list for `klt sta` (issue #7418):
`constraints.max_violating_endpoints`, the `violating_endpoints` response
field, and the marker-block Tcl/parser behind them. The implementation
lives in `klayout_tools/post_route_sta_violating_endpoints.py`.

`tests/fixtures/sta_violating_endpoints/sg13g2_capture.json` is real engine
output (OpenROAD 26Q3-1510 `sta` binary, 2026-11-23) replayed through the
stub helpers `test_post_route_sta.py` exports -- the same replay pattern
`test_post_route_sta_path_groups.py` uses for the sky130 boundary capture.
The live-engine check of the same flow is the `_LIVE_ONLY`-gated test
at the bottom (the sg13g2 capture design is combinational-only because the
available liberty has no flip-flops, so its rows are all output-setup /
output-hold checks against virtual capture clocks).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import post_route_sta
from klayout_tools.post_route_sta import run_sta
from klayout_tools.post_route_sta_errors import PostRouteStaError
from test_post_route_sta import (
    _LIVE_ONLY,
    _STA_METRICS,
    _setup_multi_corner_env,
    _setup_verilog_success_env,
    fake_completed,
)
from test_sta_violating_endpoint_rows import (
    _EXPECTED_HOLD,
    _EXPECTED_SETUP,
    _VIOLATING_ENDPOINT_CAPTURES,
    _capture_block_stdout,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _stub_openroad_block(
    monkeypatch,
    block_stdout: str | None,
    *,
    setup_violations: int,
    hold_violations: int,
    metrics: dict | None = None,
    block_by_corner: dict[str, str] | None = None,
) -> list[str]:
    """Stand in for one `openroad` run whose stdout carries the violating
    count blocks plus a violating-endpoint block. `block_by_corner` (a
    `{"<corner>": <block>}` map, `None` key for a single-corner run) picks a
    distinct block per corner session like `_stub_openroad_capture` does;
    returns the generated scripts."""
    metrics = metrics if metrics is not None else _STA_METRICS
    scripts: list[str] = []

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return fake_completed(stdout="unknown \n")
        assert cmd[0] == "openroad"
        metrics_path, script_path = cmd[4], cmd[5]
        script_text = Path(script_path).read_text(encoding="utf-8")
        scripts.append(script_text)
        with open(metrics_path, "w", encoding="utf-8") as handle:
            json.dump(metrics, handle)
        chosen = block_stdout
        if block_by_corner is not None:
            corner = next(
                (c for c in block_by_corner if c and f"__{c}.lib" in script_text),
                None,
            )
            chosen = block_by_corner[corner]
        stdout_lines = [
            post_route_sta._SETUP_VIOLATIONS_BEGIN,
            *[f"pin_{i} (VIOLATED)" for i in range(setup_violations)],
            post_route_sta._SETUP_VIOLATIONS_END,
            post_route_sta._HOLD_VIOLATIONS_BEGIN,
            *[f"pin_{i} (VIOLATED)" for i in range(hold_violations)],
            post_route_sta._HOLD_VIOLATIONS_END,
        ]
        if chosen is not None:
            stdout_lines.append(chosen)
        return fake_completed(returncode=0, stdout="\n".join(stdout_lines) + "\n")

    monkeypatch.setattr(post_route_sta.subprocess, "run", fake_run)
    return scripts


# -- validation ------------------------------------------------------------- #


def test_validate_max_violating_endpoints_omitted_is_none():
    assert post_route_sta._validate_max_violating_endpoints({}) is None
    assert (
        post_route_sta._validate_max_violating_endpoints(
            {"max_violating_endpoints": None}
        )
        is None
    )


def test_validate_max_violating_endpoints_accepts_positive_integers():
    constraints = {"max_violating_endpoints": 10}
    assert post_route_sta._validate_max_violating_endpoints(constraints) == 10
    assert (
        post_route_sta._validate_max_violating_endpoints({"max_violating_endpoints": 1})
        == 1
    )


@pytest.mark.parametrize(
    "value",
    [True, False, 0, -1, 2.5, 10.0, "10", [10], {"n": 10}],
)
def test_validate_max_violating_endpoints_rejects_non_positive_integers(value):
    with pytest.raises(
        PostRouteStaError,
        match="request.constraints.max_violating_endpoints must be a positive integer",
    ):
        post_route_sta._validate_max_violating_endpoints(
            {"max_violating_endpoints": value}
        )


def test_run_sta_rejects_bad_knob_before_the_engine_runs(tmp_path, monkeypatch):
    request_path = _setup_verilog_success_env(
        tmp_path,
        monkeypatch,
        constraints={
            "clock_port": "clk",
            "clock_period_ns": 1.1,
            "max_violating_endpoints": 0,
        },
    )
    with pytest.raises(
        PostRouteStaError,
        match="request.constraints.max_violating_endpoints",
    ):
        run_sta(request_path)


# -- Tcl emission ----------------------------------------------------------- #


def test_violating_endpoint_lines_omitted_emits_nothing():
    assert post_route_sta._violating_endpoint_lines(None) == []


def test_violating_endpoint_lines_asks_cap_plus_one_per_group():
    """N+1 rows per group is what makes `*_truncated` derivable without a
    second engine query -- the cap applies after a global resort."""
    assert "-group_path_count 11" in "\n".join(
        post_route_sta._violating_endpoint_lines(10)
    )
    assert "-group_path_count 2 " in "\n".join(
        post_route_sta._violating_endpoint_lines(1)
    )


def test_violating_endpoint_lines_quotes_the_group_name_as_a_list():
    """`-path_group` is list-valued: a bare multi-word group name
    (`{gated clock}`) splits into unknown names that only warn and silently
    disable the filter (verified live, issue #7418)."""
    assert "-path_group [list $klt_ve_group]" in "\n".join(
        post_route_sta._violating_endpoint_lines(5)
    )


def test_violating_endpoint_lines_filters_strictly_negative_slack():
    """`-slack_max` is inclusive at the boundary (verified live), so the
    strict `$s < 0` comparison is what keeps a zero-slack endpoint out of
    the list -- the population `report_check_types -violators` counts."""
    assert any(
        line == "                if {$klt_ve_s < 0} {"
        for line in post_route_sta._violating_endpoint_lines(5)
    )


def test_violating_endpoint_lines_query_each_distinct_group_once_per_side():
    """`sta::path_group_names` can list one name twice (a design clock named
    `asynchronous` or `unconstrained` collides with the built-in group), and
    the name-keyed query then returns that group's endpoints twice -- so the
    seen-list must reset per side, guard the loop body, and be recorded
    *before* the query. Verified live on OpenSTA 26Q3-1510 (PR #7449)."""
    text = "\n".join(post_route_sta._violating_endpoint_lines(5))
    ordered = [
        "foreach klt_ve_side {max min} {",
        "set klt_ve_seen {}",
        "foreach klt_ve_group [sta::path_group_names] {",
        "if {[lsearch -exact $klt_ve_seen $klt_ve_group] >= 0} {",
        "continue",
        "lappend klt_ve_seen $klt_ve_group",
        "foreach klt_pe [find_timing_paths",
    ]
    positions = [text.index(fragment) for fragment in ordered]
    assert positions == sorted(positions)
    assert text.count("sta::path_group_names") == 1


def test_capture_used_the_shipped_tcl():
    """Fixture drift guard: the capture must come from the Tcl this module
    emits today (the ask-11 block is the knob-10 request)."""
    assert _VIOLATING_ENDPOINT_CAPTURES[
        "violating_endpoints_tcl_ask_11"
    ] == post_route_sta._violating_endpoint_lines(10)


# -- run_sta integration ---------------------------------------------------- #


def test_run_sta_reports_violating_endpoints_verilog_mode(tmp_path, monkeypatch):
    request_path = _setup_verilog_success_env(
        tmp_path,
        monkeypatch,
        constraints={
            "clock_port": "clk",
            "clock_period_ns": 1.1,
            "max_violating_endpoints": 10,
        },
    )
    scripts = _stub_openroad_block(
        monkeypatch,
        _capture_block_stdout(),
        setup_violations=3,
        hold_violations=3,
    )

    report = run_sta(request_path)

    assert report["violating_endpoints"] == {
        "status": "ok",
        "reason": None,
        "setup": _EXPECTED_SETUP,
        "setup_truncated": False,
        "hold": _EXPECTED_HOLD,
        "hold_truncated": False,
    }
    assert report["setup_violation_count"] == 3
    assert report["hold_violation_count"] == 3
    # The knob reached the generated Tcl with the cap+1 per-group ask.
    assert any("-group_path_count 11" in script for script in scripts)


def test_run_sta_omitted_knob_is_byte_identical(tmp_path, monkeypatch):
    """No knob: no Tcl for the block, and no field on the response."""
    request_path = _setup_verilog_success_env(
        tmp_path,
        monkeypatch,
        constraints={"clock_port": "clk", "clock_period_ns": 1.1},
    )
    scripts = _stub_openroad_block(
        monkeypatch, None, setup_violations=1, hold_violations=0
    )

    report = run_sta(request_path)

    assert "violating_endpoints" not in report
    assert all(
        "===KLT_STA_VIOLATING_ENDPOINTS_BEGIN===" not in script for script in scripts
    )


def test_run_sta_def_mode_reports_violating_endpoints(tmp_path, monkeypatch):
    from test_post_route_sta import _setup_success_env

    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        constraints={
            "clock_port": "clk",
            "clock_period_ns": 2.0,
            "max_violating_endpoints": 3,
        },
    )
    _stub_openroad_block(
        monkeypatch,
        _capture_block_stdout(),
        setup_violations=3,
        hold_violations=3,
    )

    report = run_sta(request_path)

    assert report["violating_endpoints"]["status"] == "ok"
    assert len(report["violating_endpoints"]["setup"]) == 3
    assert report["violating_endpoints"]["setup_truncated"] is False


def test_run_sta_multi_corner_carries_the_field_per_corner(tmp_path, monkeypatch):
    request_path = _setup_multi_corner_env(
        tmp_path,
        monkeypatch,
        constraints={
            "clock_port": "clk",
            "clock_period_ns": 2.0,
            "max_violating_endpoints": 10,
        },
    )
    _stub_openroad_block(
        monkeypatch,
        None,
        setup_violations=3,
        hold_violations=3,
        block_by_corner={
            "tt_025C_1v80": _capture_block_stdout(),
            "ss_100C_1v60": _capture_block_stdout(),
        },
    )

    report = run_sta(request_path)

    assert "violating_endpoints" not in report
    tt_entry, ss_entry = report["corners"]
    for entry in (tt_entry, ss_entry):
        block = entry["violating_endpoints"]
        assert block["status"] == "ok"
        assert [row["endpoint"] for row in block["hold"]] == ["y3", "y1", "y2"]
    # Per-corner counts come from each corner's own session.
    assert tt_entry["hold_violation_count"] == 3
    assert ss_entry["hold_violation_count"] == 3


# -- live engine (openroad on PATH + PDK_ROOT with sky130A) ------------------ #


@_LIVE_ONLY
def test_live_engine_boundary_violating_endpoints(tmp_path):
    """Engine integration: the boundary request's two hold violators (the
    removal check and the `bus[1]` data hold behind `hold_violation_count:
    2`) are listed worst-first, complete at cap 10, truncated at cap 1."""
    from test_post_route_sta import (
        _WORST_PATH_CAPTURES,
        _boundary_constraints,
        _write,
    )

    _write(tmp_path / "boundary.v", _WORST_PATH_CAPTURES["netlist"])
    request = {
        "verilog": "boundary.v",
        "hdl_toplevel": "top",
        "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
        "constraints": {
            **_boundary_constraints(),
            "max_violating_endpoints": 10,
        },
    }
    request_path = _setup_live_request(tmp_path, request)

    report = run_sta(request_path, pdk_variant="sky130A")

    block = report["violating_endpoints"]
    assert block["status"] == "ok", block["reason"]
    assert block["setup"] == []
    assert block["setup_truncated"] is False
    assert [(row["startpoint"], row["endpoint"]) for row in block["hold"]] == [
        ("rst_n", "ff_a/RESET_B"),
        ("bus[1]", "ff_bus1/D"),
    ]
    assert block["hold_truncated"] is False
    assert len(block["hold"]) == report["hold_violation_count"]

    request["constraints"]["max_violating_endpoints"] = 1
    request_path = _setup_live_request(tmp_path, request)
    report = run_sta(request_path, pdk_variant="sky130A")
    block = report["violating_endpoints"]
    assert len(block["hold"]) == 1
    assert block["hold"][0]["endpoint"] == "ff_a/RESET_B"
    assert block["hold_truncated"] is True


@_LIVE_ONLY
@pytest.mark.parametrize("clock_name", ["asynchronous", "unconstrained"])
@pytest.mark.parametrize("cap", [2, 10])
def test_live_engine_clock_named_like_a_builtin_group_lists_each_endpoint_once(
    tmp_path, clock_name, cap
):
    """PR #7449 review: a design clock named like a built-in path group makes
    `sta::path_group_names` list that name twice. Before the per-side
    distinct-name dedupe, `asynchronous` returned `ff_a/RESET_B` (and
    `ff_bus1/D`) twice with `status: "inconsistent"`, and `unconstrained` at
    cap 2 reported a false `hold_truncated`. The same two hold violators as
    the `clk` case must come back once each, complete, and consistent."""
    import re

    from test_post_route_sta import (
        _WORST_PATH_CAPTURES,
        _boundary_constraints,
        _write,
    )

    original = _WORST_PATH_CAPTURES["netlist"]
    netlist = re.sub(r"\bclk\b", clock_name, original)
    # The rename must have taken effect everywhere (`\bclk\b` is
    # case-sensitive, so the flops' `.CLK(...)` pins are left alone).
    assert netlist != original
    assert not re.search(r"\bclk\b", netlist)
    _write(tmp_path / "boundary.v", netlist)
    request = {
        "verilog": "boundary.v",
        "hdl_toplevel": "top",
        "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
        "constraints": {
            **_boundary_constraints(clock_port=clock_name),
            "max_violating_endpoints": cap,
        },
    }
    report = run_sta(_setup_live_request(tmp_path, request), pdk_variant="sky130A")

    block = report["violating_endpoints"]
    assert block["status"] == "ok", block["reason"]
    assert [(row["startpoint"], row["endpoint"]) for row in block["hold"]] == [
        ("rst_n", "ff_a/RESET_B"),
        ("bus[1]", "ff_bus1/D"),
    ]
    assert block["hold_truncated"] is False
    assert len(block["hold"]) == report["hold_violation_count"] == 2


def _setup_live_request(tmp_path, request: dict) -> str:
    import json as _json

    path = tmp_path / "request.json"
    path.write_text(_json.dumps(request), encoding="utf-8")
    return str(path)
