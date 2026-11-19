"""`klt sta` `report_instances` end-to-end behaviour: `run_sta` response
assembly, the engine-side name-check error path, the text renderer and the
real-engine check for `constraints.report_instances` -> `instance_paths`
(issue #5754).

Split out of ``test_post_route_sta_instance_paths.py`` to keep both files
under their size ceiling; the boundary capture fixture and the OpenROAD
stub it replays through are reused from ``test_post_route_sta.py``, and the
synthesized-row helpers from the sibling module. The instance-query stdout
below is synthesized from the *measured* direct-engine values in
`boundary_captures.json` ("direct_queries"), not captured from a
`report_instances` session: no engine is available to the offline suite. The
live engine test at the end of this file is the real-engine check.
"""

from __future__ import annotations

import pytest

from helpers.subprocess_fakes import fake_completed, patch_openroad_run
from klayout_tools import post_route_sta
from klayout_tools.cli import sta_cmd
from klayout_tools.post_route_sta import PostRouteStaError, run_sta
from klayout_tools.post_route_sta_instance_paths import _INSTANCE_PATHS_BEGIN
from test_post_route_sta import (
    _WORST_PATH_CAPTURES,
    _boundary_constraints,
    _live_sky130_env,
    _setup_multi_corner_env,
    _setup_verilog_success_env,
    _stub_openroad_capture,
    _write,
    _write_request,
)
from test_post_route_sta_instance_paths import (
    _direct_instance_rows,
    _ipath_block,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _append_instance_block(monkeypatch, block: str) -> None:
    """Make every stubbed engine run also print ``block`` on stdout."""
    inner = post_route_sta.subprocess.run

    def run(cmd, **kwargs):
        completed = inner(cmd, **kwargs)
        if completed.stdout and post_route_sta._WORST_PATHS_BEGIN in completed.stdout:
            completed.stdout += block
        return completed

    patch_openroad_run(monkeypatch, run)


# -- response assembly ------------------------------------------------------- #


def test_run_sta_report_instances_single_corner(tmp_path, monkeypatch):
    request_path = _setup_verilog_success_env(
        tmp_path,
        monkeypatch,
        constraints=_boundary_constraints(report_instances=["ff_a", "ff_b"]),
    )
    scripts = _stub_openroad_capture(monkeypatch, {None: "boundary_tt_025C_1v80"})
    _append_instance_block(
        monkeypatch,
        _ipath_block(
            *_direct_instance_rows("ff_a", ["a_in", "rst_n"]),
            *_direct_instance_rows("ff_b", ["b_in"]),
        ),
    )

    report = run_sta(request_path)

    assert "set klt_instances_by_name [dict create]" in scripts[0]
    assert _INSTANCE_PATHS_BEGIN in scripts[0]
    assert list(report["instance_paths"]) == ["ff_a", "ff_b"]
    # `into` spans every input pin: ff_a (a dfrtp) takes the async removal
    # check on its reset pin as its worst hold, not the a_in data path.
    hold = report["instance_paths"]["ff_a"]["into"]["hold"]
    assert (hold["check_type"], hold["startpoint"], hold["endpoint"]) == (
        "removal",
        "rst_n",
        "ff_a/RESET_B",
    )
    assert hold["slack_ns"] == pytest.approx(-0.29559, abs=0.01)
    # ff_b is a plain dfxtp: its into hold is the b_in data path.
    b_hold = report["instance_paths"]["ff_b"]["into"]["hold"]
    assert (b_hold["check_type"], b_hold["endpoint"]) == ("hold", "ff_b/D")
    assert b_hold["slack_ns"] == pytest.approx(0.07772, abs=0.01)
    assert report["instance_paths"]["ff_b"]["out_of"]["setup"]["status"] == ("no_paths")


def test_run_sta_report_instances_check_runs_before_timing(tmp_path, monkeypatch):
    request_path = _setup_verilog_success_env(
        tmp_path,
        monkeypatch,
        constraints=_boundary_constraints(report_instances=["ff_a"]),
    )
    scripts = _stub_openroad_capture(monkeypatch, {None: "boundary_tt_025C_1v80"})
    _append_instance_block(
        monkeypatch,
        _ipath_block(*_direct_instance_rows("ff_a", ["a_in", "rst_n"])),
    )

    report = run_sta(request_path)

    script = scripts[0]
    check_at = script.index("set klt_instances_by_name [dict create]")
    assert script.index("report_worst_slack_metric -setup") > check_at
    assert report["instance_paths"]["ff_a"]["into"]["setup"]["status"] == "ok"


def test_run_sta_report_instances_multi_corner_each_entry(tmp_path, monkeypatch):
    request_path = _setup_multi_corner_env(
        tmp_path,
        monkeypatch,
        constraints=_boundary_constraints(report_instances=["ff_a"]),
    )
    _stub_openroad_capture(
        monkeypatch,
        {
            "tt_025C_1v80": "boundary_tt_025C_1v80",
            "ss_100C_1v60": "boundary_ss_100C_1v60",
        },
    )
    _append_instance_block(
        monkeypatch,
        _ipath_block(*_direct_instance_rows("ff_a", ["a_in", "rst_n"])),
    )

    report = run_sta(request_path)

    assert "instance_paths" not in report
    assert len(report["corners"]) == 2
    for entry in report["corners"]:
        assert list(entry["instance_paths"]) == ["ff_a"]
        assert set(entry["instance_paths"]["ff_a"]) == {"into", "out_of"}


def test_run_sta_without_report_instances_has_no_instance_paths(tmp_path, monkeypatch):
    request_path = _setup_verilog_success_env(
        tmp_path, monkeypatch, constraints=_boundary_constraints()
    )
    _stub_openroad_capture(monkeypatch, {None: "boundary_tt_025C_1v80"})

    report = run_sta(request_path)

    assert "instance_paths" not in report


def test_run_sta_report_instances_with_report_ports_both_present(tmp_path, monkeypatch):
    request_path = _setup_verilog_success_env(
        tmp_path,
        monkeypatch,
        constraints=_boundary_constraints(
            report_ports=["a_in"], report_instances=["ff_a"]
        ),
    )
    _stub_openroad_capture(monkeypatch, {None: "boundary_tt_025C_1v80"})
    _append_instance_block(
        monkeypatch,
        _ipath_block(*_direct_instance_rows("ff_a", ["a_in", "rst_n"])),
    )

    report = run_sta(request_path)

    assert list(report["port_paths"]) == ["a_in"]
    assert list(report["instance_paths"]) == ["ff_a"]


# -- error paths ------------------------------------------------------------- #


def test_run_sta_report_instances_rejected_by_engine_check(tmp_path, monkeypatch):
    request_path = _setup_verilog_success_env(
        tmp_path,
        monkeypatch,
        constraints=_boundary_constraints(report_instances=["nope"]),
    )
    block = (
        f"{post_route_sta._PORT_CHECK_BEGIN}\n"
        "report_instance\tnope\tnot_found\n"
        f"{post_route_sta._PORT_CHECK_END}\n"
    )

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return fake_completed(stdout="unknown \n")
        return fake_completed(returncode=3, stdout=block)

    patch_openroad_run(monkeypatch, fake_run)

    with pytest.raises(PostRouteStaError) as excinfo:
        run_sta(request_path)
    message = str(excinfo.value)
    assert message.startswith("constraints.report_instances rejected")
    assert (
        "constraints.report_instances['nope']: no instance with exactly this "
        "name" in message
    )


# -- CLI render -------------------------------------------------------------- #


def test_print_text_renders_instance_paths(capsys):
    record = {
        "status": "ok",
        "check_type": "hold",
        "startpoint": "a_in",
        "endpoint": "ff_a/D",
        "slack_ns": 0.33143,
    }
    empty = post_route_sta._empty_path_record("no_paths", None)
    fields = {
        "worst_slack_ns": 0.1,
        "total_negative_slack_ns": 0,
        "worst_hold_slack_ns": 0.1,
        "total_negative_hold_slack_ns": 0,
        "fmax_mhz": 1,
        "setup_violation_count": 0,
        "hold_violation_count": 0,
        "clock_skew_ns": 0,
        "estimated_power_mw": 0,
        "instance_paths": {
            "ff_a": {
                "into": {"setup": empty, "hold": record},
                "out_of": {"setup": empty, "hold": empty},
            }
        },
    }

    sta_cmd._print_slack_fields(fields, indent="")

    out = capsys.readouterr().out
    assert "instance_paths[ff_a].into.setup: no_paths" in out
    assert (
        "instance_paths[ff_a].into.hold: hold a_in -> ff_a/D "
        "slack_ns=0.33143 (ok)" in out
    )
    assert "instance_paths[ff_a].out_of.setup: no_paths" in out
    assert "instance_paths[ff_a].out_of.hold: no_paths" in out


# -- live engine ------------------------------------------------------------- #


@pytest.mark.skipif(
    _live_sky130_env() is None,
    reason="needs openroad on PATH, PDK_ROOT with a sky130A install, and "
    "KLT_SKIP_OPENROAD_TESTS unset",
)
def test_live_engine_report_instances(tmp_path):
    """Engine integration (issue #5754): the boundary request with
    ``report_instances=["ff_a", "ff_b"]`` against a real session must
    report each flip-flop's own into/out_of arcs, consistent with the
    boundary ports' own ``port_paths`` records from the same session.
    ``into`` spans every input pin: ``ff_a`` is a ``dfrtp`` whose reset pin
    adds the async checks, so its worst hold is the ``rst_n`` removal path
    (not the ``a_in`` data path), while plain ``dfxtp`` ``ff_b`` has no
    such pin and its into hold *is* the ``b_in`` data path."""
    _write(tmp_path / "boundary.v", _WORST_PATH_CAPTURES["netlist"])
    request = {
        "verilog": "boundary.v",
        "hdl_toplevel": "top",
        "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
        "constraints": _boundary_constraints(
            report_ports=["a_in", "b_in", "rst_n"],
            report_instances=["ff_a", "ff_b"],
        ),
    }
    request_path = _write_request(tmp_path / "request.json", request)

    report = run_sta(request_path, pdk_variant="sky130A")

    paths = report["instance_paths"]
    assert list(paths) == ["ff_a", "ff_b"]
    # ff_a: D + RESET_B inputs -- the async removal check on the reset pin
    # is the worst path into the instance on the min side.
    into_a = paths["ff_a"]["into"]
    assert into_a["hold"]["check_type"] == "removal"
    assert (into_a["hold"]["startpoint"], into_a["hold"]["endpoint"]) == (
        "rst_n",
        "ff_a/RESET_B",
    )
    assert into_a["hold"]["slack_ns"] == pytest.approx(
        report["port_paths"]["rst_n"]["hold"]["slack_ns"], abs=1e-6
    )
    # The max side stays the a_in setup path (worse than rst_n recovery).
    assert into_a["setup"]["endpoint"] == "ff_a/D"
    assert into_a["setup"]["slack_ns"] == pytest.approx(
        report["port_paths"]["a_in"]["setup"]["slack_ns"], abs=1e-6
    )
    # ff_b: a plain dfxtp -- its only constrained input data pin is D, so
    # the worst path into it is the b_in data path.
    into_b = paths["ff_b"]["into"]
    assert into_b["hold"]["check_type"] == "hold"
    assert (into_b["hold"]["startpoint"], into_b["hold"]["endpoint"]) == (
        "b_in",
        "ff_b/D",
    )
    assert into_b["hold"]["slack_ns"] == pytest.approx(
        report["port_paths"]["b_in"]["hold"]["slack_ns"], abs=1e-6
    )
    # The Q-pin launch path out of ff_a.
    out_a = paths["ff_a"]["out_of"]
    assert out_a["setup"]["startpoint"] == "ff_a/Q"
    assert out_a["setup"]["endpoint"] == "q_a"
    assert out_a["setup"]["check_type"] == "output_setup"
    assert out_a["hold"]["status"] == "no_paths"
    # The global worst hold stays the unrelated async-reset check.
    assert report["worst_hold_path"]["startpoint"] == "rst_n"
