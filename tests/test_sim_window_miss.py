"""Issue #5415: an "out of interval" measurement miss is a window miss.

ngspice reports ``out of interval`` when a ``.meas`` ``WHEN``/``TRIG``/``TARG``
search never found its crossing in the analysis interval. The run completed --
the target was simply never reached -- so it is never an ``ngspice_fatal``
deck abort, and the ``measurement`` diagnostic says so and names the opt-ins
that grade the miss instead of erroring.
"""

from __future__ import annotations

from klayout_tools import sim

_OUT_OF_INTERVAL_LOG = (
    "Doing analysis at TEMP = 27.000000 and TNOM = 27.000000\n"
    "Error: measure  tr1  when(WHEN) : out of interval\n"
    " .meas tran tr1 when v(out)=0.5 rise=5 failed!\n"
)


def test_out_of_interval_alone_is_not_ngspice_fatal():
    codes = [d["code"] for d in sim._classify_diagnostics(_OUT_OF_INTERVAL_LOG)]
    assert "ngspice_fatal" not in codes


def test_out_of_interval_with_real_abort_stays_ngspice_fatal():
    log = _OUT_OF_INTERVAL_LOG + "Simulation interrupted due to error!\n"
    codes = [d["code"] for d in sim._classify_diagnostics(log)]
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
