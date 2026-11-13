"""Per-path-group worst paths for `klt sta`: `worst_paths_by_group` (issue
#5424). The parsers live in `klayout_tools/post_route_sta_path_groups.py`.

Split out of ``test_post_route_sta.py`` to keep that file under its size
ceiling; the boundary capture fixture and the OpenROAD stub it replays
through are reused from there.
"""

from __future__ import annotations

import pytest

from klayout_tools import post_route_sta
from klayout_tools.post_route_sta import run_sta
from test_post_route_sta import (
    _WORST_PATH_CAPTURES,
    _boundary_constraints,
    _path_block,
    _setup_multi_corner_env,
    _setup_success_env,
    _setup_verilog_success_env,
    _stub_openroad_capture,
    _stub_openroad_success,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _recovery_worst_stdout() -> str:
    """The real `boundary_tt_025C_1v80` capture with the async-recovery
    row's slack edited to be worse than the data-setup row's -- the issue's
    own symptom (a recovery violation owning the setup-side headline) on
    otherwise captured engine output."""
    stdout = _WORST_PATH_CAPTURES["scenarios"]["boundary_tt_025C_1v80"]["stdout"]
    return stdout.replace(
        "PATH\tmax\trecovery\t2.228268458637217e-9\trst_n\tff_a/RESET_B",
        "PATH\tmax\trecovery\t-4.1e-10\trst_n\tff_a/RESET_B",
    )


def test_worst_paths_by_group_keeps_every_parsed_row(tmp_path, monkeypatch):
    """Both setup-side rows the engine reported survive into the response,
    worst first -- the data-setup slack is no longer dropped."""
    request_path = _setup_verilog_success_env(
        tmp_path, monkeypatch, constraints=_boundary_constraints()
    )
    _stub_openroad_capture(monkeypatch, {None: "boundary_tt_025C_1v80"})

    report = run_sta(request_path)

    assert report["worst_paths_by_group"] == {
        "status": "ok",
        "reason": None,
        "setup": [
            {
                "check_type": "setup",
                "engine_check_role": "setup",
                "startpoint": "b_in",
                "endpoint": "ff_b/D",
                "slack_ns": 0.69875,
            },
            {
                "check_type": "recovery",
                "engine_check_role": "recovery",
                "startpoint": "rst_n",
                "endpoint": "ff_a/RESET_B",
                "slack_ns": 2.22827,
            },
        ],
        "hold": [
            {
                "check_type": "removal",
                "engine_check_role": "removal",
                "startpoint": "rst_n",
                "endpoint": "ff_a/RESET_B",
                "slack_ns": -0.29559,
            },
            {
                "check_type": "hold",
                "engine_check_role": "hold",
                "startpoint": "bus[1]",
                "endpoint": "ff_bus1/D",
                "slack_ns": -0.17228,
            },
        ],
    }
    # Existing fields are untouched by the addition.
    assert report["worst_slack_ns"] == 0.69875
    assert report["worst_hold_slack_ns"] == -0.29559
    assert report["fmax_mhz"] == 768.493
    assert report["setup_violation_count"] == 0
    assert report["hold_violation_count"] == 2


def test_worst_paths_by_group_first_entry_is_the_worst_path_record(
    tmp_path, monkeypatch
):
    """Each side's head entry names the same path `worst_*_path` reports."""
    request_path = _setup_verilog_success_env(
        tmp_path, monkeypatch, constraints=_boundary_constraints()
    )
    _stub_openroad_capture(monkeypatch, {None: "boundary_tt_025C_1v80"})

    report = run_sta(request_path)

    by_group = report["worst_paths_by_group"]
    for side, record_key in (
        ("setup", "worst_setup_path"),
        ("hold", "worst_hold_path"),
    ):
        record = report[record_key]
        head = by_group[side][0]
        assert (head["startpoint"], head["endpoint"], head["slack_ns"]) == (
            record["startpoint"],
            record["endpoint"],
            record["slack_ns"],
        )
        assert head["check_type"] == record["check_type"]
        assert head["engine_check_role"] == record["engine_check_role"]


def test_worst_paths_by_group_exposes_data_setup_behind_a_recovery_violation():
    """The issue's symptom: an async-recovery check owns the setup-side
    aggregate, and the data-setup slack is readable from the response."""
    stdout = _recovery_worst_stdout()
    setup_record, _ = post_route_sta._worst_paths(stdout, -0.41, -0.29559)
    by_group = post_route_sta._worst_paths_by_group(stdout)

    # The headline is the reset network, exactly as reported today.
    assert setup_record["check_type"] == "recovery"
    assert setup_record["slack_ns"] == -0.41
    # ... and the datapath's own worst setup slack is now in the response.
    assert [entry["check_type"] for entry in by_group["setup"]] == [
        "recovery",
        "setup",
    ]
    assert by_group["setup"][0]["slack_ns"] == -0.41
    assert by_group["setup"][1] == {
        "check_type": "setup",
        "engine_check_role": "setup",
        "startpoint": "b_in",
        "endpoint": "ff_b/D",
        "slack_ns": 0.69875,
    }


@pytest.mark.parametrize(
    ("block", "reason"),
    [
        (None, "absent from engine output"),
        (['ERROR\tinvalid command name "find_timing_paths"'], "introspection failed"),
        (["PATH\tmax\tsetup\t1e-10\tonly_start"], "malformed worst-path record"),
        (["PATH\tmax\tsetup\tfast\ta\tb"], "malformed worst-path slack"),
        (["PATH\tsideways\tsetup\t1e-10\ta\tb"], "malformed worst-path record"),
        (["PATH\tmax\tsetup\tinf\ta\tb"], "non-finite"),
    ],
)
def test_worst_paths_by_group_unavailable_never_fabricated(block, reason):
    """A failed parse yields empty lists and the same reason the single
    records report -- never a partial list built from the good rows."""
    if block is None:
        stdout = "no markers here"
    else:
        stdout = _path_block(*block)
    by_group = post_route_sta._worst_paths_by_group(stdout)
    record, _ = post_route_sta._worst_paths(stdout, -0.1, 0.2)

    assert by_group["status"] == "unavailable"
    assert reason in by_group["reason"]
    assert by_group["reason"] == record["reason"]
    assert by_group["setup"] == []
    assert by_group["hold"] == []


def test_worst_paths_by_group_empty_side_is_ok():
    """A side with no constrained path is an empty list, not unavailable."""
    by_group = post_route_sta._worst_paths_by_group(
        _path_block("PATH\tmax\tsetup\t1e-10\ta\tff/D")
    )
    assert by_group["status"] == "ok"
    assert by_group["reason"] is None
    assert by_group["hold"] == []
    assert len(by_group["setup"]) == 1


def test_worst_paths_by_group_unmapped_role_is_unknown():
    by_group = post_route_sta._worst_paths_by_group(
        _path_block("PATH\tmin\tdata check hold\t-1e-10\ta\tff/D")
    )
    assert by_group["hold"][0]["check_type"] == "unknown"
    assert by_group["hold"][0]["engine_check_role"] == "data check hold"


def test_worst_paths_by_group_ties_keep_engine_order():
    by_group = post_route_sta._worst_paths_by_group(
        _path_block(
            "PATH\tmax\trecovery\t-1e-10\trst\tff_a/RESET_B",
            "PATH\tmax\tsetup\t-1e-10\ta\tff_b/D",
        )
    )
    assert [entry["engine_check_role"] for entry in by_group["setup"]] == [
        "recovery",
        "setup",
    ]


def test_run_sta_unconstrained_reports_empty_groups(tmp_path, monkeypatch):
    request_path = _setup_verilog_success_env(tmp_path, monkeypatch)
    _stub_openroad_capture(monkeypatch, {None: "unconstrained_tt_025C_1v80"})

    report = run_sta(request_path)

    assert report["worst_paths_by_group"] == {
        "status": "ok",
        "reason": None,
        "setup": [],
        "hold": [],
    }


def test_run_sta_without_worst_path_block_reports_groups_unavailable(
    tmp_path, monkeypatch
):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)

    report = run_sta(request_path)

    assert report["worst_paths_by_group"]["status"] == "unavailable"
    assert report["worst_paths_by_group"]["setup"] == []
    assert report["worst_paths_by_group"]["hold"] == []
    assert (
        report["worst_paths_by_group"]["reason"] == report["worst_setup_path"]["reason"]
    )


def test_run_sta_corner_sweep_carries_worst_paths_by_group_per_corner(
    tmp_path, monkeypatch
):
    request_path = _setup_multi_corner_env(
        tmp_path, monkeypatch, constraints=_boundary_constraints()
    )
    _stub_openroad_capture(
        monkeypatch,
        {
            "tt_025C_1v80": "boundary_tt_025C_1v80",
            "ss_100C_1v60": "boundary_ss_100C_1v60",
        },
    )

    report = run_sta(request_path)

    assert "worst_paths_by_group" not in report
    tt_entry, ss_entry = report["corners"]
    for entry in (tt_entry, ss_entry):
        by_group = entry["worst_paths_by_group"]
        assert by_group["status"] == "ok"
        assert [e["check_type"] for e in by_group["setup"]] == ["setup", "recovery"]
        assert [e["check_type"] for e in by_group["hold"]] == ["removal", "hold"]
    # Per-corner slacks come from each corner's own session.
    assert tt_entry["worst_paths_by_group"]["setup"][0]["slack_ns"] == 0.69875
    assert ss_entry["worst_paths_by_group"]["setup"][0]["slack_ns"] == 0.54129
    assert tt_entry["worst_paths_by_group"]["hold"][0]["slack_ns"] == -0.29559
    assert ss_entry["worst_paths_by_group"]["hold"][0]["slack_ns"] == -0.6148
