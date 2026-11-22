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
    _PULSE_BODY,
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
@pytest.mark.parametrize(
    ("operand", "measurement_status", "report_status", "exit_code"),
    [
        ("v(absent)", "error", "error", 4),
        ("v(unsaved)", "error", "error", 4),
        ("v(x)", "not_reached", "pass_partial", 0),
        ("i(vx)", "not_reached", "pass_partial", 0),
    ],
)
def test_find_when_miss_requires_its_operand_on_the_step_plot(
    tmp_path, operand, measurement_status, report_status, exit_code
):
    card = f".meas tran suspect find {operand} when v(x)=2 rise=1"
    request = _request(
        [
            _edge_step(
                [
                    {"name": "hit", "spice": _HIT_CARD, "limits": {"max": 1e-8}},
                    {
                        "name": "suspect",
                        "spice": card,
                        "on_unreached": "null",
                        "limits": {"max": 1},
                    },
                ]
            )
        ]
    )
    request["options"] = {"save_mode": "netlist"}
    _write_body(tmp_path, _PULSE_BODY + "Ry unsaved 0 1k\n.save v(x) i(vx)\n")
    report = sim.run_sim(str(_write_request(tmp_path, request)))
    hit, suspect = report["corners"][0]["measurements"]
    assert hit["status"] == "pass"
    assert suspect["status"] == measurement_status
    assert report["status"] == report_status
    assert _exit_code_for(report, deck_mode=False) == exit_code


@_SKIP_NO_NGSPICE
def test_find_operand_is_checked_on_the_plot_at_the_time_of_the_card(tmp_path):
    intermediate = {"name": "wave", "expr": "v(x)", "intermediate": True}
    find = {
        "name": "sample",
        "spice": ".meas tran sample find wave when v(x)=2 rise=1",
        "on_unreached": "null",
    }
    request = _request(
        [
            {**_edge_step([intermediate, find]), "name": "first"},
            {**_edge_step([find, intermediate]), "name": "second"},
        ]
    )
    _write_body(tmp_path)
    report = sim.run_sim(str(_write_request(tmp_path, request)))
    assert [(m["name"], m["status"]) for m in report["corners"][0]["measurements"]] == [
        ("first.sample", "not_reached"),
        ("second.sample", "error"),
    ]


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
