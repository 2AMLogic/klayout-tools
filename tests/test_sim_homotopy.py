"""DC operating-point stepping narration and its place in corner grading."""

import pytest

from helpers.subprocess_fakes import _stub_subprocess_run
from klayout_tools import sim, sim_homotopy
from test_sim import _RECOVERED_SINGULAR_MATRIX_LOG, _write_body, _write_request

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)

# Captured with ngspice-47 on a 2 V, equal-resistor divider using .options noopiter.
_DYNAMIC = (
    "Note: Starting dynamic gmin stepping\nNote: Dynamic gmin stepping completed\n"
)
# The same deck with gminsteps=2 (ngspice-47 prints this method in lowercase).
_SPICE3 = "Note: Starting spice3 gmin stepping\nNote: spice3 gmin stepping completed\n"
# The same deck with gminsteps=0.
_SOURCE = "Note: Starting source stepping\nNote: Source stepping completed\n"
# Captured during a single ngspice-47 .op run on this nonlinear circuit:
# V1 in 0 2
# R1 out 0 1k
# B1 out 0 I=v(out)*v(out)*v(out)-2*v(out)+v(in)
# .options noopiter gminsteps=2 itl1=10
_SPICE3_TO_SOURCE = (
    "Note: Starting spice3 gmin stepping\n"
    "Warning: spice3 gmin stepping failed\n"
    "Note: Starting source stepping\n"
    "Note: Source stepping completed\n"
)
_PRIOR = (
    "Note: Starting dynamic gmin stepping\nWarning: Dynamic gmin stepping failed\n"
    "Note: Starting true gmin stepping\nWarning: True gmin stepping failed\n"
)
_VALUE = "  Measurements for Transient Analysis\n\nvout = 1.00000e+00\n"
_CODE = "dc_homotopy_recovery"


@pytest.mark.parametrize(
    ("log", "method", "history"),
    [
        (_DYNAMIC, "dynamic gmin", ""),
        (_SOURCE, "source", ""),
        (_SPICE3, "spice3 gmin", ""),
        (_SPICE3_TO_SOURCE, "source", " after spice3 gmin stepping failed"),
        (
            _PRIOR + _SOURCE,
            "source",
            " after dynamic gmin stepping failed, true gmin stepping failed",
        ),
        (
            "Note: Starting source stepping\r\nNote: True gmin stepping completed\r\n",
            "true gmin",
            " after source stepping attempted",
        ),
        (_PRIOR + _SOURCE + _DYNAMIC, "dynamic gmin", ""),
    ],
)
def test_last_completed_method_and_its_history(log, method, history):
    assert sim_homotopy.homotopy_recovery_diagnostic(log) == {
        "code": _CODE,
        "severity": "info",
        "recovered_via": method.replace(" ", "_") + "_stepping",
        "message": f"DC operating point recovered via {method} stepping{history}",
    }


@pytest.mark.parametrize(
    "log",
    [
        "",
        _RECOVERED_SINGULAR_MATRIX_LOG,
        "echo Note: Source stepping completed",
        "Note: Source stepping completed later",
    ],
)
def test_no_completed_narration(log):
    assert sim_homotopy.homotopy_recovery_diagnostic(log) is None


def test_xyce_log_is_excluded():
    assert list(sim_homotopy.recovery_diagnostics("xyce", _SOURCE)) == []


def _request(tmp_path, codes):
    _write_body(tmp_path)
    return str(
        _write_request(
            tmp_path,
            {
                "netlist": "body.spice",
                "analysis": {"kind": "tran", "args": "1n 10n"},
                "measurements": [
                    {
                        "name": "vout",
                        "spice": ".meas tran vout FIND v(out) AT=8n",
                        "limits": {"min": 0.9, "max": 1.1},
                    }
                ],
                "options": {"fail_on_diagnostic": codes},
            },
        )
    )


@pytest.mark.parametrize(
    ("codes", "expected_status"), [([], "pass"), (["nonconvergence"], "inconclusive")]
)
def test_recovery_is_reported_after_grading(
    tmp_path, monkeypatch, codes, expected_status
):
    path = _request(tmp_path, codes)
    _stub_subprocess_run(monkeypatch, log_text=_PRIOR + _VALUE)
    baseline = sim.run_sim(path)
    _stub_subprocess_run(monkeypatch, log_text=_PRIOR + _SOURCE + _VALUE)
    report = sim.run_sim(path)
    (corner,) = report["corners"]
    (original,) = baseline["corners"]
    assert baseline["status"] == report["status"] == expected_status
    assert corner["status"] == original["status"] == expected_status
    assert corner["measurements"] == original["measurements"]
    assert [d for d in corner["diagnostics"] if d["code"] != _CODE] == original[
        "diagnostics"
    ]
    assert [
        (d["code"], d["severity"])
        for d in original["diagnostics"]
        if d["code"] == "nonconvergence"
    ] == [("nonconvergence", "warning")]
    assert report["diagnostic_counts"]["by_code"][_CODE] == 1
    assert report["diagnostic_counts"]["by_severity"]["info"] == 1


def test_later_abort_keeps_the_dc_recovery(tmp_path, monkeypatch):
    path = _request(tmp_path, [])
    _stub_subprocess_run(monkeypatch, log_text=_SOURCE + "tran simulation(s) aborted\n")
    (corner,) = sim.run_sim(path)["corners"]
    assert corner["status"] == "error"
    assert {"analysis_aborted", _CODE} <= {d["code"] for d in corner["diagnostics"]}


def test_recovery_is_not_a_selectable_failure(tmp_path):
    with pytest.raises(sim.SimError, match="unknown diagnostic code"):
        sim.run_sim(_request(tmp_path, [_CODE]))
