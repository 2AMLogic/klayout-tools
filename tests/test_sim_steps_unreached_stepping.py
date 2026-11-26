"""Step-scoped `on_unreached` beside stepping narration (issue #7906).

The step twin of `tests/test_sim_window_miss.py`'s stepping block: a step-local
`.meas` window miss that opted into `on_unreached: "null"` still grades
`not_reached` when the corner's log also carries gmin/source-stepping
narration, unless the engine's abort trailer says the analysis never
completed. The request/bench helpers are shared with
`tests/test_sim_steps_unreached.py` (which is at its file-size ceiling).
"""

from __future__ import annotations

import pytest

from test_sim_steps_unreached import (
    _HIT_CARD,
    _MISSING_CARD,
    _MISSING_CMD,
    _confirmed_miss,
    _edge_step,
    _region_log,
    _request,
    _run,
)

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)

_NARRATION = (
    "Warning: singular matrix: check node x\n"
    "Dynamic gmin stepping failed\n"
    "True gmin stepping failed\n"
)
_ABORT_TRAILER = "run simulation(s) aborted\n"
_OTHER_CARD = ".meas tran other when v(x)=3 rise=1 td=4n"
_OTHER_CMD = "meas tran other when v(x)=3 rise=1 td=4n"


def _opted_in(**extra) -> dict:
    return {"name": "missing", "spice": _MISSING_CARD, "on_unreached": "null", **extra}


def _corner(tmp_path, monkeypatch, measurements, log_text):
    report = _run(tmp_path, monkeypatch, _request([_edge_step(measurements)]), log_text)
    return report["corners"][0]


def _codes(corner: dict) -> list[tuple[str, str]]:
    return [(d["severity"], d["code"]) for d in corner["diagnostics"]]


def _missing_log(extra: str = "", *, narration: str = _NARRATION) -> str:
    evidence = _confirmed_miss("missing", _MISSING_CMD) + extra
    return narration + _region_log(steps=1, evidence={0: evidence})


def test_opt_in_grades_not_reached_beside_stepping_narration(tmp_path, monkeypatch):
    corner = _corner(tmp_path, monkeypatch, [_opted_in()], _missing_log())
    (meas,) = corner["measurements"]
    assert meas["status"] == "not_reached" and meas["value"] is None
    assert meas["step"] == "edge"
    assert corner["status"] != "error"
    codes = _codes(corner)
    assert ("info", "measurement_not_reached") in codes
    assert not any(code == "measurement" for _, code in codes)
    assert ("warning", "singular_matrix") in codes
    assert ("warning", "nonconvergence") in codes
    assert not any(severity == "error" for severity, _ in codes)


def test_only_nonconvergence_narration_is_forgiven_too(tmp_path, monkeypatch):
    log = _missing_log(narration="Dynamic gmin stepping failed\n")
    corner = _corner(tmp_path, monkeypatch, [_opted_in()], log)
    assert corner["measurements"][0]["status"] == "not_reached"
    assert not any(severity == "error" for severity, _ in _codes(corner))


def test_abort_trailer_still_blocks_the_opt_in(tmp_path, monkeypatch):
    log = _missing_log() + _ABORT_TRAILER
    corner = _corner(tmp_path, monkeypatch, [_opted_in()], log)
    assert corner["measurements"][0]["status"] == "error"
    assert corner["status"] == "error"
    assert ("error", "analysis_aborted") in _codes(corner)


def test_without_the_opt_in_the_corner_still_errors(tmp_path, monkeypatch):
    plain = {"name": "missing", "spice": _MISSING_CARD}
    corner = _corner(tmp_path, monkeypatch, [plain], _missing_log())
    assert corner["measurements"][0]["status"] == "error"
    assert corner["status"] == "error"


def test_a_second_valueless_measurement_keeps_the_narration_error(
    tmp_path, monkeypatch
):
    log = _missing_log(_confirmed_miss("other", _OTHER_CMD))
    other = {"name": "other", "spice": _OTHER_CARD}
    corner = _corner(tmp_path, monkeypatch, [_opted_in(), other], log)
    statuses = {m["name"]: m["status"] for m in corner["measurements"]}
    assert statuses == {"edge.missing": "not_reached", "edge.other": "error"}
    assert corner["status"] == "error"
    assert ("error", "singular_matrix") in _codes(corner)
    assert ("error", "nonconvergence") in _codes(corner)


def test_a_non_stepping_error_diagnostic_still_blocks(tmp_path, monkeypatch):
    log = "Error: Timestep too small; run aborted\n" + _missing_log()
    corner = _corner(tmp_path, monkeypatch, [_opted_in()], log)
    assert ("error", "timestep_too_small") in _codes(corner)
    assert corner["measurements"][0]["status"] == "error"
    assert corner["status"] == "error"


def test_a_hit_beside_the_miss_is_graded_normally(tmp_path, monkeypatch):
    hit = {"name": "hit", "spice": _HIT_CARD, "unit": "s"}
    log = _NARRATION + _region_log(
        steps=1,
        evidence={0: _confirmed_miss("missing", _MISSING_CMD)},
        values={"edge__hit": 1.08e-09},
    )
    corner = _corner(tmp_path, monkeypatch, [hit, _opted_in()], log)
    statuses = {m["name"]: m["status"] for m in corner["measurements"]}
    assert statuses == {"edge.hit": "pass", "edge.missing": "not_reached"}
    assert corner["status"] == "pass"
    assert not any(severity == "error" for severity, _ in _codes(corner))
