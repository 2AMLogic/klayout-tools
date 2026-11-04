"""Runner-image gap hint on off-host ``expr()`` ngspice_fatal (issue #4891)."""

from __future__ import annotations

from klayout_tools import sim_batch as sb

_EXPR_LOG = (
    "Error in netlist line no. 12:\n"
    "Undefined parameter [expr]\n"
    "Expression err: expr((int((@nf+1)/2)*@w/@nf*0.29))\n"
)


def test_off_host_expr_failure_gains_runner_hint(tmp_path):
    (tmp_path / "ngspice.log").write_text(_EXPR_LOG)
    _, found = sb._engine_log_note(str(tmp_path))
    assert found["code"] == "ngspice_fatal"
    assert "runner_klt_version" in found["message"]
    assert "None" not in found["message"]


def test_local_classify_expr_failure_has_no_runner_hint():
    from klayout_tools.sim import _classify_diagnostics

    found = _classify_diagnostics(_EXPR_LOG)
    assert [d["code"] for d in found] == ["ngspice_fatal"]
    assert "runner_klt_version" not in found[0]["message"]


def test_other_undefined_parameter_gets_no_runner_hint(tmp_path):
    (tmp_path / "ngspice.log").write_text(
        "Error in netlist line no. 3:\nUndefined parameter [w]\n"
    )
    _, found = sb._engine_log_note(str(tmp_path))
    assert found["code"] == "ngspice_fatal"
    assert "runner_klt_version" not in found["message"]


def test_existing_fatal_diagnostic_is_hinted_once():
    corner = {
        "status": "error",
        "diagnostics": [
            {"code": "ngspice_fatal", "message": "Undefined parameter [expr]"}
        ],
    }
    from klayout_tools.sim_batch_hints import hint_existing_fatal

    assert hint_existing_fatal(corner["diagnostics"]) is True
    assert "runner_klt_version" in corner["diagnostics"][0]["message"]
    assert hint_existing_fatal(corner["diagnostics"]) is True  # idempotent
    assert corner["diagnostics"][0]["message"].count("runner_klt_version") == 1
