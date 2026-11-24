"""Supply-relative measurement limits are resolved independently at every corner."""

import json

import pytest

from helpers.subprocess_fakes import _stub_subprocess_run
from klayout_tools import sim
from klayout_tools.report_measurements import _limits
from klayout_tools.signoff_spec_row_table import _active_bound
from test_sim import _write_body, _write_request

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)


def _request(tmp_path, limits, **overrides):
    _write_body(tmp_path)
    return _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "level",
                    "spice": ".meas tran level FIND v(out) AT=1u",
                    "limits": limits,
                },
                {
                    "name": "plain",
                    "spice": ".meas tran plain FIND v(out) AT=1u",
                    "limits": {"min": 1},
                },
            ],
            "corners": {"supply_v": {"vdd": [1.62, 1.8, 1.98]}},
            **overrides,
        },
    )


def test_one_bound_grades_three_supplies(tmp_path, monkeypatch):
    limits = {"min": {"frac_of": "VDD", "fraction": 0.9}, "max": 2}
    path = _request(tmp_path, limits)
    _stub_subprocess_run(monkeypatch, log_text="level = 1.65\nplain = 1.65\n")
    report = sim.run_sim(str(path), backend="local")
    rows = [c["measurements"][0] for c in report["corners"]]
    assert [r["status"] for r in rows] == ["pass", "pass", "fail"]
    for corner, row in zip(report["corners"], rows, strict=True):
        assert row["limits_resolved"] == pytest.approx(
            {"min": 0.9 * corner["supply_v"]["vdd"], "max": 2}
        )
        assert "limits_resolved" not in corner["measurements"][1]
    assert report["measurements"][0]["limits"] == limits
    assert _limits(limits) == ">=0.9*VDD, <=2"
    assert _active_bound({"min": limits["min"]}, 1.65, 0.03) is None


@pytest.mark.parametrize(
    "bound",
    [
        {"frac_of": "absent", "fraction": 0.5},
        {"frac_of": "vdd", "fraction": -0.5},
        {"frac_of": "vdd", "fraction": float("inf")},
        {"frac_of": "vdd", "fraction": True},
        {"frac_of": "vdd", "extra": 0.5},
    ],
)
def test_invalid_bound_refused_before_dispatch(tmp_path, monkeypatch, bound):
    def unexpected(*args, **kwargs):
        pytest.fail("a corner was dispatched")

    monkeypatch.setattr(sim, "_run_corner", unexpected)
    with pytest.raises(sim.SimError, match=r"measurement 'level': limits\.max"):
        sim.run_sim(str(_request(tmp_path, {"max": bound})), backend="local")


@pytest.mark.parametrize("window", ["measurement", "run"])
def test_relative_sigma_window_refused(tmp_path, window):
    path = _request(tmp_path, {"min": {"frac_of": "vdd", "fraction": 0.9}})
    request = json.loads(path.read_text())
    mc = {"n": 2, "seed": 1, "vary": "mismatch"}
    request["monte_carlo"] = mc
    if window == "measurement":
        request["measurements"][0]["k_sigma"] = 1
    else:
        mc["k_sigma"] = 3
    with pytest.raises(
        sim.SimError, match=r"measurement 'level': limits\.min.*k_sigma"
    ):
        sim.run_sim(
            str(_write_request(tmp_path, request)), backend="local", dry_run=True
        )


def test_upper_bound_zero_and_unknown_limit_key(tmp_path, monkeypatch):
    limits = {"max": {"frac_of": "vdd", "fraction": 0}, "future": 17}
    path = _request(tmp_path, limits, corners={"supply_v": {"vdd": [1.8]}})
    _stub_subprocess_run(monkeypatch, log_text="level = 1.65\nplain = 1.65\n")
    report = sim.run_sim(str(path), backend="local")
    assert report["corners"][0]["measurements"][0]["limits_resolved"] == {"max": 0}
    assert report["coverage"]["unrecognized_limit_keys"] == [
        {"measurement": "level", "keys": ["future"]}
    ]


def test_missing_rail_in_later_unit_is_rejected(tmp_path):
    units = [{"supply_v": {"vdd": 1.8}}, {"supply_v": {}}]
    path = _request(
        tmp_path, {"max": {"frac_of": "vdd", "fraction": 1}}, corners={"units": units}
    )
    with pytest.raises(sim.SimError, match=r"measurement 'level': limits\.max"):
        sim.run_sim(str(path), backend="local", dry_run=True)


@pytest.mark.parametrize(
    ("limits", "value", "expected", "margin"),
    [
        ({"max": 0, "exclusive_max": True}, 0, "fail", 0),
        ({"min": 0, "exclusive_min": True}, 0, "fail", 0),
        ({"max": 0, "exclusive_max": False}, 0, "pass", 0),
        ({"min": 0, "max": 2, "exclusive_min": True}, 2, "pass", 0),
        ({"min": 0, "max": 2, "exclusive_max": True}, 1, "pass", 1),
        (
            {"min": {"frac_of": "vdd", "fraction": 0.5}, "exclusive_min": True},
            0.9,
            "fail",
            0,
        ),
    ],
)
def test_exclusive_limits_grade_and_cover_the_bound(
    tmp_path, monkeypatch, limits, value, expected, margin
):
    path = _request(tmp_path, limits, corners={"supply_v": {"vdd": [1.8]}})
    _stub_subprocess_run(monkeypatch, log_text=f"level = {value}\nplain = 1.65\n")
    report = sim.run_sim(str(path), backend="local")
    measurement = report["corners"][0]["measurements"][0]
    assert (measurement["status"], measurement["margin"]) == (expected, margin)
    assert report["status"] == report["measurements"][0]["status"] == expected
    assert report["measurements"][0]["limits"] == limits
    coverage = report["coverage"]
    assert coverage["skipped"] == coverage["unrecognized_limit_keys"] == []
    assert len(coverage["checked"]) == 1 + sum(key in limits for key in ("min", "max"))


@pytest.mark.parametrize(
    "limits",
    [
        {"max": 1, "exclusive_max": "true"},
        {"exclusive_min": True},
        {"exclusive_max": True, "max": None},
        {"exclusive_min": True, "min": False},
    ],
)
def test_invalid_exclusive_limit_refused_before_dispatch(tmp_path, limits):
    with pytest.raises(sim.SimError, match="exclusive_(max|min)"):
        sim.run_sim(str(_request(tmp_path, limits)), backend="local", dry_run=True)


@pytest.mark.parametrize("stepped", [True, False])
def test_older_batch_runner_refused_for_strict_bounds(stepped):
    from klayout_tools import sim_batch_contract_spec as contract

    limits = {"min": 1, "exclusive_min": True}
    scope = {"measurements": [{"limits": limits}]}
    request = {"analysis_steps": [scope]} if stepped else scope
    features = contract._request_features(request)
    assert "exclusive_bounds" in contract._MANDATORY_RUNNER_FEATURES
    assert "exclusive_bounds" in contract._missing_runner_features(features, "0.9.2")
    assert "exclusive_bounds" not in contract._missing_runner_features(
        features, "0.9.3"
    )
    limits["exclusive_min"] = False
    assert "exclusive_bounds" not in contract._request_features(request)
