"""The clock uncertainty report agrees with the Tcl applied in each corner."""

import pytest

from klayout_tools.post_route_sta import run_sta
from test_post_route_sta import (
    _propagated_constraints,
    _setup_multi_corner_env,
    _setup_success_env,
    _stub_openroad_clock_capture,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


@pytest.mark.parametrize("multi_corner", [False, True])
@pytest.mark.parametrize(
    "sides, expected, commands",
    [
        ({"setup_ns": None}, {"applied": False}, []),
        ({"hold_ns": None}, {"applied": False}, []),
        ({"setup_ns": None, "hold_ns": None}, {"applied": False}, []),
        (
            {"setup_ns": None, "hold_ns": 0.125},
            {"applied": True, "setup_ns": None, "hold_ns": 0.125},
            ["set_clock_uncertainty -hold 0.125"],
        ),
        (
            {"setup_ns": 0, "hold_ns": None},
            {"applied": True, "setup_ns": 0.0, "hold_ns": None},
            ["set_clock_uncertainty -setup 0.0"],
        ),
    ],
)
def test_clock_uncertainty_echo(
    tmp_path, monkeypatch, multi_corner, sides, expected, commands
):
    setup = _setup_multi_corner_env if multi_corner else _setup_success_env
    request_path = setup(
        tmp_path,
        monkeypatch,
        constraints=_propagated_constraints(clock_uncertainty_ns=sides),
    )
    scripts = _stub_openroad_clock_capture(
        monkeypatch,
        {None: "propagated_buffered", "ss_100C_1v60": "propagated_buffered"},
    )

    assert run_sta(request_path)["clock_uncertainty"] == expected
    assert len(scripts) == (2 if multi_corner else 1)
    for script in scripts:
        assert [
            line.split(" [get_clocks")[0]
            for line in script.splitlines()
            if line.startswith("set_clock_uncertainty ")
        ] == commands
