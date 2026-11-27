"""Off-host expression diagnostics use runner identity and the first missing value."""

import pytest

from helpers.tool_probes import SKIP_NO_NGSPICE
from klayout_tools import sim
from klayout_tools import sim_batch_hints as hints
from test_sim_steps import (
    _grade,
    _resolve,
    _step_log,
    _two_point_request,
    _write_body,
    _write_request,
)


@pytest.mark.parametrize("version", ["0.7.0", "0.9.0", "0.9.0.dev3+gabc"])
def test_expr_on_supporting_runner_reports_first_missing_measurement(version):
    expr = {"name": "seq.edge", "expr": "(t80-t20) / (swing gt 0)"}
    first = "measurement 'seq.edge' produced no value: no scalar printed"
    corners = [
        {
            "corner_id": "tt",
            "measurements": [{"name": "seq.edge", "value": None}],
            "diagnostics": [{"code": "measurement", "message": first}],
        }
    ]
    remote = {"runner_klt_version": version, "client_klt_version": "0.9.3.dev2586"}
    for identity in [remote, {"fleet": [remote, remote]}]:
        report = {"environment": {"remote": identity}}
        hints.attach_runner_hints(report, [expr], corners, backend="batch")
        [warning] = report["diagnostics"]
        assert warning["code"] == "expr_no_value"
        assert warning["measurements"] == ["seq.edge"]
        for detail in [version, "0.9.3.dev2586", first, "tt"]:
            assert detail in warning["message"]


@pytest.mark.parametrize(
    "identity",
    [
        {"runner_klt_version": "0.6.9"},
        {"runner_klt_version": "0.7.0.dev12"},
        {"runner_klt_version": None},
        {"fleet": [{"runner_klt_version": "0.9.0"}, None]},
        {"fleet": [{"runner_klt_version": "0.9.0"}, {"runner_klt_version": "0.6.9"}]},
    ],
)
def test_unverified_expr_support_preserves_the_possible_version_hint(identity):
    report = {"environment": {"remote": identity}}
    hints.attach_runner_hints(
        report,
        [{"name": "v", "expr": "v(out)"}],
        [{"measurements": [{"name": "v", "value": None}]}],
        backend="batch",
    )
    assert report["diagnostics"][0]["code"] == "expr_runner_may_predate"


@SKIP_NO_NGSPICE
def test_reversed_edge_keeps_the_step_measurement_failure_on_a_supporting_runner(
    tmp_path,
):
    _write_body(tmp_path)
    measurements = [
        {"name": "swing", "expr": "v(out) - v(in)", "intermediate": True},
        {"name": "level", "expr": "v(out)"},
        {"name": "edge", "expr": "1 / (swing gt 0)"},
    ]
    request = {
        "netlist": "body.spice",
        "analysis_steps": [
            {
                "name": "seq",
                "analysis": {"kind": "op", "args": ""},
                "measurements": measurements,
            }
        ],
    }
    report = sim.run_sim(str(_write_request(tmp_path, request)), backend="local")
    [corner] = report["corners"]
    assert corner["steps"][0]["status"] == "ok"
    assert {m["name"]: m["value"] for m in corner["measurements"]} == {
        "seq.level": pytest.approx(2.0),
        "seq.edge": None,
    }
    report["environment"]["remote"] = {
        "client_klt_version": "0.9.3.dev2586",
        "runner_klt_version": "0.9.0",
    }
    _, specs = _resolve(request)
    sim._attach_runner_hints(report, specs, report["corners"], backend="batch")
    [warning] = report["diagnostics"]
    assert warning["code"] == "expr_no_value"
    assert corner["diagnostics"][0]["message"] in warning["message"]


def test_derived_expression_failure_keeps_first_matching_corner_evidence():
    request = _two_point_request()
    _, specs = _resolve(request)
    results, diagnostics, _ = _grade(
        request, _step_log(steps=2, values={"lo__vout": 2.0, "hi__vout": 2.2})
    )
    [failure] = diagnostics
    assert failure["code"] == "measurement"
    assert failure["message"].startswith(
        "derived measurement 'line_sens' produced no value: every input"
    )
    corners = [
        {
            "corner_id": "ss",
            "measurements": results,
            "diagnostics": [
                {"code": "worker", "message": failure["message"]},
                {"code": "measurement", "message": "derived measurement 'line_sens'"},
                {
                    "code": "measurement",
                    "message": failure["message"].replace("line_sens", "other"),
                },
                {
                    "code": "measurement",
                    "message": "measurement 'line_sens' target not reached",
                },
            ],
        },
        {"corner_id": "tt", "measurements": results, "diagnostics": diagnostics},
    ]
    report = {"environment": {"remote": {"runner_klt_version": "0.9.0"}}}
    hints.attach_runner_hints(report, specs, corners, backend="batch")
    [warning] = report["diagnostics"]
    assert warning["code"] == "expr_no_value"
    assert warning["measurements"] == ["line_sens"]
    assert warning["message"].endswith(f"First failure in tt: {failure['message']}")
    assert corners[1]["diagnostics"] == [failure]
