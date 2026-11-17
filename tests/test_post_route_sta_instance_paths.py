"""Per-instance worst-path queries for `klt sta`:
`constraints.report_instances` -> `instance_paths` (issue #5754). The
validator, Tcl emitters, parser and record builder live in
`klayout_tools/post_route_sta_instance_paths.py`.

Split out of ``test_post_route_sta.py`` to keep that file under its size
ceiling; the boundary capture fixture and the OpenROAD stub it replays
through are reused from there. The instance-query stdout below is
synthesized from the *measured* direct-engine values in
`boundary_captures.json` ("direct_queries"), not captured from a
`report_instances` session: no engine is available to the offline suite. The
live engine test at the end of this file is the real-engine check.
"""

from __future__ import annotations

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import post_route_sta
from klayout_tools.cli import sta_cmd
from klayout_tools.post_route_sta import PostRouteStaError, run_sta
from klayout_tools.post_route_sta_instance_paths import (
    _INSTANCE_PATHS_BEGIN,
    _INSTANCE_PATHS_END,
    _instance_check_lines,
    _instance_path_lines,
    _instance_paths,
    _validate_report_instances,
)
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

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _ipath_block(*rows: str) -> str:
    return "\n".join([_INSTANCE_PATHS_BEGIN, *rows, _INSTANCE_PATHS_END, ""])


def _direct_instance_rows(instance: str, input_ports: list[str]) -> list[str]:
    """``IPATH`` rows for ``instance``'s *into* side, synthesized from the
    fixture's measured direct port queries. A real ``-to`` query spans
    *every* input pin of the instance and keeps the single worst path per
    check side, so these rows take the worst direct query per side across
    ``input_ports``. For ``ff_a`` (a ``dfrtp``: ``D`` *and* ``RESET_B``)
    that is the ``rst_n`` removal path on ``min`` -- not the ``a_in`` data
    hold path the ``-from a_in`` query measured."""
    direct = _WORST_PATH_CAPTURES["direct_queries"]["tt_025C_1v80"]["from"]
    worst: dict[str, tuple[str, float, str, str]] = {}
    for port in input_ports:
        for side, capture in direct[port].items():
            if capture is not None and (
                side not in worst or capture[1] < worst[side][1]
            ):
                role, slack_s, endpoint = capture
                worst[side] = (role, slack_s, endpoint, port)
    rows = []
    for side in ("max", "min"):
        if side in worst:
            role, slack_s, endpoint, startpoint = worst[side]
            rows.append(
                f"IPATH\t{instance}\tinto\t{side}\t{role}\t{slack_s!r}"
                f"\t{startpoint}\t{endpoint}"
            )
    return rows


def _append_instance_block(monkeypatch, block: str) -> None:
    """Make every stubbed engine run also print ``block`` on stdout."""
    inner = post_route_sta.subprocess.run

    def run(cmd, **kwargs):
        completed = inner(cmd, **kwargs)
        if completed.stdout and post_route_sta._WORST_PATHS_BEGIN in completed.stdout:
            completed.stdout += block
        return completed

    monkeypatch.setattr(post_route_sta.subprocess, "run", run)


# -- validation ------------------------------------------------------------- #


def test_validate_report_instances_omitted_and_empty_are_none():
    assert _validate_report_instances({}) is None
    assert _validate_report_instances({"report_instances": []}) is None


def test_validate_report_instances_keeps_request_order():
    assert _validate_report_instances(
        {"report_instances": ["ff_b", "u_macro/u_core", "ff_a"]}
    ) == ["ff_b", "u_macro/u_core", "ff_a"]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("ff_a", "must be a list of instance names"),
        ({"ff_a": 1}, "must be a list of instance names"),
        ([1], "non-empty instance-name strings"),
        ([None], "non-empty instance-name strings"),
        ([""], "non-empty instance-name strings"),
        ([" ff_a"], "non-empty instance-name strings"),
        (["ff\na"], "control character"),
        (["ff_a", "ff_b", "ff_a"], r"must not repeat an instance \('ff_a'\)"),
    ],
)
def test_validate_report_instances_rejects(value, message):
    with pytest.raises(PostRouteStaError, match=message):
        _validate_report_instances({"report_instances": value})


def test_run_sta_report_instances_validation_runs_before_engine(tmp_path, monkeypatch):
    request_path = _setup_verilog_success_env(
        tmp_path,
        monkeypatch,
        constraints=_boundary_constraints(report_instances=["ff_a", "ff_a"]),
    )

    def fail_run(*args, **kwargs):
        raise AssertionError("engine must not run for an invalid request")

    monkeypatch.setattr(post_route_sta.subprocess, "run", fail_run)
    with pytest.raises(PostRouteStaError, match="must not repeat an instance"):
        run_sta(request_path)


# -- Tcl emission ------------------------------------------------------------ #


def test_omitting_report_instances_leaves_tcl_unchanged():
    assert _instance_path_lines(None) == []
    assert _instance_path_lines([]) == []
    assert _instance_check_lines(None) == []
    assert _instance_check_lines([]) == []
    maps = ({"a": {"max_ns": 1}}, {})
    assert post_route_sta._port_check_lines(*maps) == (
        post_route_sta._port_check_lines(*maps, None)
    )
    script = "\n".join(
        post_route_sta._sta_netlist_script_lines(
            tech_lef="t.lef",
            cell_lef="c.lef",
            verilog_path="top.v",
            liberty_path="l.lib",
            hdl_toplevel="top",
            clock_port="clk",
            clock_period_ns=2.0,
        )
    )
    assert _INSTANCE_PATHS_BEGIN not in script
    assert "klt_instances_by_name" not in script


def test_instance_path_lines_queries_into_and_out_of_with_bounded_counts():
    lines = _instance_path_lines(["ff_a", "u_core"])
    text = "\n".join(lines)
    assert lines[0] == f'puts "{_INSTANCE_PATHS_BEGIN}"'
    assert lines[-1] == f'puts "{_INSTANCE_PATHS_END}"'
    assert "[list ff_a u_core]" in text
    # One `-to` and one `-from` query per emitted body, each covering both
    # check sides via `foreach klt_ri_side {max min}`; the per-instance rows
    # multiply at the Tcl level, so the script is fixed-size in the entry
    # count while the output stays bounded by entries x 4 records.
    assert text.count("-group_path_count 1 -endpoint_path_count 1") == 2
    assert "find_timing_paths -to $klt_ri_in_pins" in text
    assert "find_timing_paths -from $klt_ri_out_pins" in text
    assert "-filter {direction == input}" in text
    assert "-filter {direction == output}" in text
    # One catch wrapping the whole per-instance body.
    assert text.count("if {[catch {") == 1


def test_instance_names_are_resolved_through_the_dict_never_a_reparse():
    text = "\n".join(_instance_path_lines(["we$ird[0]"]))
    assert r"[list we\$ird\[0\]]" in text
    assert "dict get $klt_instances_by_name $klt_ri_name" in text


def test_report_instances_alone_still_emits_the_name_check_block():
    lines = post_route_sta._request_io_delay_lines(
        "clk", 0.0, 0.1, None, None, report_instances=["ff_a"]
    )
    text = "\n".join(lines)
    assert "set klt_instances_by_name [dict create]" in text
    assert "[list report_instance $klt_ri_name not_found]" in text


# -- parser and records ------------------------------------------------------ #


def test_parse_instance_paths_matches_direct_engine_values():
    """``ff_b`` is a plain ``dfxtp`` with one constrained input data pin
    (``D``), so the worst path into it *is* the ``b_in`` direct query's
    path, both sides."""
    stdout = _ipath_block(*_direct_instance_rows("ff_b", ["b_in"]))
    result = _instance_paths(stdout, ["ff_b"])
    into = result["ff_b"]["into"]
    direct = _WORST_PATH_CAPTURES["direct_queries"]["tt_025C_1v80"]["from"]["b_in"]
    for side, key in (("setup", "max"), ("hold", "min")):
        role, slack_s, endpoint = direct[key]
        record = into[side]
        assert record["status"] == "ok"
        assert record["engine_check_role"] == role
        assert record["startpoint"] == "b_in"
        assert record["endpoint"] == "ff_b/D"
        assert record["slack_ns"] == round(slack_s * 1e9, 5)
        assert record["consistent_with_aggregate"] is None


def test_parse_instance_paths_into_spans_every_input_pin():
    """``into`` covers every input pin, so for ``ff_a`` (``dfrtp``: ``D``
    and ``RESET_B``) the min side is the ``rst_n`` removal check -- worse
    than the ``a_in`` data hold path -- and the max side stays the ``a_in``
    setup path (worse than ``rst_n`` recovery). The ``check_type`` keeps
    ``removal``; it is never relabeled as data hold."""
    stdout = _ipath_block(*_direct_instance_rows("ff_a", ["a_in", "rst_n"]))
    direct = _WORST_PATH_CAPTURES["direct_queries"]["tt_025C_1v80"]["from"]
    into = _instance_paths(stdout, ["ff_a"])["ff_a"]["into"]
    assert into["setup"]["engine_check_role"] == "setup"
    assert (into["setup"]["startpoint"], into["setup"]["endpoint"]) == (
        "a_in",
        "ff_a/D",
    )
    assert into["hold"]["engine_check_role"] == "removal"
    assert (into["hold"]["startpoint"], into["hold"]["endpoint"]) == (
        "rst_n",
        "ff_a/RESET_B",
    )
    assert into["hold"]["check_type"] == "removal"
    assert into["hold"]["slack_ns"] == round(direct["rst_n"]["min"][1] * 1e9, 5)


def test_parse_instance_paths_side_without_constrained_path_is_no_paths():
    stdout = _ipath_block(*_direct_instance_rows("ff_a", ["a_in", "rst_n"]))
    out_of = _instance_paths(stdout, ["ff_a"])["ff_a"]["out_of"]
    for side in ("setup", "hold"):
        assert out_of[side] == post_route_sta._empty_path_record("no_paths", None)
        assert out_of[side]["startpoint"] is None


def test_parse_instance_paths_engine_error_is_unavailable_for_that_instance_only():
    stdout = _ipath_block(
        "warning: something unrelated",
        *_direct_instance_rows("ff_a", ["a_in", "rst_n"]),
        "ERROR\tff_b\tboom",
    )

    result = _instance_paths(stdout, ["ff_a", "ff_b", "ff_bus0"])

    assert result["ff_a"]["into"]["setup"]["status"] == "ok"
    for group in ("into", "out_of"):
        for side in ("setup", "hold"):
            record = result["ff_b"][group][side]
            assert record["status"] == "unavailable"
            assert "boom" in record["reason"]
            assert record["startpoint"] is None
            assert result["ff_bus0"][group][side]["status"] == "no_paths"


def test_parse_instance_paths_out_of_side_reports_launch_path():
    stdout = _ipath_block(
        "IPATH\tff_a\tout_of\tmax\tsetup\t1.5e-09\tff_a/Q\tq_a",
        "IPATH\tff_a\tout_of\tmin\thold\t2e-10\tff_a/Q\tq_a",
    )

    out_of = _instance_paths(stdout, ["ff_a"])["ff_a"]["out_of"]

    assert out_of["setup"]["check_type"] == "setup"
    assert out_of["setup"]["startpoint"] == "ff_a/Q"
    assert out_of["setup"]["endpoint"] == "q_a"
    assert out_of["hold"]["status"] == "ok"


@pytest.mark.parametrize(
    "row",
    [
        "IPATH\tff_a\tinto\tmax\tsetup\tnotanumber\ta_in\tff_a/D",
        "IPATH\tff_a\tinto\tmax\tsetup\tinf\ta_in\tff_a/D",
        "IPATH\tff_a\tinto\tbogus\tsetup\t1e-9\ta_in\tff_a/D",
        "IPATH\tff_a\tinto\tmax\tsetup\t1e-9\ta_in",
        "IPATH\tff_a\tsideways\tmax\tsetup\t1e-9\ta_in\tff_a/D",
    ],
)
def test_parse_instance_paths_malformed_row_is_unavailable(row):
    stdout = _ipath_block(row)
    entry = _instance_paths(stdout, ["ff_a"])["ff_a"]
    assert entry["into"]["setup"]["status"] == "unavailable"
    assert entry["into"]["hold"]["status"] == "unavailable"


def test_parse_instance_paths_absent_block_is_unavailable_for_every_instance():
    result = _instance_paths("no markers here\n", ["ff_a", "ff_b"])
    assert all(
        record["status"] == "unavailable"
        for entry in result.values()
        for group in entry.values()
        for record in group.values()
    )


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

    monkeypatch.setattr(post_route_sta.subprocess, "run", fake_run)

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
