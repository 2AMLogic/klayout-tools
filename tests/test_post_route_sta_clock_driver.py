"""Primary clock port driving cells in request validation, Tcl, and reports."""

import pytest

from klayout_tools.post_route_sta import PostRouteStaError, run_sta
from test_post_route_sta import (
    _live_sky130_env,
    _propagated_constraints,
    _setup_multi_corner_env,
    _setup_success_env,
    _setup_verilog_success_env,
    _sta_script_text,
    _stub_openroad_clock_capture,
    _stub_openroad_success,
    _write_clock_fixtures,
    _write_request,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_CELL = {"cell": "buf_2", "pin": "X"}


@pytest.mark.parametrize("mode", ["def", "verilog"])
def test_primary_driver_and_data_conditions(tmp_path, monkeypatch, mode):
    constraints = _propagated_constraints(clock_driving_cell=_CELL)
    if mode == "def":
        constraints.update(propagated_clock=True, input_transition_ns=0.12)
        path = _setup_success_env(tmp_path, monkeypatch, constraints=constraints)
    else:
        constraints["driving_cell"] = {"cell": "inv_1", "pin": "Y"}
        path = _setup_verilog_success_env(
            tmp_path, monkeypatch, constraints=constraints
        )
    _stub_openroad_success(monkeypatch)
    report = run_sta(path)
    script = _sta_script_text(tmp_path)
    cmd = "set_driving_cell -lib_cell buf_2 -pin X [get_ports clk]"
    assert script.count(cmd) == 1
    assert (
        script.index("create_clock")
        < script.index(cmd)
        < script.index("report_worst_slack_metric")
    )
    assert report["clock_driving_cell"] == _CELL
    if mode == "def":
        assert (
            "set_propagated_clock" in script and "set_input_transition 0.12" in script
        )
    else:
        assert (
            "set_driving_cell -lib_cell inv_1 -pin Y $klt_drive_default_ports" in script
        )


def test_driver_applies_once_per_corner_only_to_first_clock(tmp_path, monkeypatch):
    cell = {**_CELL, "input_pin": "A"}
    constraints = {
        "clocks": [{"port": "clk", "period_ns": 2}, {"port": "clk_b", "period_ns": 4}],
        "clock_driving_cell": cell,
    }
    path = _setup_multi_corner_env(tmp_path, monkeypatch, constraints=constraints)
    scripts = _stub_openroad_clock_capture(monkeypatch, {None: "propagated_buffered"})
    report = run_sta(path)
    assert report["clock_driving_cell"] == cell
    assert len(scripts) == 2
    for script in scripts:
        assert script.count("set_driving_cell ") == 1
        assert (
            "set_driving_cell -lib_cell buf_2 -pin X -from_pin A [get_ports clk]"
            in script
        )
    assert all("clock_driving_cell" not in row for row in report["corners"])


@pytest.mark.parametrize("field", [{}, {"clock_driving_cell": None}])
def test_absent_or_null_driver_omits_command_and_echo(tmp_path, monkeypatch, field):
    path = _setup_success_env(
        tmp_path, monkeypatch, constraints=_propagated_constraints(**field)
    )
    _stub_openroad_success(monkeypatch)
    assert "clock_driving_cell" not in run_sta(path)
    assert "set_driving_cell" not in _sta_script_text(tmp_path)


@pytest.mark.parametrize(
    ("bad", "msg"),
    [
        ({"cell": "buf"}, "pin is required"),
        ({"cell": "bad name", "pin": "X"}, "literal liberty name"),
        ({**_CELL, "extra": "value"}, "unknown key"),
    ],
)
def test_invalid_driver_reports_field_before_engine(tmp_path, monkeypatch, bad, msg):
    path = _setup_success_env(
        tmp_path,
        monkeypatch,
        constraints=_propagated_constraints(clock_driving_cell=bad),
    )
    _stub_openroad_success(monkeypatch)
    with pytest.raises(
        PostRouteStaError, match=rf"request\.constraints\.clock_driving_cell.*{msg}"
    ):
        run_sta(path)


@pytest.mark.skipif(
    _live_sky130_env() is None, reason="requires OpenROAD and the sky130A PDK"
)
def test_live_driver_on_propagated_buffered_clock(tmp_path):
    paths = _write_clock_fixtures(tmp_path)
    driver = {"cell": "sky130_fd_sc_hd__buf_2", "pin": "X", "input_pin": "A"}
    path = _write_request(
        tmp_path / "sta.json",
        {
            "def": paths["buffered.def"],
            "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
            "constraints": _propagated_constraints(
                propagated_clock=True, clock_driving_cell=driver
            ),
        },
    )
    report = run_sta(path, pdk_variant="sky130A")
    assert report["clock_driving_cell"] == driver
    assert report["clock_mode"] == "propagated"
    assert report["clock_propagation"]["clock_sinks"] > 0
