"""``run_sta`` end-to-end for ``constraints.multicycle_paths`` (issue
#6921): the stubbed-engine integration tests (generated-script shape and
seam, response fields on both response shapes, request-error surfacing)
and the pinned-engine feedback fixture -- #6908's release criterion: the
same-enable feedback path fails setup at one period and passes at four,
hold is recorded on both runs, the adjacent-phase path keeps its
one-period slack, and selector resolution rejects an unclocked register
and a matched-nothing selector naming the entry index. The offline
validator/emitter/parse tests live in ``test_sta_multicycle.py``; this
file was split from it for the per-file size ratchet, the same way
``test_post_route_sta_instance_paths_run.py`` was."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import post_route_sta
from klayout_tools.post_route_sta import PostRouteStaError, run_sta
from test_post_route_sta import (
    _LIVE_ONLY,
    _STA_METRICS,
    _setup_multi_corner_env,
    _setup_success_env,
    _setup_verilog_success_env,
    _write,
    _write_request,
)
from test_sta_multicycle import _ENTRY, _mcp_block, _mcperr_block

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


# -- run_sta integration (stubbed engine, no engine needed) ----------------- #


def _mcp_stdout(with_slack: float, without_slack: float, hold: float) -> dict:
    """stdout for the with-run (`with_slack`) and the entry-0 without-run
    (`without_slack`), plus the metrics file contents."""
    return {
        "with": _mcp_block(
            f"MCP\t0\tmax\t{with_slack}\tff_fb/D",
            f"MCP\t0\tmin\t{hold}\tff_fb/D",
        ),
        "without": _mcp_block(
            f"MCP\t0\tmax\t{without_slack}\tff_fb/D",
            f"MCP\t0\tmin\t{hold}\tff_fb/D",
        ),
    }


def _stub_openroad_multicycle(monkeypatch, blocks: dict) -> list[str]:
    """Replay the with-run and each ``__mcp<i>`` without-run; ``blocks`` is
    ``{"with": <stdout>, "without": <stdout> | {index: <stdout>}}``. Returns
    every generated script in call order."""
    scripts: list[str] = []

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return fake_completed(stdout="unknown \n")
        metrics_path, script_path = cmd[4], cmd[5]
        script_text = Path(script_path).read_text(encoding="utf-8")
        scripts.append(script_text)
        with open(metrics_path, "w", encoding="utf-8") as handle:
            json.dump(_STA_METRICS, handle)
        without = re.search(r"__mcp(\d+)\.tcl$", script_path)
        if without is None:
            stdout = blocks["with"]
        else:
            outs = blocks["without"]
            stdout = (
                outs.get(int(without.group(1)), "") if isinstance(outs, dict) else outs
            )
        return fake_completed(returncode=0, stdout=stdout)

    monkeypatch.setattr(post_route_sta.subprocess, "run", fake_run)
    return scripts


def _multicycle_constraints(**overrides) -> dict:
    constraints = {
        "clock_port": "clk",
        "clock_period_ns": 2.0,
        "multicycle_paths": [_ENTRY],
    }
    constraints.update(overrides)
    return constraints


def test_run_sta_script_emits_block_between_delays_and_reports(tmp_path, monkeypatch):
    request_path = _setup_verilog_success_env(
        tmp_path, monkeypatch, constraints=_multicycle_constraints()
    )
    scripts = _stub_openroad_multicycle(
        monkeypatch, _mcp_stdout(5.46515, -0.53485, 2.29215)
    )
    report = run_sta(request_path)
    script = scripts[0]
    assert "set_multicycle_path 4 -setup" in script
    # after the clock/IO constraint seam, before every report command
    assert script.index("create_clock") < script.index("set_multicycle_path 4 -setup")
    assert script.index("set_multicycle_path 4 -setup") < script.index(
        "report_worst_slack_metric -setup"
    )
    (record,) = report["multicycle_paths"]
    assert record["setup_slack_ns"] == {"with": 5.46515, "without": -0.53485}
    assert record["matched_endpoints"] == 1


def test_run_sta_writes_one_without_script_per_entry(tmp_path, monkeypatch):
    request_path = _setup_verilog_success_env(
        tmp_path,
        monkeypatch,
        constraints=_multicycle_constraints(
            multicycle_paths=[_ENTRY, {**_ENTRY, "from": "ff_b", "to": "ff_c"}]
        ),
    )
    blocks = {
        "with": _mcp_block(
            "MCP\t0\tmax\t5.4\tff_fb/D",
            "MCP\t0\tmin\t2.2\tff_fb/D",
            "MCP\t1\tmax\t3.3\tff_c/D",
            "MCP\t1\tmin\t1.1\tff_c/D",
        ),
        "without": _mcp_block(
            "MCP\t0\tmax\t-0.5\tff_fb/D",
            "MCP\t0\tmin\t2.2\tff_fb/D",
            "MCP\t1\tmax\t-0.6\tff_c/D",
            "MCP\t1\tmin\t1.1\tff_c/D",
        ),
    }
    scripts = _stub_openroad_multicycle(monkeypatch, blocks)
    report = run_sta(request_path)
    assert len(scripts) == 3  # with-run + one without-run per entry
    without0 = scripts[1]
    # entry 0's pair dropped, entry 1's kept -- the unambiguous baseline
    assert without0.count("set_multicycle_path") == 2
    assert not any(line.endswith(";# klt mcp 0") for line in without0.splitlines())
    assert any(line.endswith(";# klt mcp 1") for line in without0.splitlines())
    (first, second) = report["multicycle_paths"]
    assert first["index"] == 0 and second["index"] == 1
    assert first["setup_slack_ns"] == {"with": 5.4, "without": -0.5}
    assert second["setup_slack_ns"] == {"with": 3.3, "without": -0.6}


def test_run_sta_omitted_and_empty_leave_tcl_and_response_unchanged(
    tmp_path, monkeypatch
):
    plain_path = _setup_verilog_success_env(
        tmp_path,
        monkeypatch,
        constraints=_multicycle_constraints(multicycle_paths=None),
    )
    scripts_plain = _stub_openroad_multicycle(monkeypatch, {"with": "", "without": {}})
    plain = run_sta(plain_path)

    empty_path = _setup_verilog_success_env(
        tmp_path, monkeypatch, constraints=_multicycle_constraints(multicycle_paths=[])
    )
    scripts_empty = _stub_openroad_multicycle(monkeypatch, {"with": "", "without": {}})
    empty = run_sta(empty_path)

    assert scripts_plain[0] == scripts_empty[0]
    assert "request_sha256" not in plain["provenance"]
    assert "request_sha256" not in empty["provenance"]
    assert "multicycle_paths" not in empty
    assert set(plain) == set(empty)


def test_run_sta_def_mode_script_also_carries_the_block(tmp_path, monkeypatch):
    request_path = _setup_success_env(
        tmp_path, monkeypatch, constraints=_multicycle_constraints()
    )
    scripts = _stub_openroad_multicycle(monkeypatch, _mcp_stdout(1.0, -1.0, 0.5))
    report = run_sta(request_path)
    assert "set_multicycle_path 4 -setup" in scripts[0]
    assert report["multicycle_paths"][0]["setup_slack_ns"]["without"] == -1.0


def test_run_sta_multi_corner_reports_each_corner(tmp_path, monkeypatch):
    request_path = _setup_multi_corner_env(
        tmp_path,
        monkeypatch,
        constraints=_multicycle_constraints(),
    )
    _stub_openroad_multicycle(monkeypatch, _mcp_stdout(5.46515, -0.53485, 2.29215))
    report = run_sta(request_path)
    assert len(report["corners"]) == 2
    for corner in report["corners"]:
        (record,) = corner["multicycle_paths"]
        assert record["setup_slack_ns"] == {"with": 5.46515, "without": -0.53485}
    # per-corner only, never hoisted: each corner's session measured its own
    assert "multicycle_paths" not in report


def test_run_sta_resolution_error_is_request_error_naming_index(tmp_path, monkeypatch):
    request_path = _setup_verilog_success_env(
        tmp_path, monkeypatch, constraints=_multicycle_constraints()
    )
    _stub_openroad_multicycle(
        monkeypatch,
        {"with": _mcperr_block("MCPERR\t0\tfrom unclocked_register ff_u")},
    )
    with pytest.raises(PostRouteStaError) as excinfo:
        run_sta(request_path)
    message = str(excinfo.value)
    assert "constraints.multicycle_paths rejected" in message
    assert (
        "constraints.multicycle_paths[0].from selects unclocked register 'ff_u'"
        in message
    )


def test_run_sta_no_timed_path_is_request_error(tmp_path, monkeypatch):
    request_path = _setup_verilog_success_env(
        tmp_path, monkeypatch, constraints=_multicycle_constraints()
    )
    _stub_openroad_multicycle(monkeypatch, {"with": _mcp_block("")})
    with pytest.raises(PostRouteStaError, match="no timed path runs through it"):
        run_sta(request_path)


@pytest.mark.parametrize("failed_sides", [("max", "min"), ("max",), ("min",)])
def test_failed_with_query_preserves_diagnostic(tmp_path, monkeypatch, failed_sides):
    request = _setup_verilog_success_env(
        tmp_path, monkeypatch, constraints=_multicycle_constraints()
    )
    blocks = _mcp_stdout(1.0, -1.0, 0.5)
    blocks["with"] = _mcp_block(
        *(f"MCPERR\t0\t{side}\tno such object" for side in failed_sides)
    )
    _stub_openroad_multicycle(monkeypatch, blocks)
    (record,) = run_sta(request)["multicycle_paths"]
    assert record["setup_slack_ns"]["with"] is None
    assert record["hold_slack_ns"]["with"] is None
    assert "no such object" in record["reason"]


def test_run_sta_without_run_failure_surfaces(tmp_path, monkeypatch):
    request_path = _setup_verilog_success_env(
        tmp_path, monkeypatch, constraints=_multicycle_constraints()
    )

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return fake_completed(stdout="unknown \n")
        with open(cmd[4], "w", encoding="utf-8") as handle:
            json.dump(_STA_METRICS, handle)
        if "__mcp0" in cmd[5]:
            return fake_completed(returncode=1, stdout="boom")
        return fake_completed(
            returncode=0,
            stdout=_mcp_block("MCP\t0\tmax\t1.0\tff_fb/D", "MCP\t0\tmin\t0.5\tff_fb/D"),
        )

    monkeypatch.setattr(post_route_sta.subprocess, "run", fake_run)
    with pytest.raises(PostRouteStaError, match="openroad"):
        run_sta(request_path)


def _feedback_netlist(stages: int = 60) -> str:
    """A mapped, enabled-feedback fixture: ``ff_fb`` recaptures its own Q
    through a ``stages``-long inverter chain and an enable mux (the
    same-enable loop, worth four periods), ``ff_adj`` captures ``q_fb``
    every cycle (the adjacent-phase one-period path), and ``ff_u`` is a
    register whose clock comes from a plain input (never clocked)."""
    wires = ", ".join(f"t{i}" for i in range(stages))
    cells = [
        f"  sky130_fd_sc_hd__inv_1 u_g{i} "
        f"(.A({'q_fb' if i == 0 else f't{i - 1}'}), .Y(t{i}));"
        for i in range(stages)
    ]
    cells.append(
        f"  sky130_fd_sc_hd__mux2_1 u_mux "
        f"(.A0(d_in), .A1(t{stages - 1}), .S(en), .X(mux_out));"
    )
    return f"""module top (clk, d_in, en, slow_en, q_fb, q_next);
  input clk, d_in, en, slow_en;
  output q_fb, q_next;
  wire mux_out;
  wire {wires};
  sky130_fd_sc_hd__dfxtp_1 ff_fb (.CLK(clk), .D(mux_out), .Q(q_fb));
{chr(10).join(cells)}
  sky130_fd_sc_hd__dfxtp_1 ff_adj (.CLK(clk), .D(q_fb), .Q(q_next));
  sky130_fd_sc_hd__dfxtp_1 ff_u (.CLK(slow_en), .D(q_fb), .Q());
endmodule
"""


def _feedback_request(tmp_path, multicycle: bool, *, entry=_ENTRY) -> str:
    _write(tmp_path / "fb.v", _feedback_netlist())
    constraints = {
        "clock_port": "clk",
        "clock_period_ns": 2.0,
        "report_instances": ["ff_adj", "ff_fb"],
    }
    if multicycle:
        constraints["multicycle_paths"] = [entry]
    return _write_request(
        tmp_path / "request.json",
        {
            "verilog": "fb.v",
            "hdl_toplevel": "top",
            "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
            "constraints": constraints,
        },
    )


@_LIVE_ONLY
@pytest.mark.parametrize("entry", [_ENTRY, {**_ENTRY, "through": "u_mux/X"}])
def test_live_engine_multicycle_feedback_four_periods(tmp_path, entry):
    """The #6908 release criterion on the pinned engine: the same-enable
    feedback path fails setup at one period (the entry's "without" run) and
    passes at four (the "with" run); hold is recorded on both; the
    adjacent-phase path keeps its one-period slack; the unclocked register
    stays reported as unclocked -- an exception never converts it."""
    report = run_sta(
        _feedback_request(tmp_path, multicycle=True, entry=entry), pdk_variant="sky130A"
    )

    (record,) = report["multicycle_paths"]
    assert record["matched_endpoints"] == 1
    assert record["setup_slack_ns"]["without"] < 0
    assert record["setup_slack_ns"]["with"] >= 0
    for run in ("with", "without"):
        assert record["hold_slack_ns"][run] is not None
    # setup 4 / hold 3 keeps the hold check at the single-cycle edge.
    assert record["hold_slack_ns"]["with"] == pytest.approx(
        record["hold_slack_ns"]["without"], abs=1e-6
    )
    # The exception never converted the unclocked register.
    assert report["constraint_completeness"]["unclocked_registers"]["count"] == 1

    (tmp_path / "plain").mkdir()
    plain = run_sta(
        _feedback_request(tmp_path / "plain", multicycle=False), pdk_variant="sky130A"
    )
    # The adjacent-phase path is checked at one period with and without the
    # entry: bit-identical slack from two independent sessions.
    adjacent_with = report["instance_paths"]["ff_adj"]["into"]["setup"]
    adjacent_plain = plain["instance_paths"]["ff_adj"]["into"]["setup"]
    assert adjacent_with["endpoint"] == "ff_adj/D"
    assert adjacent_with["slack_ns"] == pytest.approx(
        adjacent_plain["slack_ns"], abs=1e-9
    )
    # And the "without" baseline is the same number a request with no
    # exceptions at all reports for the feedback path.
    feedback_plain = plain["instance_paths"]["ff_fb"]["into"]["setup"]
    assert record["setup_slack_ns"]["without"] == pytest.approx(
        feedback_plain["slack_ns"], abs=1e-5
    )


@_LIVE_ONLY
def test_live_engine_multicycle_resolution_errors(tmp_path):
    """Selector resolution on the linked netlist, live: an unclocked
    register and a matched-nothing selector are request errors naming the
    entry index (verilog-linked here; the def-linked script emits the
    identical resolution block, asserted offline above)."""
    _write(tmp_path / "fb.v", _feedback_netlist())
    for selector, fragment in (
        ("ff_u", "selects unclocked register 'ff_u'"),
        # a wildcard selector resolving to the unclocked register: the
        # pattern grammar reaches engine-side resolution too
        ("ff_?", "selects unclocked register 'ff_u'"),
        ("no_such_*", "matches no register or pin"),
    ):
        constraints = {
            "clock_port": "clk",
            "clock_period_ns": 2.0,
            "multicycle_paths": [
                {"from": selector, "to": "ff_fb", "setup": 4, "hold": 3},
            ],
        }
        request_path = _write_request(
            tmp_path / f"request_{selector}.json",
            {
                "verilog": "fb.v",
                "hdl_toplevel": "top",
                "pdk": {
                    "cell_library": "sky130_fd_sc_hd",
                    "corner": "tt_025C_1v80",
                },
                "constraints": constraints,
            },
        )
        with pytest.raises(PostRouteStaError) as excinfo:
            run_sta(request_path, pdk_variant="sky130A")
        assert "constraints.multicycle_paths[0]" in str(excinfo.value)
        assert fragment in str(excinfo.value)


@_LIVE_ONLY
def test_live_engine_multicycle_def_linked_resolution(tmp_path):
    """The same engine-side resolution on a DEF-linked design (the
    curator's unverified item #3): a matched-nothing selector is a request
    error against the LEF/DEF-loaded session too."""
    fixture = Path(__file__).parent / "fixtures" / "sta_electrical_checks"
    request = {
        "def": str(fixture / "top.def"),
        "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "ff_n40C_1v95"},
        "constraints": {
            "clock_port": "clk",
            "clock_period_ns": 2.0,
            "multicycle_paths": [
                {"from": "no_such_reg", "to": "ff1", "setup": 2, "hold": 1},
            ],
        },
    }
    request_path = _write_request(tmp_path / "request.json", request)
    with pytest.raises(PostRouteStaError) as excinfo:
        run_sta(request_path, pdk_variant="sky130A")
    assert "constraints.multicycle_paths[0].from matches no register or pin" in str(
        excinfo.value
    )
