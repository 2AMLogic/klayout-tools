"""The bounded violating-endpoint list for `klt sta` (issue #7418):
`constraints.max_violating_endpoints`, the `violating_endpoints` response
field, and the marker-block Tcl/parser behind them. The implementation
lives in `klayout_tools/post_route_sta_violating_endpoints.py`.

`tests/fixtures/sta_violating_endpoints/sg13g2_capture.json` is real engine
output (OpenROAD 26Q3-1510 `sta` binary, 2026-11-23) replayed through the
stub helpers `test_post_route_sta.py` exports -- the same replay pattern
`test_post_route_sta_path_groups.py` uses for the sky130 boundary capture.
The live-engine check of the same flow is the `_live_sky130_env`-gated test
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
from klayout_tools.post_route_sta_violating_endpoints import (
    _VIOLATING_ENDPOINTS_BEGIN,
    _VIOLATING_ENDPOINTS_END,
    _violating_endpoints,
)
from test_post_route_sta import (
    _STA_METRICS,
    _setup_multi_corner_env,
    _setup_verilog_success_env,
    fake_completed,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_VIOLATING_ENDPOINT_CAPTURES = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "sta_violating_endpoints"
        / "sg13g2_capture.json"
    ).read_text(encoding="utf-8")
)

_CAPTURE = _VIOLATING_ENDPOINT_CAPTURES["scenarios"]["two_group_violators"]

#: The three violating endpoints per side the capture's design
#: (`sg13g2_capture.json`'s netlist + SDC) reports, worst first after the
#: global resort. Expected rounded from the captured full-precision slacks.
_EXPECTED_SETUP = [
    {
        "check_type": "output_setup",
        "engine_check_role": "output setup",
        "startpoint": "b",
        "endpoint": "y2",
        "slack_ns": -0.17954,
        "path_group": "oclk1",
    },
    {
        "check_type": "output_setup",
        "engine_check_role": "output setup",
        "startpoint": "a",
        "endpoint": "y1",
        "slack_ns": -0.15888,
        "path_group": "oclk1",
    },
    {
        "check_type": "output_setup",
        "engine_check_role": "output setup",
        "startpoint": "b",
        "endpoint": "y3",
        "slack_ns": -0.15111,
        "path_group": "oclk2",
    },
]
_EXPECTED_HOLD = [
    {
        "check_type": "output_hold",
        "engine_check_role": "output hold",
        "startpoint": "a",
        "endpoint": "y3",
        "slack_ns": -0.26624,
        "path_group": "oclk2",
    },
    {
        "check_type": "output_hold",
        "engine_check_role": "output hold",
        "startpoint": "a",
        "endpoint": "y1",
        "slack_ns": -0.24449,
        "path_group": "oclk1",
    },
    {
        "check_type": "output_hold",
        "engine_check_role": "output hold",
        "startpoint": "b",
        "endpoint": "y2",
        "slack_ns": -0.22058,
        "path_group": "oclk1",
    },
]


def _capture_block_stdout() -> str:
    return _CAPTURE["stdout_block_ask_11"]


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


def test_capture_used_the_shipped_tcl():
    """Fixture drift guard: the capture must come from the Tcl this module
    emits today (the ask-11 block is the knob-10 request)."""
    assert _VIOLATING_ENDPOINT_CAPTURES[
        "violating_endpoints_tcl_ask_11"
    ] == post_route_sta._violating_endpoint_lines(10)


# -- parsing and the response block ----------------------------------------- #


def test_violating_endpoints_from_the_capture(tmp_path, monkeypatch):
    """Real engine output: both sides globally worst-first (the hold side's
    worst row lives in the second-queried group, so this exercises the
    resort), counts agreeing, nothing truncated."""
    block = _violating_endpoints(
        _capture_block_stdout(),
        10,
        _CAPTURE["violator_counts"]["setup"],
        _CAPTURE["violator_counts"]["hold"],
    )
    assert block == {
        "status": "ok",
        "reason": None,
        "setup": _EXPECTED_SETUP,
        "setup_truncated": False,
        "hold": _EXPECTED_HOLD,
        "hold_truncated": False,
    }


def test_violating_endpoints_resorts_across_groups():
    """The engine returns rows group-major with per-group order only
    (verified live: the capture's hold side is y1, y2, y3 in emission
    order); the response must be worst-first across groups."""
    hold_endpoints = [row["endpoint"] for row in _EXPECTED_HOLD]
    assert hold_endpoints == ["y3", "y1", "y2"]


def test_violating_endpoints_truncates_after_the_global_resort():
    block = _violating_endpoints(_capture_block_stdout(), 2, 3, 3)
    assert block["status"] == "ok"
    assert block["setup"] == _EXPECTED_SETUP[:2]
    assert block["hold"] == _EXPECTED_HOLD[:2]
    assert block["setup_truncated"] is True
    assert block["hold_truncated"] is True


def test_violating_endpoints_cap_one_keeps_only_the_worst_per_side():
    block = _violating_endpoints(_capture_block_stdout(), 1, 3, 3)
    assert block["setup"] == _EXPECTED_SETUP[:1]
    assert block["hold"] == _EXPECTED_HOLD[:1]
    assert block["setup_truncated"] is True
    assert block["hold_truncated"] is True


def test_violating_endpoints_per_group_ask_recovers_the_whole_list():
    """The ask-2 capture (per-group cap 2, the knob-1 request shape) still
    carries every violating endpoint of this design -- no group holds more
    than 2 -- so a knob-10 parse of it is complete and untruncated."""
    block = _violating_endpoints(_CAPTURE["stdout_block_ask_2"], 10, 3, 3)
    assert block["status"] == "ok"
    assert len(block["setup"]) == 3
    assert len(block["hold"]) == 3
    assert block["setup_truncated"] is False


def test_violating_endpoints_zero_violations_is_ok():
    block = _violating_endpoints(
        "===KLT_STA_VIOLATING_ENDPOINTS_BEGIN===\n"
        "===KLT_STA_VIOLATING_ENDPOINTS_END===\n",
        10,
        0,
        0,
    )
    assert block == {
        "status": "ok",
        "reason": None,
        "setup": [],
        "setup_truncated": False,
        "hold": [],
        "hold_truncated": False,
    }


def test_violating_endpoints_ignores_stray_engine_lines():
    block = _violating_endpoints(
        "===KLT_STA_VIOLATING_ENDPOINTS_BEGIN===\n"
        "Warning 527: something the engine said mid-block\n"
        + _capture_block_stdout().splitlines()[1]
        + "\n===KLT_STA_VIOLATING_ENDPOINTS_END===\n",
        10,
        1,
        0,
    )
    assert block["status"] == "ok"
    assert len(block["setup"]) == 1
    assert block["hold"] == []


def test_violating_endpoints_ties_keep_engine_order():
    stdout = (
        "===KLT_STA_VIOLATING_ENDPOINTS_BEGIN===\n"
        "VPATH\tmax\trecovery\t-1e-10\trst\tff_a/RESET_B\tasynchronous\n"
        "VPATH\tmax\tsetup\t-1e-10\ta\tff_b/D\tclk\n"
        "===KLT_STA_VIOLATING_ENDPOINTS_END===\n"
    )
    block = _violating_endpoints(stdout, 10, 2, 0)
    assert [row["engine_check_role"] for row in block["setup"]] == [
        "recovery",
        "setup",
    ]


def test_violating_endpoints_async_roles_keep_their_own_check_type():
    stdout = (
        "===KLT_STA_VIOLATING_ENDPOINTS_BEGIN===\n"
        "VPATH\tmax\trecovery\t-4.1e-10\trst_n\tff_a/RESET_B\tasynchronous\n"
        "VPATH\tmin\tremoval\t-2.9e-10\trst_n\tff_a/RESET_B\tasynchronous\n"
        "===KLT_STA_VIOLATING_ENDPOINTS_END===\n"
    )
    block = _violating_endpoints(stdout, 10, 1, 1)
    assert block["setup"][0]["check_type"] == "recovery"
    assert block["hold"][0]["check_type"] == "removal"
    assert block["setup"][0]["path_group"] == "asynchronous"


@pytest.mark.parametrize(
    ("block", "reason"),
    [
        (None, "absent from engine output"),
        (['ERROR\tinvalid command name "find_timing_paths"'], "introspection failed"),
        (["VPATH\tmax\tsetup\t1e-10\ta\tff/D"], "malformed violating-endpoint record"),
        (["VPATH\tsideways\tsetup\t-1e-10\ta\tff/D\tclk"], "malformed"),
        (
            ["VPATH\tmax\tsetup\tfast\ta\tff/D\tclk"],
            "malformed violating-endpoint slack",
        ),
        (["VPATH\tmax\tsetup\t-inf\ta\tff/D\tclk"], "non-finite"),
        (
            ["VPATH\tmax\tsetup\t-1e-10\t\tff/D\tclk"],
            "malformed violating-endpoint record",
        ),
    ],
)
def test_violating_endpoints_unavailable_never_fabricated(block, reason):
    """A failed parse yields empty lists and a reason -- never a partial
    list built from the good rows."""
    if block is None:
        stdout = "no markers here"
    else:
        stdout = "\n".join(
            [
                _VIOLATING_ENDPOINTS_BEGIN,
                *block,
                _VIOLATING_ENDPOINTS_END,
                "",
            ]
        )
    result = _violating_endpoints(stdout, 10, 2, 1)
    assert result["status"] == "unavailable"
    assert reason in result["reason"]
    assert result["setup"] == []
    assert result["hold"] == []
    assert result["setup_truncated"] is False
    assert result["hold_truncated"] is False


# -- count agreement -------------------------------------------------------- #


def test_violating_endpoints_short_list_is_inconsistent():
    """A side whose list is shorter than its violation count (and not
    truncated -- nothing proves more rows exist) is reported, not silently
    shortened."""
    block = _violating_endpoints(_capture_block_stdout(), 10, 2, 3)
    assert block["status"] == "inconsistent"
    assert "setup" in block["reason"]
    assert "setup_violation_count is 2" in block["reason"]
    assert len(block["setup"]) == 3
    assert len(block["hold"]) == 3


def test_violating_endpoints_truncated_count_below_cap_is_inconsistent():
    """Truncation proves the engine found more than the cap, so a count at
    or below the cap cannot describe the same population."""
    block = _violating_endpoints(_capture_block_stdout(), 2, 2, 3)
    assert block["status"] == "inconsistent"
    assert "setup" in block["reason"]
    assert "hold" not in block["reason"]
    assert len(block["setup"]) == 2


def test_violating_endpoints_both_sides_disagree_names_both():
    block = _violating_endpoints(_capture_block_stdout(), 2, 1, 1)
    assert block["status"] == "inconsistent"
    assert "setup" in block["reason"]
    assert "hold" in block["reason"]


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


def _live_sky130_env() -> str | None:
    import os
    import shutil

    if shutil.which("openroad") is None:
        return None
    if os.environ.get("KLT_SKIP_OPENROAD_TESTS") == "1":
        return None
    if not os.environ.get("PDK_ROOT"):
        return None
    try:
        post_route_sta._resolve_liberty(
            "sky130_fd_sc_hd", "tt_025C_1v80", variant="sky130A"
        )
    except PostRouteStaError:
        return None
    return "sky130A"


@pytest.mark.skipif(
    _live_sky130_env() is None,
    reason="needs openroad on PATH, PDK_ROOT with a sky130A install, and "
    "KLT_SKIP_OPENROAD_TESTS unset",
)
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


def _setup_live_request(tmp_path, request: dict) -> str:
    import json as _json

    path = tmp_path / "request.json"
    path.write_text(_json.dumps(request), encoding="utf-8")
    return str(path)
