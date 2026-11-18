"""Per-stage delay breakdown for `klt sta`'s reported worst paths (issue
#5783): the emitter, parser and attach step live in
`klayout_tools/post_route_sta_stages.py`.

Split out of ``test_post_route_sta.py`` to keep that file under its size
ceiling (the same pattern as ``test_post_route_sta_path_groups.py``); the
boundary capture fixture and the request/stub helpers are reused from there.
The stage-region fixtures are real engine output (OpenROAD 26Q3-1510,
2026-11-17, sky130_fd_sc_hd tt_025C_1v80), abridged to the rows the parser
reads: headers, capture-side clock rows, closing-summary repeats removed.
"""

from __future__ import annotations

import json

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import post_route_sta
from klayout_tools.post_route_sta import run_sta
from klayout_tools.post_route_sta_stages import (
    _PATH_STAGES_BEGIN,
    _PATH_STAGES_END,
    _PATH_STAGES_ROW,
    _attach_worst_path_stages,
    _worst_path_stage_lines,
)
from test_post_route_sta import (
    _WORST_PATH_CAPTURES,
    _boundary_constraints,
    _path_block,
    _setup_multi_corner_env,
    _setup_verilog_success_env,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

# -- fixtures (real captured engine output, parser-inert rows trimmed) ------- #

_BOUNDARY_MAX_REGION = """Path Group: clk
Path Type: max

      Delay        Time   Description
   0.000000    0.000000   clock clk (rise edge)
   0.000000    0.000000   clock network delay (ideal)
   1.200000    1.200000 v input external delay
   0.000000    1.200000 v b_in (in)
   0.000000    1.200000 v ff_b/D (sky130_fd_sc_hd__dfxtp_1)
               1.200000   data arrival time
  -0.101249    1.898751   library setup time
               1.898751   data required time
"""

_BOUNDARY_MIN_REGION = """Path Group: asynchronous
Path Type: min

      Delay        Time   Description
   0.000000    0.000000   clock clk (rise edge)
   0.000000    0.000000   clock network delay (ideal)
   0.000000    0.000000 ^ input external delay
   0.000000    0.000000 ^ rst_n (in)
   0.000000    0.000000 ^ ff_a/RESET_B (sky130_fd_sc_hd__dfrtp_1)
               0.000000   data arrival time
   0.295591    0.295591   library removal time
               0.295591   data required time
"""

_REG2REG_MAX_REGION = """Path Group: clk
Path Type: max

      Delay        Time   Description
   0.000000    0.000000   clock clk (rise edge)
   0.000000    0.000000   clock network delay (ideal)
   0.000000    0.000000 ^ ff_in/CLK (sky130_fd_sc_hd__dfxtp_1)
   0.268646    0.268646 v ff_in/Q (sky130_fd_sc_hd__dfxtp_1)
   0.099703    0.368349 v b1/X (sky130_fd_sc_hd__buf_2)
   0.099311    0.467660 v b2/X (sky130_fd_sc_hd__buf_2)
   0.000000    0.467660 v ff_out/D (sky130_fd_sc_hd__dfxtp_1)
               0.467660   data arrival time
  -0.110604    0.689396   library setup time
               0.689396   data required time
"""

_REG2REG_MIN_REGION = """Path Group: clk
Path Type: min

      Delay        Time   Description
   0.000000    0.000000   clock clk (rise edge)
   0.000000    0.000000   clock network delay (ideal)
   0.000000    0.000000 ^ input external delay
   0.000000    0.000000 ^ d_in (in)
   0.000000    0.000000 ^ ff_in/D (sky130_fd_sc_hd__dfxtp_1)
               0.000000   data arrival time
  -0.027717   -0.027717   library hold time
              -0.027717   data required time
"""


def _region(side: str, text: str) -> str:
    return "\n".join(
        [
            f"{_PATH_STAGES_ROW}\t{side}\tBEGIN",
            text,
            f"{_PATH_STAGES_ROW}\t{side}\tEND",
        ]
    )


def _stage_stdout(*regions: str) -> str:
    return "\n".join([_PATH_STAGES_BEGIN, *regions, _PATH_STAGES_END])


_BOUNDARY_STDOUT = "\n".join(
    [
        _path_block(
            "PATH\tmax\trecovery\t2.228268458637217e-9\trst_n\tff_a/RESET_B",
            "PATH\tmax\tsetup\t6.987513900824638e-10\tb_in\tff_b/D",
            "PATH\tmin\tremoval\t-2.9559105163556865e-10\trst_n\tff_a/RESET_B",
            "PATH\tmin\thold\t-1.722826326044924e-10\tbus[1]\tff_bus1/D",
        ),
        _stage_stdout(
            _region("max", _BOUNDARY_MAX_REGION),
            _region("min", _BOUNDARY_MIN_REGION),
        ),
    ]
)

_REG2REG_STDOUT = "\n".join(
    [
        _path_block(
            "PATH\tmax\tsetup\t2.2173529679037074e-10\tff_in/Q\tff_out/D",
            "PATH\tmin\thold\t2.771735792272967e-11\td_in\tff_in/D",
        ),
        _stage_stdout(
            _region("max", _REG2REG_MAX_REGION),
            _region("min", _REG2REG_MIN_REGION),
        ),
    ]
)


def _stub_openroad_stage_stdout(monkeypatch, stdout: str) -> None:
    """Replay `stdout` (worst-path block + stage block) for every corner of
    one `run_sta` request; metrics are the real boundary capture's."""
    metrics = _WORST_PATH_CAPTURES["scenarios"]["boundary_tt_025C_1v80"]["metrics"]

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return fake_completed(stdout="26Q3-1510-g6cb3f2b704 \n")
        with open(cmd[4], "w", encoding="utf-8") as handle:
            json.dump(metrics, handle)
        return fake_completed(returncode=0, stdout=stdout)

    monkeypatch.setattr(post_route_sta.subprocess, "run", fake_run)


def _ok_record(side: str, **overrides) -> dict:
    is_max = side == "max"
    record = {
        "status": "ok",
        "engine_check_role": "setup" if is_max else "removal",
        "startpoint": "b_in" if is_max else "rst_n",
        "endpoint": "ff_b/D" if is_max else "ff_a/RESET_B",
        "slack_ns": 0.69875 if is_max else -0.29559,
        "consistent_with_aggregate": True,
        "reason": None,
    }
    record.update(overrides)
    return record


# -- parser: ok cases on real captures --------------------------------------- #


def test_boundary_setup_and_hold_stages_ok():
    setup, hold = _attach_worst_path_stages(
        _BOUNDARY_STDOUT, _ok_record("max"), _ok_record("min")
    )
    assert setup["stages"] == {
        "status": "ok",
        "reason": None,
        "stages": [
            {"pin": "b_in", "cell": "in", "incr_ns": 0.0, "path_ns": 1.2},
            {
                "pin": "ff_b/D",
                "cell": "sky130_fd_sc_hd__dfxtp_1",
                "incr_ns": 0.0,
                "path_ns": 1.2,
            },
        ],
    }
    assert hold["stages"] == {
        "status": "ok",
        "reason": None,
        "stages": [
            {"pin": "rst_n", "cell": "in", "incr_ns": 0.0, "path_ns": 0.0},
            {
                "pin": "ff_a/RESET_B",
                "cell": "sky130_fd_sc_hd__dfrtp_1",
                "incr_ns": 0.0,
                "path_ns": 0.0,
            },
        ],
    }


def test_reg2reg_stages_ok_with_real_increments():
    """A flop-to-flop path's stage table; hold required time is negative."""
    setup = _ok_record(
        "max", startpoint="ff_in/Q", endpoint="ff_out/D", slack_ns=0.22174
    )
    hold = _ok_record("min", startpoint="d_in", endpoint="ff_in/D", slack_ns=0.02772)
    setup, hold = _attach_worst_path_stages(_REG2REG_STDOUT, setup, hold)
    assert setup["stages"]["stages"] == [
        {
            "pin": "ff_in/Q",
            "cell": "sky130_fd_sc_hd__dfxtp_1",
            "incr_ns": 0.268646,
            "path_ns": 0.268646,
        },
        {
            "pin": "b1/X",
            "cell": "sky130_fd_sc_hd__buf_2",
            "incr_ns": 0.099703,
            "path_ns": 0.368349,
        },
        {
            "pin": "b2/X",
            "cell": "sky130_fd_sc_hd__buf_2",
            "incr_ns": 0.099311,
            "path_ns": 0.46766,
        },
        {
            "pin": "ff_out/D",
            "cell": "sky130_fd_sc_hd__dfxtp_1",
            "incr_ns": 0.0,
            "path_ns": 0.46766,
        },
    ]
    assert hold["stages"]["stages"] == [
        {"pin": "d_in", "cell": "in", "incr_ns": 0.0, "path_ns": 0.0},
        {
            "pin": "ff_in/D",
            "cell": "sky130_fd_sc_hd__dfxtp_1",
            "incr_ns": 0.0,
            "path_ns": 0.0,
        },
    ]


# -- parser: never fabricated ------------------------------------------------- #


def _assert_unavailable(block: dict) -> None:
    assert block["status"] == "unavailable"
    assert block["stages"] is None
    assert isinstance(block["reason"], str) and block["reason"]


def test_stage_block_absent_is_unavailable():
    stdout = _path_block("PATH\tmax\tsetup\t-1e-9\ta\tb")
    setup, hold = _attach_worst_path_stages(
        stdout, _ok_record("max"), _ok_record("min")
    )
    assert "absent from engine output" in setup["stages"]["reason"]
    assert hold["stages"]["reason"] == setup["stages"]["reason"]
    _assert_unavailable(setup["stages"])


def test_stage_engine_error_is_unavailable_both_sides():
    stdout = _stage_stdout('ERROR\tinvalid command name "report_checks"')
    setup, hold = _attach_worst_path_stages(
        stdout, _ok_record("max"), _ok_record("min")
    )
    assert "engine path-stage introspection failed" in setup["stages"]["reason"]
    assert "report_checks" in hold["stages"]["reason"]
    _assert_unavailable(setup["stages"])


def test_missing_side_region_is_unavailable_on_that_side():
    stdout = _stage_stdout(_region("max", _BOUNDARY_MAX_REGION))
    setup, hold = _attach_worst_path_stages(
        stdout, _ok_record("max"), _ok_record("min")
    )
    assert setup["stages"]["status"] == "ok"
    _assert_unavailable(hold["stages"])
    assert hold["stages"]["reason"] == "no path-stage report for the min side"


@pytest.mark.parametrize(
    "mutate",
    [
        {"status": "no_paths", "startpoint": None, "endpoint": None, "slack_ns": None},
        {
            "status": "inconsistent",
            "reason": "worst path slack 1.0 ns differs from the aggregate WNS 2.0",
        },
        {"status": "unavailable", "reason": "worst-path records absent"},
    ],
)
def test_non_ok_record_carries_stated_reason_never_a_table(mutate):
    stdout = _stage_stdout(_region("max", _BOUNDARY_MAX_REGION))
    record = _ok_record("max")
    record.update(mutate)
    setup, _ = _attach_worst_path_stages(stdout, record, _ok_record("min"))
    _assert_unavailable(setup["stages"])
    assert setup["stages"]["reason"].startswith(
        f"worst path status is {mutate['status']}"
    )


def test_startpoint_row_missing_is_unavailable():
    stdout = _stage_stdout(_region("max", _BOUNDARY_MAX_REGION))
    setup, _ = _attach_worst_path_stages(
        stdout, _ok_record("max", startpoint="zzz"), _ok_record("min")
    )
    _assert_unavailable(setup["stages"])
    assert "no 'zzz' startpoint row" in setup["stages"]["reason"]


def test_endpoint_row_missing_is_unavailable():
    stdout = _stage_stdout(_region("max", _BOUNDARY_MAX_REGION))
    setup, _ = _attach_worst_path_stages(
        stdout, _ok_record("max", endpoint="zzz/D"), _ok_record("min")
    )
    _assert_unavailable(setup["stages"])
    assert "no 'zzz/D' endpoint row" in setup["stages"]["reason"]


def test_broken_increment_chain_is_unavailable():
    stdout = _stage_stdout(
        _region(
            "max", _REG2REG_MAX_REGION.replace("0.467660 v b2/X", "0.599660 v b2/X")
        )
    )
    setup, _ = _attach_worst_path_stages(
        stdout,
        _ok_record("max", startpoint="ff_in/Q", endpoint="ff_out/D", slack_ns=0.22174),
        _ok_record("min"),
    )
    _assert_unavailable(setup["stages"])
    assert "adds 0.099311 ns to" in setup["stages"]["reason"]


def test_setup_side_slack_disagreement_is_unavailable():
    stdout = _stage_stdout(_region("max", _BOUNDARY_MAX_REGION))
    setup, _ = _attach_worst_path_stages(
        stdout, _ok_record("max", slack_ns=0.9), _ok_record("min")
    )
    _assert_unavailable(setup["stages"])
    assert "disagrees with the worst path slack 0.9 ns" in setup["stages"]["reason"]


def test_hold_side_uses_the_hold_sign_convention():
    """The hold-side gate rejects the setup-side sign and accepts the real
    one -- the conventions are not interchangeable."""
    stdout = _stage_stdout(
        _region("max", _BOUNDARY_MAX_REGION), _region("min", _BOUNDARY_MIN_REGION)
    )
    _, wrong_sign = _attach_worst_path_stages(
        stdout, _ok_record("max"), _ok_record("min", slack_ns=0.29559)
    )
    _assert_unavailable(wrong_sign["stages"])
    _, right = _attach_worst_path_stages(
        stdout, _ok_record("max"), _ok_record("min", slack_ns=-0.29559)
    )
    assert right["stages"]["status"] == "ok"


# -- request-field validation -------------------------------------------------- #


def test_report_path_stages_validation():
    assert post_route_sta._validate_report_path_stages({}) is False
    field = {"report_path_stages": None}
    assert post_route_sta._validate_report_path_stages(field) is False
    field["report_path_stages"] = True
    assert post_route_sta._validate_report_path_stages(field) is True
    field["report_path_stages"] = False
    assert post_route_sta._validate_report_path_stages(field) is False
    for bad in ("yes", 1, [], {"a": 1}):
        with pytest.raises(post_route_sta.PostRouteStaError, match="must be a boolean"):
            post_route_sta._validate_report_path_stages({"report_path_stages": bad})


# -- Tcl generation ------------------------------------------------------------- #


def test_flag_off_leaves_both_scripts_byte_identical():
    cases = (
        (
            post_route_sta._sta_script_lines,
            dict(
                tech_lef="t",
                cell_lef="c",
                def_path="d",
                liberty_path="l",
                clock_port="clk",
                clock_period_ns=2.0,
            ),
        ),
        (
            post_route_sta._sta_netlist_script_lines,
            dict(
                tech_lef="t",
                cell_lef="c",
                verilog_path="v",
                liberty_path="l",
                hdl_toplevel="top",
                clock_port="clk",
                clock_period_ns=2.0,
            ),
        ),
    )
    for build, kwargs in cases:
        base = build(**kwargs)
        assert base == build(**kwargs, report_path_stages=False)
        assert not any("PATH_STAGES" in ln for ln in base)
        idx = base.index(f'puts "{post_route_sta._WORST_PATHS_END}"') + 1
        on = build(**kwargs, report_path_stages=True)
        assert on == base[:idx] + _worst_path_stage_lines(True) + base[idx:]


def test_stage_lines_flag_off_empty_flag_on_pinned_query():
    assert _worst_path_stage_lines(False) == []
    lines = _worst_path_stage_lines(True)
    assert lines[0] == f'puts "{_PATH_STAGES_BEGIN}"'
    assert lines[-1] == f'puts "{_PATH_STAGES_END}"'
    script = "\n".join(lines)
    assert (
        "report_checks -to $klt_stage_end -path_delay $klt_stage_side "
        "-group_path_count 1 -endpoint_path_count 1 -format full -digits 6"
    ) in script
    assert "if {[llength $klt_stage_pes] > 0} {" in script
    assert "} klt_stage_error]} {" in script


def test_moved_worst_path_lines_stays_reachable_and_unchanged():
    """`post_route_sta._worst_path_lines` (the name the golden at
    test_post_route_sta.py:3337 pins) is the moved function, byte-equal."""
    from klayout_tools.post_route_sta_stages import _worst_path_lines as moved

    assert post_route_sta._worst_path_lines is moved
    assert _WORST_PATH_CAPTURES["worst_path_tcl"] == post_route_sta._worst_path_lines()


# -- end-to-end through run_sta (stubbed engine) --------------------------------- #


def test_run_sta_verilog_flag_on_attaches_stages(tmp_path, monkeypatch):
    request_path = _setup_verilog_success_env(
        tmp_path,
        monkeypatch,
        constraints=_boundary_constraints(report_path_stages=True),
    )
    _stub_openroad_stage_stdout(monkeypatch, _BOUNDARY_STDOUT)
    report = run_sta(request_path)
    assert report["worst_setup_path"]["stages"]["status"] == "ok"
    assert report["worst_setup_path"]["stages"]["stages"][0]["pin"] == "b_in"
    assert report["worst_hold_path"]["stages"]["status"] == "ok"
    assert report["worst_hold_path"]["stages"]["stages"][0]["pin"] == "rst_n"
    # The worst-path records themselves keep their pre-#5783 shape.
    assert report["worst_setup_path"]["endpoint"] == "ff_b/D"
    assert report["worst_hold_path"]["engine_check_role"] == "removal"


def test_run_sta_flag_off_records_carry_no_stages_key(tmp_path, monkeypatch):
    request_path = _setup_verilog_success_env(
        tmp_path, monkeypatch, constraints=_boundary_constraints()
    )
    _stub_openroad_stage_stdout(monkeypatch, _BOUNDARY_STDOUT)
    report = run_sta(request_path)
    assert "stages" not in report["worst_setup_path"]
    assert "stages" not in report["worst_hold_path"]


def test_run_sta_multi_corner_stages_per_corner(tmp_path, monkeypatch):
    request_path = _setup_multi_corner_env(
        tmp_path,
        monkeypatch,
        constraints=_boundary_constraints(report_path_stages=True),
    )
    _stub_openroad_stage_stdout(monkeypatch, _BOUNDARY_STDOUT)
    report = run_sta(request_path)
    assert len(report["corners"]) == 2
    for corner in report["corners"]:
        assert corner["worst_setup_path"]["stages"]["status"] == "ok"
        assert corner["worst_hold_path"]["stages"]["stages"][0]["pin"] == "rst_n"
