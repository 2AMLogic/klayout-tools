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


_PDK_MSG = "no supported-layout PDK install providing variant 'gf180mcuC'"


def test_missing_pdk_variant_gains_stage_model_inputs_hint_once():
    from klayout_tools.sim_batch_hints import hint_existing_fatal

    diags = [{"code": "runner_error", "message": _PDK_MSG, "severity": "error"}]
    assert hint_existing_fatal(diags) is False
    assert "options.stage_model_inputs" in diags[0]["message"]
    assert diags[0]["code"] == "runner_error"
    hint_existing_fatal(diags)
    assert diags[0]["message"].count("options.stage_model_inputs") == 1


def test_unrelated_fatal_gets_no_pdk_hint():
    from klayout_tools.sim_batch_hints import hint_existing_fatal

    diags = [{"code": "ngspice_fatal", "message": "Undefined parameter [w]"}]
    assert hint_existing_fatal(diags) is True
    assert "stage_model_inputs" not in diags[0]["message"]
