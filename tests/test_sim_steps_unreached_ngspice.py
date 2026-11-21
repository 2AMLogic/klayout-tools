"""Real-`ngspice` integration tests for step-scoped `on_unreached` (issue #7072).

The integration half of `tests/test_sim_steps_unreached.py`, split into its
own file to keep both modules under the repository's 500-line new-module
limit. The unit tests there stub `subprocess.run`; these run the real
`ngspice -b` and skip without it. The request/bench helpers are shared with
the sibling module rather than duplicated.
"""

from __future__ import annotations

import pytest

from helpers.tool_probes import SKIP_NO_NGSPICE as _SKIP_NO_NGSPICE
from klayout_tools import sim
from klayout_tools.cli.sim_cmd import _exit_code_for
from test_sim_steps_unreached import (
    _HIT_CARD,
    _MISSING_CARD,
    _edge_step,
    _request,
    _write_body,
    _write_request,
)

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)


@_SKIP_NO_NGSPICE
def test_real_ngspice_unreached_step_card_grades_not_reached(tmp_path):
    request = _request(
        [
            _edge_step(
                [
                    {
                        "name": "hit",
                        "spice": _HIT_CARD,
                        "unit": "s",
                        "limits": {"max": 1e-08},
                    },
                    {
                        "name": "missing",
                        "spice": _MISSING_CARD,
                        "on_unreached": "null",
                        "unit": "s",
                        "limits": {"max": 1e-08},
                    },
                ]
            )
        ]
    )
    _write_body(tmp_path)
    report = sim.run_sim(str(_write_request(tmp_path, request)))
    corner = report["corners"][0]
    by_name = {m["name"]: m for m in corner["measurements"]}
    assert by_name["edge.hit"]["status"] == "pass"
    assert by_name["edge.hit"]["value"] == pytest.approx(1.08e-09, abs=1e-11)
    assert by_name["edge.missing"] == {
        "name": "edge.missing",
        "value": None,
        "unit": "s",
        "status": "not_reached",
        "margin": None,
        "step": "edge",
    }
    assert corner["status"] == "pass"
    assert [s["status"] for s in corner["steps"]] == ["ok"]
    assert any(
        (d["severity"], d["code"]) == ("info", "measurement_not_reached")
        for d in corner["diagnostics"]
    )
    assert report["not_reached"] == 1
    assert report["status"] == "pass_partial"
    assert _exit_code_for(report, deck_mode=False) == 0


@_SKIP_NO_NGSPICE
def test_real_ngspice_without_opt_in_keeps_the_error(tmp_path):
    request = _request(
        [
            _edge_step(
                [
                    {
                        "name": "missing",
                        "spice": _MISSING_CARD,
                        "unit": "s",
                        "limits": {"max": 1e-08},
                    }
                ]
            )
        ]
    )
    _write_body(tmp_path)
    report = sim.run_sim(str(_write_request(tmp_path, request)))
    corner = report["corners"][0]
    assert corner["measurements"][0]["status"] == "error"
    assert corner["status"] == "error"
    assert report["status"] == "error"
    assert "not_reached" not in report
    assert _exit_code_for(report, deck_mode=False) == 4


@_SKIP_NO_NGSPICE
def test_real_ngspice_two_steps_sharing_a_local_name_stay_independent(tmp_path):
    measurements = [{"name": "missing", "spice": _MISSING_CARD, "on_unreached": "null"}]
    request = _request(
        [
            {
                "name": "early",
                "analysis": {"kind": "tran", "args": "20p 20n"},
                "measurements": measurements,
            },
            {
                "name": "late",
                "analysis": {"kind": "tran", "args": "20p 20n"},
                "measurements": [
                    {
                        "name": "missing",
                        "spice": ".meas tran missing when v(x)=0.8 rise=1",
                        "on_unreached": "null",
                        "unit": "s",
                    }
                ],
            },
        ]
    )
    _write_body(tmp_path)
    report = sim.run_sim(str(_write_request(tmp_path, request)))
    corner = report["corners"][0]
    by_name = {m["name"]: m for m in corner["measurements"]}
    # The same local card name: the unreachable 2 V target grades
    # not_reached under its own step, the reachable 0.8 V one passes under
    # the other -- evidence never crosses the step boundary.
    assert by_name["early.missing"]["status"] == "not_reached"
    assert by_name["early.missing"]["step"] == "early"
    assert by_name["late.missing"]["status"] == "pass"
    assert by_name["late.missing"]["value"] == pytest.approx(1.08e-09, abs=1e-11)
    assert [s["status"] for s in corner["steps"]] == ["ok", "ok"]
