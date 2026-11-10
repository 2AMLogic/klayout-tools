"""Per-corner clock period maps for `klt sta` (issue #5240); the helpers live
in `klayout_tools/_sta_period_maps.py`. Split from `test_post_route_sta.py`
to respect the per-file size ratchet."""

from __future__ import annotations

import pytest

from klayout_tools import _sta_period_maps, post_route_sta
from klayout_tools.post_route_sta import PostRouteStaError, run_sta

pytestmark = pytest.mark.usefixtures("real_build_identity_git")
from test_post_route_sta import _setup_multi_corner_env, _stub_openroad_clock_capture

_PC = ("tt_025C_1v80", "ss_100C_1v60")


def test_validate_clock_period_map_accepted():
    constraints = {
        "clocks": [
            {"port": "a", "period_ns": {_PC[0]: 2.0, _PC[1]: 3}},
            {"port": "b", "period_ns": 5.0},
        ]
    }
    port, period, _, _ = post_route_sta._validate_constraints(constraints)
    assert (port, period) == ("a", {_PC[0]: 2.0, _PC[1]: 3.0})
    _sta_period_maps.check_period_maps(
        period, post_route_sta._validate_clock_extras(constraints), port, list(_PC)
    )


@pytest.mark.parametrize(
    "period, message",
    [
        ({}, "at least one corner"),
        ({_PC[0]: 0, _PC[1]: 1}, "greater than zero"),
        ({_PC[0]: True, _PC[1]: 1}, "positive number"),
        ({_PC[0]: "2", _PC[1]: 1}, "positive number"),
    ],
)
def test_validate_clock_period_map_rejects_bad_values(period, message):
    with pytest.raises(PostRouteStaError, match=message):
        post_route_sta._validate_constraints(
            {"clocks": [{"port": "a", "period_ns": period}]}
        )


@pytest.mark.parametrize(
    "period, corners, message",
    [
        ({_PC[0]: 2.0}, list(_PC), "missing corner"),
        ({_PC[0]: 2.0, _PC[1]: 2.0, "ff": 1.0}, list(_PC), "unknown corner"),
        ({_PC[0]: 2.0, _PC[1]: 2.0}, None, "requires request.pdk.corners"),
    ],
)
def test_period_map_key_mismatch_is_a_named_error(period, corners, message):
    constraints = {"clocks": [{"port": "a", "period_ns": period}]}
    port, per, _, _ = post_route_sta._validate_constraints(constraints)
    with pytest.raises(PostRouteStaError, match=message):
        _sta_period_maps.check_period_maps(per, None, port, corners)


def test_secondary_period_map_key_mismatch_is_a_named_error():
    constraints = {
        "clocks": [
            {"port": "a", "period_ns": 1.0},
            {"port": "b", "period_ns": {_PC[0]: 2.0}},
        ]
    }
    port, per, _, _ = post_route_sta._validate_constraints(constraints)
    extras = post_route_sta._validate_clock_extras(constraints)
    with pytest.raises(PostRouteStaError, match="missing corner"):
        _sta_period_maps.check_period_maps(per, extras, port, list(_PC))


def _per_corner_request(tmp_path, monkeypatch, clocks):
    request_path = _setup_multi_corner_env(
        tmp_path,
        monkeypatch,
        constraints={"clocks": clocks, "input_delay_ns": 0.0},
    )
    scripts = _stub_openroad_clock_capture(monkeypatch, {None: "propagated_buffered"})
    return request_path, scripts


def test_run_sta_per_corner_period_each_session_and_entry(tmp_path, monkeypatch):
    request_path, scripts = _per_corner_request(
        tmp_path,
        monkeypatch,
        [
            {"port": "clk", "period_ns": {_PC[0]: 2.0, _PC[1]: 3.0}},
            {"port": "clk2", "period_ns": {_PC[0]: 4.0, _PC[1]: 6.0}},
        ],
    )

    report = run_sta(request_path)

    assert len(scripts) == 2
    for script, (p1, p2) in zip(scripts, [(2.0, 4.0), (3.0, 6.0)], strict=True):
        assert f"create_clock -name clk -period {p1} [get_ports clk]" in script
        assert f"create_clock -name clk2 -period {p2} [get_ports clk2]" in script
    assert [e["clock_targets"]["clocks"] for e in report["corners"]] == [
        [{"port": "clk", "period_ns": 2.0}, {"port": "clk2", "period_ns": 4.0}],
        [{"port": "clk", "period_ns": 3.0}, {"port": "clk2", "period_ns": 6.0}],
    ]
    assert report["clock_targets"]["clocks"][0]["period_ns"] == {
        _PC[0]: 2.0,
        _PC[1]: 3.0,
    }


def test_run_sta_secondary_only_period_map(tmp_path, monkeypatch):
    request_path, scripts = _per_corner_request(
        tmp_path,
        monkeypatch,
        [
            {"port": "clk", "period_ns": 2.0},
            {"port": "clk2", "period_ns": {_PC[0]: 4.0, _PC[1]: 6.0}},
        ],
    )
    run_sta(request_path)
    assert "create_clock -name clk -period 2.0 [get_ports clk]" in scripts[0]
    assert "create_clock -name clk2 -period 4.0 [get_ports clk2]" in scripts[0]
    assert "create_clock -name clk2 -period 6.0 [get_ports clk2]" in scripts[1]


def test_run_sta_scalar_periods_leave_corner_entries_unchanged(tmp_path, monkeypatch):
    request_path, _ = _per_corner_request(
        tmp_path, monkeypatch, [{"port": "clk", "period_ns": 2.0}]
    )
    report = run_sta(request_path)
    assert all("clock_targets" not in e for e in report["corners"])
    assert report["clock_targets"]["clocks"] == [{"port": "clk", "period_ns": 2.0}]
