"""``constraints.timing_derate`` for ``klt sta`` (issue #7014).

The ``set_timing_derate -early/-late`` equivalent: request validation, the
generated Tcl in both input modes, and the response echo on single- and
multi-corner runs. Lives in its own module and imports the shared request /
stub helpers from ``tests.test_post_route_sta`` unchanged, so that
size-baselined test file does not grow.
"""

from __future__ import annotations

import pytest

from klayout_tools import _sta_timing_derate, post_route_sta
from klayout_tools.post_route_sta import PostRouteStaError, run_sta
from test_post_route_sta import (
    _propagated_constraints,
    _setup_multi_corner_env,
    _setup_success_env,
    _setup_verilog_success_env,
    _sta_script_text,
    _stub_openroad_clock_capture,
    _stub_openroad_success,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_MARKER = "set_timing_derate"
_SCRIPT_KWARGS = dict(
    tech_lef="/pdk/tech.lef",
    cell_lef="/pdk/cells.lef",
    liberty_path="/pdk/lib.lib",
    clock_port="clk",
    clock_period_ns=2.5,
)
_BOTH = {"early": 0.95, "late": 1.05}


def _derate_lines(script: str) -> list[str]:
    return [x for x in script.splitlines() if x.startswith(_MARKER)]


# -- validation ---------------------------------------------------------------#


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        (_BOTH, {"early": 0.95, "late": 1.05}),
        ({"early": 0.9}, {"early": 0.9, "late": None}),
        ({"late": 1.1}, {"early": None, "late": 1.1}),
        ({"early": 1, "late": 1.0}, {"early": 1.0, "late": 1.0}),
    ],
)
def test_validate_timing_derate_accepts(value, expected):
    constraints = {"timing_derate": value}
    assert _sta_timing_derate.validate_timing_derate(constraints) == expected
    if value is None:
        assert _sta_timing_derate.validate_timing_derate({}) is None


@pytest.mark.parametrize(
    "value, match",
    [
        ({"early": 0}, r"timing_derate\.early must be > 0"),
        ({"late": -1.05}, r"timing_derate\.late must be > 0"),
        ({"early": True}, r"timing_derate\.early must be a finite number"),
        ({"late": "1.05"}, r"timing_derate\.late must be a finite number"),
        ({"early": None}, r"timing_derate\.early must be a finite number"),
        ({"early": 0.95, "late": None}, r"timing_derate\.late must be a finite"),
        ({"late": float("nan")}, r"timing_derate\.late must be a finite number"),
        ({"late": float("inf")}, r"timing_derate\.late must be a finite number"),
        ({"early": 10**400}, r"timing_derate\.early must be a finite number"),
        ({"late": [1.05]}, r"timing_derate\.late must be a finite number"),
        (1.05, r"constraints\.timing_derate must be an object"),
        ("1.05", r"constraints\.timing_derate must be an object"),
        (True, r"constraints\.timing_derate must be an object"),
        ([0.95, 1.05], r"constraints\.timing_derate must be an object"),
        ({}, r"constraints\.timing_derate must give at least one"),
        ({"early": 0.95, "clock": 1.0}, r"unknown key\(s\): clock"),
    ],
)
def test_validate_timing_derate_rejects(value, match):
    with pytest.raises(PostRouteStaError, match=match):
        _sta_timing_derate.validate_timing_derate({"timing_derate": value})


@pytest.mark.parametrize(
    "value", [{"early": 0}, {}, {"late": True}, {"bogus": 1.0}, 1.05]
)
def test_run_sta_timing_derate_validation_runs_before_engine(
    tmp_path, monkeypatch, value
):
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        constraints=_propagated_constraints(timing_derate=value),
    )
    calls: list = []
    monkeypatch.setattr(
        post_route_sta.subprocess, "run", lambda *a, **k: calls.append(a)
    )
    with pytest.raises(PostRouteStaError, match="timing_derate"):
        run_sta(request_path)
    assert calls == []


# -- Tcl / response helpers ---------------------------------------------------#


def test_timing_derate_lines_and_block():
    assert _sta_timing_derate.timing_derate_lines(None) == []
    assert _sta_timing_derate.timing_derate_block(None) == {"applied": False}
    both = {"early": 0.95, "late": 1.05}
    assert _sta_timing_derate.timing_derate_lines(both) == [
        "set_timing_derate -early 0.95",
        "set_timing_derate -late 1.05",
    ]
    assert _sta_timing_derate.timing_derate_block(both) == {
        "applied": True,
        "early": 0.95,
        "late": 1.05,
    }
    late_only = {"early": None, "late": 1.0}
    assert _sta_timing_derate.timing_derate_lines(late_only) == [
        "set_timing_derate -late 1.0"
    ]
    assert _sta_timing_derate.timing_derate_block(late_only) == {
        "applied": True,
        "early": None,
        "late": 1.0,
    }


@pytest.mark.parametrize(
    "derate, expected",
    [
        (_BOTH, ["set_timing_derate -early 0.95", "set_timing_derate -late 1.05"]),
        ({"early": 0.9, "late": None}, ["set_timing_derate -early 0.9"]),
        ({"early": None, "late": 1.1}, ["set_timing_derate -late 1.1"]),
        (
            {"early": 1.0, "late": 1.0},
            [
                "set_timing_derate -early 1.0",
                "set_timing_derate -late 1.0",
            ],
        ),
    ],
)
def test_script_lines_timing_derate_both_modes(derate, expected):
    def_base = post_route_sta._sta_script_lines(def_path="/d/top.def", **_SCRIPT_KWARGS)
    def_lines = post_route_sta._sta_script_lines(
        def_path="/d/top.def", timing_derate=derate, **_SCRIPT_KWARGS
    )
    net_kwargs = dict(verilog_path="/d/top.v", hdl_toplevel="top", **_SCRIPT_KWARGS)
    net_base = post_route_sta._sta_netlist_script_lines(**net_kwargs)
    net_lines = post_route_sta._sta_netlist_script_lines(
        timing_derate=derate, **net_kwargs
    )
    for base, lines in ((def_base, def_lines), (net_base, net_lines)):
        assert not any(_MARKER in x for x in base)
        assert [x for x in lines if _MARKER in x] == expected
        # Only the derate lines are added; nothing else in the script moves.
        assert [x for x in lines if _MARKER not in x] == base
        # Global (no object argument) and before any timing is reported.
        first_report = next(i for i, x in enumerate(lines) if x.startswith("report_"))
        assert all(lines.index(x) < first_report for x in expected)


def test_script_lines_timing_derate_omitted_is_byte_identical():
    assert post_route_sta._sta_script_lines(
        def_path="/d/top.def", timing_derate=None, **_SCRIPT_KWARGS
    ) == post_route_sta._sta_script_lines(def_path="/d/top.def", **_SCRIPT_KWARGS)


# -- run_sta end to end (engine stubbed) --------------------------------------#


@pytest.mark.parametrize("derate_field", ["omitted", "null"])
def test_run_sta_def_timing_derate_disabled(tmp_path, monkeypatch, derate_field):
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    request_path = _setup_success_env(
        base_dir, monkeypatch, constraints=_propagated_constraints()
    )
    baseline = _stub_openroad_clock_capture(monkeypatch, {None: "propagated_buffered"})
    run_sta(request_path)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    extra = {} if derate_field == "omitted" else {"timing_derate": None}
    request_path = _setup_success_env(
        run_dir, monkeypatch, constraints=_propagated_constraints(**extra)
    )
    scripts = _stub_openroad_clock_capture(monkeypatch, {None: "propagated_buffered"})
    report = run_sta(request_path)
    assert report["timing_derate"] == {"applied": False}
    assert _MARKER not in scripts[0]
    assert scripts[0].replace(str(run_dir), "<D>") == baseline[0].replace(
        str(base_dir), "<D>"
    )


def test_run_sta_def_timing_derate_response_and_script(tmp_path, monkeypatch):
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        constraints=_propagated_constraints(timing_derate=_BOTH),
    )
    scripts = _stub_openroad_clock_capture(monkeypatch, {None: "propagated_buffered"})
    report = run_sta(request_path)
    assert report["timing_derate"] == {"applied": True, "early": 0.95, "late": 1.05}
    assert _derate_lines(scripts[0]) == [
        "set_timing_derate -early 0.95",
        "set_timing_derate -late 1.05",
    ]


def test_run_sta_def_timing_derate_one_side(tmp_path, monkeypatch):
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        constraints=_propagated_constraints(timing_derate={"late": 1.05}),
    )
    scripts = _stub_openroad_clock_capture(monkeypatch, {None: "propagated_buffered"})
    report = run_sta(request_path)
    assert report["timing_derate"] == {"applied": True, "early": None, "late": 1.05}
    assert _derate_lines(scripts[0]) == ["set_timing_derate -late 1.05"]


def test_run_sta_verilog_timing_derate(tmp_path, monkeypatch):
    request_path = _setup_verilog_success_env(
        tmp_path,
        monkeypatch,
        constraints={
            "clock_port": "clk",
            "clock_period_ns": 1.1,
            "timing_derate": {"early": 0.95},
        },
    )
    _stub_openroad_success(monkeypatch)
    report = run_sta(request_path)
    assert report["timing_derate"] == {"applied": True, "early": 0.95, "late": None}
    assert _derate_lines(_sta_script_text(tmp_path)) == [
        "set_timing_derate -early 0.95"
    ]


def test_run_sta_timing_derate_every_corner(tmp_path, monkeypatch):
    request_path = _setup_multi_corner_env(
        tmp_path,
        monkeypatch,
        constraints=_propagated_constraints(timing_derate=_BOTH),
    )
    scripts = _stub_openroad_clock_capture(
        monkeypatch,
        {None: "propagated_buffered", "ss_100C_1v60": "propagated_buffered"},
    )
    report = run_sta(request_path)
    assert len(scripts) == 2
    for script in scripts:
        assert _derate_lines(script) == [
            "set_timing_derate -early 0.95",
            "set_timing_derate -late 1.05",
        ]
    # One hoisted echo, never repeated per corner.
    assert report["timing_derate"] == {"applied": True, "early": 0.95, "late": 1.05}
    assert all("timing_derate" not in entry for entry in report["corners"])


def test_run_sta_timing_derate_multi_corner_disabled(tmp_path, monkeypatch):
    request_path = _setup_multi_corner_env(
        tmp_path, monkeypatch, constraints=_propagated_constraints()
    )
    scripts = _stub_openroad_clock_capture(
        monkeypatch,
        {None: "propagated_buffered", "ss_100C_1v60": "propagated_buffered"},
    )
    report = run_sta(request_path)
    assert report["timing_derate"] == {"applied": False}
    assert all(_MARKER not in s for s in scripts)
