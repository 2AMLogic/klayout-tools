"""Per-entry ``clock`` references for ``klt sta`` I/O delays (issue #7399).

Covers the entry validation (accepted primary/secondary clocks, every
rejection shape), the Tcl emission in ``def``/``verilog`` mode and in every
``pdk.corners`` session, the negative control (a strobe-clocked port is
delayed against ``dqs`` and **not** against the primary), byte-identity of
``clock``-free and explicit-primary requests, and the ``sdc_check``
comparison of the SDC ``-clock`` against the entry's clock.
"""

from __future__ import annotations

import pytest

from klayout_tools import post_route_sta
from klayout_tools._sta_sdc_check import sdc_drift_block
from klayout_tools.post_route_sta import PostRouteStaError, run_sta
from test_post_route_sta import (
    _setup_multi_corner_env,
    _setup_success_env,
    _setup_verilog_success_env,
    _sta_script_text,
    _stub_openroad_multi_corner,
    _stub_openroad_success,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

#: primary + secondary strobe, the DDR shape the issue reports.
_CLOCKS = [
    {"port": "clk", "period_ns": 2.0},
    {"port": "dqs", "period_ns": 2.0},
]
_DECLARED = {"clk", "dqs"}
#: the strobe-relative entry the reporter needed (`d[0]` is a bus bit).
_STROBE_ENTRY = {"clock": "dqs", "min_ns": -0.1, "max_ns": 0.1}
_DQS_MIN = "set_input_delay -min -0.1 -clock dqs [dict get $klt_ports_by_name d\\[0\\]]"
_DQS_MAX = "set_input_delay -max 0.1 -clock dqs [dict get $klt_ports_by_name d\\[0\\]]"


# -- validation ------------------------------------------------------------- #


def test_entry_clock_accepted_secondary_and_explicit_primary():
    inputs, _ = post_route_sta._validate_port_delay_maps(
        {
            "input_delays": {
                "d[0]": dict(_STROBE_ENTRY),
                "a": {"clock": "clk", "max_ns": 0.5},
                "b": {"min_ns": 0.05},
            }
        },
        "clk",
        _DECLARED,
    )
    assert inputs == {
        "d[0]": {"min_ns": -0.1, "max_ns": 0.1, "clock": "dqs"},
        "a": {"max_ns": 0.5, "clock": "clk"},
        "b": {"min_ns": 0.05},
    }


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"clock": "nope", "min_ns": 0.1}, r"names 'nope', which is not a declared"),
        ({"clock": 5, "min_ns": 0.1}, "must be a non-empty string"),
        ({"clock": "", "min_ns": 0.1}, "must be a non-empty string"),
        ({"clock": None, "min_ns": 0.1}, "must be a non-empty string"),
        ({"clock": "dqs"}, "has no bound"),
        ({"clock": "dqs", "min_ns": 0.1, "extra": 1}, "unknown key"),
        ({"clock": True, "min_ns": 0.1}, "must be a non-empty string"),
    ],
)
def test_entry_clock_rejects(entry, message):
    with pytest.raises(PostRouteStaError, match=message):
        post_route_sta._validate_port_delay_maps(
            {"input_delays": {"d[0]": entry}}, "clk", _DECLARED
        )


def test_undeclared_clock_error_names_declared_clocks():
    with pytest.raises(PostRouteStaError) as exc:
        post_route_sta._validate_port_delay_maps(
            {"output_delays": {"q": {"clock": "nope", "max_ns": 0.4}}},
            "clk",
            _DECLARED,
        )
    assert "declared: clk, dqs" in str(exc.value)


def test_run_sta_entry_clock_error_precedes_engine(tmp_path, monkeypatch):
    """The clock reference is a request error raised before any engine run."""
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        constraints={
            "clocks": _CLOCKS,
            "input_delays": {"d[0]": {**_STROBE_ENTRY, "clock": "nope"}},
        },
    )
    calls: list = []
    monkeypatch.setattr(
        post_route_sta.subprocess, "run", lambda *a, **k: calls.append(a)
    )
    with pytest.raises(PostRouteStaError, match="not a declared clock"):
        run_sta(request_path)
    assert calls == []


# -- emission --------------------------------------------------------------- #


def test_strobe_relative_entry_emitted_against_dqs_not_clk(tmp_path, monkeypatch):
    """Negative control: the strobe-clocked bus bit is delayed against
    ``dqs`` and NOT against the primary -- the assertions fail if the
    entry ``clock`` were ignored."""
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        constraints={
            "clocks": _CLOCKS,
            "input_delay_ns": 0.2,
            "input_delays": {"d[0]": dict(_STROBE_ENTRY)},
        },
    )
    _stub_openroad_success(monkeypatch)
    assert run_sta(request_path)["status"] == "ok"
    script = _sta_script_text(tmp_path)
    assert _DQS_MIN in script and _DQS_MAX in script
    assert "create_clock -name dqs -period 2.0 [get_ports dqs]" in script
    # the scalar default keeps the primary clock ...
    assert "set_input_delay 0.2 -clock clk $klt_input_default_ports" in script
    # ... and the strobe port never gets a primary-clock line of its own.
    for line in script.splitlines():
        if "[dict get $klt_ports_by_name d\\[0\\]]" in line:
            assert "-clock dqs" in line, line


def test_primary_clock_named_explicitly_is_byte_identical(tmp_path, monkeypatch):
    scripts = []
    for clock in (None, "clk"):
        entry = {"min_ns": -0.1, "max_ns": 0.1}
        if clock is not None:
            entry["clock"] = clock
        request_path = _setup_success_env(
            tmp_path,
            monkeypatch,
            constraints={
                "clocks": _CLOCKS,
                "input_delays": {"d[0]": entry},
            },
        )
        _stub_openroad_success(monkeypatch)
        run_sta(request_path)
        scripts.append(_sta_script_text(tmp_path))
    assert scripts[0] == scripts[1]


def test_output_delay_entry_clock_emitted(tmp_path, monkeypatch):
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        constraints={
            "clocks": _CLOCKS,
            "output_delays": {"q": {"clock": "dqs", "max_ns": 0.4}},
        },
    )
    _stub_openroad_success(monkeypatch)
    run_sta(request_path)
    script = _sta_script_text(tmp_path)
    assert (
        "set_output_delay -max 0.4 -clock dqs [dict get $klt_ports_by_name q]" in script
    )
    assert "-clock clk [dict get $klt_ports_by_name q]" not in script


def test_verilog_mode_emits_entry_clock(tmp_path, monkeypatch):
    request_path = _setup_verilog_success_env(
        tmp_path,
        monkeypatch,
        constraints={
            "clocks": _CLOCKS,
            "input_delays": {"d[0]": dict(_STROBE_ENTRY)},
        },
    )
    _stub_openroad_success(monkeypatch)
    run_sta(request_path)
    script = _sta_script_text(tmp_path)
    assert _DQS_MIN in script and _DQS_MAX in script


def test_every_corner_session_emits_entry_clock(tmp_path, monkeypatch):
    request_path = _setup_multi_corner_env(
        tmp_path,
        monkeypatch,
        constraints={
            "clocks": _CLOCKS,
            "input_delays": {"d[0]": dict(_STROBE_ENTRY)},
        },
    )
    _stub_openroad_multi_corner(monkeypatch)
    report = run_sta(request_path)
    assert report["status"] == "ok"
    for corner in ("tt_025C_1v80", "ss_100C_1v60"):
        script = _sta_script_text(tmp_path, f"sta_top__{corner}.tcl")
        assert _DQS_MIN in script and _DQS_MAX in script, corner


def test_entry_clock_under_per_corner_period_map(tmp_path, monkeypatch):
    """A secondary clock carrying a per-corner ``period_ns`` map still
    accepts -- and emits -- entries referenced against it."""
    request_path = _setup_multi_corner_env(
        tmp_path,
        monkeypatch,
        constraints={
            "clocks": [
                {"port": "clk", "period_ns": 2.0},
                {
                    "port": "dqs",
                    "period_ns": {"tt_025C_1v80": 2.0, "ss_100C_1v60": 2.5},
                },
            ],
            "input_delays": {"d[0]": dict(_STROBE_ENTRY)},
        },
    )
    _stub_openroad_multi_corner(monkeypatch)
    run_sta(request_path)
    tt = _sta_script_text(tmp_path, "sta_top__tt_025C_1v80.tcl")
    ss = _sta_script_text(tmp_path, "sta_top__ss_100C_1v60.tcl")
    assert _DQS_MIN in tt and _DQS_MIN in ss
    assert "create_clock -name dqs -period 2.0 [get_ports dqs]" in tt
    assert "create_clock -name dqs -period 2.5 [get_ports dqs]" in ss


# -- sdc_check -------------------------------------------------------------- #

_SDC_CLOCKS = [
    {"port": "clk", "period_ns": 2.0},
    {"port": "dqs", "period_ns": 2.0},
]


def _drift_block(tmp_path, sdc, **kw):
    path = tmp_path / "c.sdc"
    path.write_text(sdc, encoding="utf-8")
    args = dict(
        request_dir=str(tmp_path),
        clocks=_SDC_CLOCKS,
        clock_port="clk",
        input_delay_ns=None,
        output_delay_ns=None,
        # a scalar port (not a bus bit): the SDC reader's tokenizer does not
        # split it, so the command lands in the checked subset.
        input_delays={"d0": dict(_STROBE_ENTRY)},
        output_delays=None,
        clock_uncertainty=None,
    )
    args.update(kw)
    return sdc_drift_block(str(path), **args)


_BASE_SDC = (
    "create_clock -name clk -period 2.0 [get_ports clk]\n"
    "create_clock -name dqs -period 2.0 [get_ports dqs]\n"
)


def test_sdc_clock_matching_entry_clock_is_match(tmp_path):
    out = _drift_block(
        tmp_path,
        _BASE_SDC + "set_input_delay -min -0.1 -clock dqs [get_ports d0]\n"
        "set_input_delay -max 0.1 -clock dqs [get_ports d0]\n",
    )
    assert out["status"] == "match"
    assert out["mismatches"] == []


def test_sdc_clock_disagreement_is_drift(tmp_path):
    out = _drift_block(
        tmp_path,
        _BASE_SDC + "set_input_delay -min -0.1 -clock clk [get_ports d0]\n"
        "set_input_delay -max 0.1 -clock clk [get_ports d0]\n",
    )
    assert out["status"] == "drift"
    for m in out["mismatches"]:
        assert (m["sdc_clock"], m["request_clock"]) == ("clk", "dqs")
        want = -0.1 if m["bound"] == "min" else 0.1
        assert (m["sdc_value"], m["request_value"]) == (want, want)
    assert {m["bound"] for m in out["mismatches"]} == {"min", "max"}


def test_sdc_primary_clock_on_clockless_entry_is_match(tmp_path):
    out = _drift_block(
        tmp_path,
        _BASE_SDC + "set_input_delay 0.1 -clock clk [get_ports d0]\n",
        input_delays={"d0": {"min_ns": 0.1, "max_ns": 0.1}},
    )
    assert out["status"] == "match"
