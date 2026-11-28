"""Issue #5415: an "out of interval" measurement miss is a window miss.

ngspice reports ``out of interval`` when a ``.meas`` ``WHEN``/``TRIG``/``TARG``
search never found its crossing in the analysis interval. The run completed --
the target was simply never reached -- so it is never an ``ngspice_fatal``
deck abort, and the ``measurement`` diagnostic says so and names the opt-ins
that grade the miss instead of erroring.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim

_OUT_OF_INTERVAL_LOG = (
    "Doing analysis at TEMP = 27.000000 and TNOM = 27.000000\n"
    "Error: measure  tr1  when(WHEN) : out of interval\n"
    " .meas tran tr1 when v(out)=0.5 rise=5 failed!\n"
)


def test_out_of_interval_alone_is_not_ngspice_fatal():
    codes = [d["code"] for d in sim.classify_diagnostics(_OUT_OF_INTERVAL_LOG)]
    assert "ngspice_fatal" not in codes


def test_out_of_interval_with_real_abort_stays_ngspice_fatal():
    log = _OUT_OF_INTERVAL_LOG + "Simulation interrupted due to error!\n"
    codes = [d["code"] for d in sim.classify_diagnostics(log)]
    assert "ngspice_fatal" in codes


def test_no_value_out_of_interval_message_names_opt_ins():
    spec = {"name": "tr1", "spice": "tran tr1 when v(out)=0.5 rise=5"}
    result, diag, attributed = sim._no_value_measurement(
        spec, {"tr1": "out of interval"}, save_mode="all"
    )
    assert result["status"] == "error"
    assert diag["code"] == "measurement" and diag["severity"] == "error"
    assert "ngspice reported: out of interval" in diag["message"]
    assert "on_unreached" in diag["message"] and "optional" in diag["message"]
    assert attributed is True


def test_no_value_other_reason_has_no_window_hint():
    spec = {"name": "bad", "spice": "tran bad when v(out)="}
    _, diag, _ = sim._no_value_measurement(spec, {"bad": "bad syntax"}, save_mode="all")
    assert "on_unreached" not in diag["message"]


# --- stepping narration beside an opted-in window miss (issue #7906) ----------

#: The real ngspice-47 log of a cap-coupled (singular DC matrix) node whose
#: ``.meas`` target is never crossed: the stepping narration, a completed
#: transient, then the window miss. No ``simulation(s) aborted`` trailer.
_STEPPING_MISS_LOG = (
    "Warning: singular matrix: check node mid\n"
    "Dynamic gmin stepping failed\n"
    "True gmin stepping failed\n"
    "Transient op finished successfully\n"
    "Error: measure tcross when(WHEN) : out of interval\n"
    " .meas tran tcross when v(mid)=5 cross=1 failed!\n"
)
_ABORT_TRAILER = "run simulation(s) aborted\n"
_TCROSS = {"name": "tcross", "spice": ".meas tran tcross when v(mid)=5 cross=1"}
_OTHER = {"name": "other", "spice": ".meas tran other when v(mid)=7 cross=1"}


def _run(tmp_path: Path, monkeypatch, log_text: str, measurements: list[dict]):
    (tmp_path / "body.spice").write_text("V1 in 0 1\nC1 in mid 1p\nC2 mid 0 1p\n")
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis": {"kind": "tran", "args": "0.1n 20n"},
                "measurements": measurements,
            }
        )
    )

    def fake_run(cmd, capture_output, text, timeout, cwd=None, env=None):
        if "--version" in cmd:
            return fake_completed("** ngspice-47\n")
        with open(cmd[cmd.index("-o") + 1], "w", encoding="utf-8") as handle:
            handle.write(log_text)
        return fake_completed("** ngspice-47\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    return sim.run_sim(str(request))["corners"][0]


def _codes(corner: dict) -> list[tuple[str, str]]:
    return [(d["severity"], d["code"]) for d in corner["diagnostics"]]


@pytest.mark.usefixtures("real_build_identity_git", "bare_name_ngspice_resolution")
class TestStepping:
    def test_opt_in_grades_not_reached_beside_stepping_narration(
        self, tmp_path, monkeypatch
    ):
        corner = _run(
            tmp_path,
            monkeypatch,
            _STEPPING_MISS_LOG,
            [{**_TCROSS, "on_unreached": "null"}],
        )
        (meas,) = corner["measurements"]
        assert meas["status"] == "not_reached" and meas["value"] is None
        assert corner["status"] != "error"
        codes = _codes(corner)
        assert ("info", "measurement_not_reached") in codes
        assert not any(code == "measurement" for _, code in codes)
        # The recovery downgrade is unchanged: it now sees no errored measurement.
        assert ("warning", "singular_matrix") in codes
        assert ("warning", "nonconvergence") in codes
        assert not any(sev == "error" for sev, _ in codes)

    def test_only_nonconvergence_narration_is_forgiven_too(self, tmp_path, monkeypatch):
        log = _STEPPING_MISS_LOG.replace(
            "Warning: singular matrix: check node mid\n", ""
        )
        corner = _run(tmp_path, monkeypatch, log, [{**_TCROSS, "on_unreached": "null"}])
        assert corner["measurements"][0]["status"] == "not_reached"
        assert not any(sev == "error" for sev, _ in _codes(corner))

    def test_abort_trailer_still_blocks_the_opt_in(self, tmp_path, monkeypatch):
        corner = _run(
            tmp_path,
            monkeypatch,
            _STEPPING_MISS_LOG + _ABORT_TRAILER,
            [{**_TCROSS, "on_unreached": "null"}],
        )
        assert corner["measurements"][0]["status"] == "error"
        assert corner["status"] == "error"
        assert ("error", "analysis_aborted") in _codes(corner)

    def test_without_the_opt_in_the_corner_still_errors(self, tmp_path, monkeypatch):
        corner = _run(tmp_path, monkeypatch, _STEPPING_MISS_LOG, [dict(_TCROSS)])
        assert corner["measurements"][0]["status"] == "error"
        assert corner["status"] == "error"

    def test_a_second_valueless_measurement_keeps_the_narration_error(
        self, tmp_path, monkeypatch
    ):
        log = _STEPPING_MISS_LOG + (
            "Error: measure other when(WHEN) : out of interval\n"
            " .meas tran other when v(mid)=7 cross=1 failed!\n"
        )
        corner = _run(
            tmp_path,
            monkeypatch,
            log,
            [{**_TCROSS, "on_unreached": "null"}, dict(_OTHER)],
        )
        by_name = {m["name"]: m["status"] for m in corner["measurements"]}
        assert by_name == {"tcross": "not_reached", "other": "error"}
        assert corner["status"] == "error"
        assert ("error", "singular_matrix") in _codes(corner)
        assert ("error", "nonconvergence") in _codes(corner)

    def test_a_non_stepping_error_diagnostic_still_blocks(self, tmp_path, monkeypatch):
        log = "Error: Timestep too small; run aborted\n" + _STEPPING_MISS_LOG
        corner = _run(tmp_path, monkeypatch, log, [{**_TCROSS, "on_unreached": "null"}])
        assert ("error", "timestep_too_small") in _codes(corner)
        assert corner["measurements"][0]["status"] == "error"
        assert corner["status"] == "error"
