"""PEX row-level attribution of ngspice measurement window misses."""

import pytest

from klayout_tools import sim
from klayout_tools.pex import _build_delta_rows

# Issue #7904: the diagnostics below are verbatim from a real single-corner
# `klt sim` run (ngspice-47) of a node coupled only through capacitors, so the
# DC matrix is singular, with a `.meas ... WHEN` that never crosses. ngspice
# narrates the stepping it recovers from; `_recovered_from_stepping` refuses to
# forgive that narration once a measurement has no value, so it stays at
# `severity: "error"` and sorts ahead of the measurement diagnostic.
_STEPPING_SINGULAR = {
    "severity": "error",
    "code": "singular_matrix",
    "message": "Warning: singular matrix:  check node mid",
}
_STEPPING_GMIN = {
    "severity": "error",
    "code": "nonconvergence",
    "message": "Warning: Dynamic gmin stepping failed",
}
_WINDOW_MISS = {
    "severity": "error",
    "code": "measurement",
    "message": (
        "measurement 'tcross' produced no value -- ngspice reported: out of "
        "interval -- the analysis completed but the target was never reached "
        "inside the analysis window (not a simulator failure): lengthen the "
        'window, or set `on_unreached: "null"` / `optional: true` on this '
        "measurement to grade it instead of erroring"
    ),
}
_NO_CAUSE_MEASUREMENT = {
    "severity": "error",
    "code": "measurement",
    "message": "measurement 'tcross' produced no value",
}
_ENGINE_ABORTED = {
    "severity": "error",
    "code": "analysis_aborted",
    "message": "analysis aborted by the engine: run simulation(s) aborted",
}


def _extracted_row_error(diagnostics, **kwargs):
    return _rows(diagnostics, **kwargs)[0]["error"]


def _rows(diagnostics, *, names=("tcross",), remote=None):
    def side(status, value):
        return {
            "corner_id": "tt/1.800V/27C",
            "status": status,
            "measurements": [
                {"name": n, "value": value, "status": status} for n in names
            ],
        }

    extracted = {**side("error", None), "diagnostics": diagnostics}
    return _build_delta_rows(
        spec_row_prefix=None,
        schematic_report={"corners": [side("pass", 1.0)]},
        extracted_report={"environment": {"remote": remote}, "corners": [extracted]},
    )


def test_delta_row_error_names_a_window_miss_not_the_stepping_narration():
    error = _extracted_row_error([_STEPPING_SINGULAR, _STEPPING_GMIN, _WINDOW_MISS])
    assert error["code"] == "measurement"
    assert error["message"] == _WINDOW_MISS["message"]
    assert error["side"] == "extracted"


def test_delta_row_error_names_a_window_miss_when_it_precedes_the_narration():
    error = _extracted_row_error([_WINDOW_MISS, _STEPPING_SINGULAR])
    assert error["code"] == "measurement"


def test_delta_row_error_keeps_singular_matrix_when_the_engine_aborted():
    # A genuine solver abort: the engine's own abort trailer is an
    # error-severity `analysis_aborted`, so the narration is not forgiven even
    # beside a measurement diagnostic.
    error = _extracted_row_error(
        [_STEPPING_SINGULAR, _ENGINE_ABORTED, _NO_CAUSE_MEASUREMENT]
    )
    assert error["code"] == "singular_matrix"
    error = _extracted_row_error([_STEPPING_SINGULAR, _WINDOW_MISS, _ENGINE_ABORTED])
    assert error["code"] == "singular_matrix"


def test_delta_row_error_keeps_singular_matrix_without_a_window_miss():
    # No engine statement that the analysis completed: nothing absolves the
    # solver, so the first error-severity diagnostic still wins.
    malformed = {
        "severity": "error",
        "code": "measurement",
        "message": (
            "measurement 'tcross' produced no value -- ngspice reported: "
            "Expressions like 0.5*1.62 are not supported."
        ),
    }
    for measurement in (_NO_CAUSE_MEASUREMENT, malformed):
        error = _extracted_row_error([_STEPPING_SINGULAR, measurement])
        assert error["code"] == "singular_matrix"


def test_delta_row_error_prefers_an_error_over_a_warning_singular_matrix():
    # Already true since v0.9.2 (`4bebc87f6`); pinned so the window-miss rule
    # cannot regress it.
    warned = {**_STEPPING_SINGULAR, "severity": "warning"}
    error = _extracted_row_error([warned, _WINDOW_MISS])
    assert error["code"] == "measurement"


def test_delta_row_error_window_miss_keeps_the_remote_job_metadata():
    error = _extracted_row_error(
        [_STEPPING_SINGULAR, _STEPPING_GMIN, _WINDOW_MISS], remote={"job_id": "job-9"}
    )
    assert error["code"] == "measurement" and error["remote"] == {"job_id": "job-9"}


def test_each_failed_measurement_names_its_own_cause():
    first = {
        **_WINDOW_MISS,
        "message": _WINDOW_MISS["message"].replace("tcross", "tcross_max"),
    }
    rows = _rows([_STEPPING_GMIN, first, _WINDOW_MISS], names=("tcross", "tcross_max"))
    assert {r["spec_row"]: r["error"]["message"] for r in rows} == {
        "tcross": _WINDOW_MISS["message"],
        "tcross_max": first["message"],
    }


def test_an_unrelated_window_miss_never_replaces_a_rows_own_measurement_error():
    other = {
        **_NO_CAUSE_MEASUREMENT,
        "message": "measurement 'other' produced no value",
    }
    rows = _rows([_STEPPING_GMIN, _WINDOW_MISS, other], names=("tcross", "other"))
    assert {r["spec_row"]: r["error"]["message"] for r in rows} == {
        "tcross": _WINDOW_MISS["message"],
        "other": other["message"],
    }


@pytest.mark.parametrize("absent_side", ["schematic", "extracted"])
@pytest.mark.parametrize("sibling_misses", [True, False])
def test_asymmetric_optional_measurement_names_its_own_absence(
    absent_side, sibling_misses
):
    optional, absent = sim._absent_measurement({"name": "tcross_hi"})
    other, other_absent = sim._absent_measurement({"name": "tcross_hi_other"})
    present = {"name": optional["name"], "value": 1.0, "status": "pass"}
    required = {"name": "tcross", "value": 1.0, "status": "pass"}
    sides = {
        side: {
            "corners": [
                {
                    "corner_id": "tt",
                    "measurements": [required, present, other],
                    "diagnostics": [other_absent],
                }
            ]
        }
        for side in ("schematic", "extracted")
    }
    failed = sides[absent_side]["corners"][0]
    failed["measurements"][1] = optional
    failed["diagnostics"] = [other_absent, absent]
    if sibling_misses:
        failed["measurements"][0] = {**required, "value": None, "status": "error"}
        failed["diagnostics"] = [
            _STEPPING_SINGULAR,
            _WINDOW_MISS,
            *failed["diagnostics"],
        ]

    required_row, optional_row, symmetric_row = _build_delta_rows(
        spec_row_prefix=None,
        schematic_report=sides["schematic"],
        extracted_report=sides["extracted"],
    )
    assert optional_row["status"] == "error"
    assert optional_row["delta_pct"] is None
    assert optional_row["error"]["side"] == absent_side
    assert optional_row["error"]["code"] == absent["code"]
    assert optional_row["error"]["message"] == absent["message"]
    assert symmetric_row["status"] == "pass"
    assert "error" not in symmetric_row
    if sibling_misses:
        assert required_row["error"]["message"] == _WINDOW_MISS["message"]
