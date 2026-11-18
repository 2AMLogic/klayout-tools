"""Runner-image gap hint on off-host ``expr()`` ngspice_fatal (issue #4891)."""

from __future__ import annotations

import pytest

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


_MODELNAME_LOG = (
    "Circuit: * deck\n"
    "Error on line 570 : m.x1.m0 d g s b nfet_03v3 w=1.28e-4 l=2.8e-7\n"
    "Error: could not find a valid modelname\n"
)


def test_modelname_fatal_names_instance_and_model_from_logged_card(tmp_path):
    (tmp_path / "ngspice.log").write_text(_MODELNAME_LOG)
    _, found = sb._engine_log_note(str(tmp_path))
    msg = found["message"]
    assert "instance 'm.x1.m0' model 'nfet_03v3'" in msg
    assert "w=1.28e-4" in msg and "expanded deck" in msg


def test_modelname_attribution_is_idempotent_and_never_raises():
    from klayout_tools.sim_batch_hints import attribute_modelname

    diag = {
        "code": "model_bin_range",
        "message": "Error: could not find a valid modelname",
    }
    attribute_modelname(diag, _MODELNAME_LOG)
    once = diag["message"]
    attribute_modelname(diag, _MODELNAME_LOG)
    assert diag["message"] == once


def test_modelname_without_quoted_card_falls_back_unchanged():
    from klayout_tools.sim_batch_hints import attribute_modelname

    raw = "Error: could not find a valid modelname"
    diag = {"code": "model_bin_range", "message": raw}
    attribute_modelname(diag, raw + "\n")
    assert diag["message"] == raw
    # Line N present but not a card we can read (out of range / odd text).
    attribute_modelname(diag, "Error on line 99999 : \n" + raw)
    assert diag["message"] == raw


# --------------------------------------------------------------------------- #
# Worker-stamp attribution of the modelname abort (issue #5593)
# --------------------------------------------------------------------------- #

_STAMP_TEXT = "open_pdks c6d73a35f524070e85faff4a6a9eef49553ebc2b\n"
_LEGACY_TAIL = "compare this model's w/l against the library's bin cards"


def _worker_block(
    *,
    capture="ok",
    variant="gf180mcuD",
    stamp_sha256="c" * 64,
    stamp_text=_STAMP_TEXT,
    models_staged=False,
    comparison=None,
):
    """A processed ``worker_environment`` block as ``_read_worker_environment``
    builds it (only the fields the attribution reads)."""
    if comparison is None:
        comparison = {"status": "mismatch", "warning": "stamps differ"}
    return {
        "capture": capture,
        "pdk": {
            "variant": variant,
            "stamp_scheme": "sources-sha256",
            "stamp_sha256": stamp_sha256,
            "stamp_text": stamp_text,
            "models_staged": models_staged,
            "stamp_scope": "image" if models_staged else "executed",
        },
        "pdk_stamp_comparison": comparison,
    }


def _attributed_message(worker_environment):
    from klayout_tools.sim_batch_hints import attribute_modelname

    diag = {
        "code": "model_bin_range",
        "message": "Error: could not find a valid modelname",
    }
    attribute_modelname(diag, _MODELNAME_LOG, worker_environment)
    return diag["message"]


def test_worker_block_names_fleet_stamp_comparison_and_remedy():
    msg = _attributed_message(_worker_block())
    assert "the fleet resolved PDK gf180mcuD" in msg
    assert "c" * 64 in msg
    assert "revision c6d73a35f524070e85faff4a6a9eef49553ebc2b" in msg
    assert "pdk_stamp_comparison: mismatch" in msg
    assert "options.stage_model_inputs: true" in msg
    assert _LEGACY_TAIL not in msg


@pytest.mark.parametrize(
    "comparison, expected",
    [
        ({"status": "match"}, "pdk_stamp_comparison: match"),
        ({"status": "mismatch", "warning": "w"}, "pdk_stamp_comparison: mismatch"),
        (
            {"status": "not_comparable", "reason": "worker_stamp_unavailable"},
            "pdk_stamp_comparison: not_comparable (worker_stamp_unavailable)",
        ),
        (
            {"status": "not_comparable", "reason": "client_stamp_unavailable"},
            "pdk_stamp_comparison: not_comparable (client_stamp_unavailable)",
        ),
    ],
)
def test_worker_block_names_every_comparison_status(comparison, expected):
    assert expected in _attributed_message(_worker_block(comparison=comparison))


@pytest.mark.parametrize(
    "block",
    [
        None,
        {"capture": "absent", "pdk": None, "pdk_stamp_comparison": {}},
        {"capture": "malformed", "pdk": None, "pdk_stamp_comparison": {}},
        {"capture": "ok", "pdk": None, "pdk_stamp_comparison": {}},
        {
            "capture": "ok",
            "pdk": {"variant": None, "stamp_sha256": "c" * 64},
            "pdk_stamp_comparison": {"status": "mismatch"},
        },
    ],
    ids=["no-block", "absent", "malformed", "no-pdk", "no-variant"],
)
def test_without_usable_worker_block_message_is_byte_for_byte_legacy(block):
    assert _attributed_message(block) == _attributed_message(None)
    assert _LEGACY_TAIL in _attributed_message(block)


def test_worker_block_without_readable_stamp_names_the_gap():
    msg = _attributed_message(
        _worker_block(
            stamp_sha256=None,
            comparison={
                "status": "not_comparable",
                "reason": "worker_stamp_unavailable",
            },
        )
    )
    assert "no readable stamp" in msg
    assert "pdk_stamp_comparison: not_comparable (worker_stamp_unavailable)" in msg
    assert "options.stage_model_inputs: true" in msg


def test_worker_block_without_parseable_revision_token_omits_it():
    msg = _attributed_message(_worker_block(stamp_text="store:1.2.3\n"))
    assert "c" * 64 in msg
    assert "revision" not in msg


def test_staged_run_is_not_told_to_stage_again():
    block = _worker_block(
        models_staged=True,
        comparison={"status": "match", "basis": "staged_model_inputs"},
    )
    msg = _attributed_message(block)
    assert "options.stage_model_inputs" not in msg
    assert "staged model inputs" in msg
    assert "worker_environment.model_inputs" in msg
    assert "pdk_stamp_comparison: match [basis: staged_model_inputs]" in msg


def test_worker_attribution_is_idempotent():
    from klayout_tools.sim_batch_hints import attribute_modelname

    diag = {
        "code": "model_bin_range",
        "message": "Error: could not find a valid modelname",
    }
    attribute_modelname(diag, _MODELNAME_LOG, _worker_block())
    once = diag["message"]
    attribute_modelname(diag, _MODELNAME_LOG, _worker_block())
    assert diag["message"] == once


# --------------------------------------------------------------------------- #
# End to end through one collected batch job (issue #5593); the fixtures come
# from ``test_sim_batch`` (its own file sits at its size-ratchet ceiling).
# --------------------------------------------------------------------------- #

_MODELNAME_ABORT_LOG = (
    "Circuit: * deck\n"
    "Error on line 570 : m.x1.m0 d g s b nfet_03v3 w=1.28e-4 l=2.8e-7\n"
    "Error: could not find a valid modelname\n"
)


def _modelname_job_runner(monkeypatch, outputs):
    from helpers.subprocess_fakes import fake_completed
    from test_sim_batch import (
        _FakeRunner,
        _install_fake_batch_transport,
        _status,
    )

    runner = _FakeRunner()
    runner.default = fake_completed(_status("failed", exit_code="4"))
    runner.outputs = outputs
    _install_fake_batch_transport(monkeypatch, runner=runner)
    return runner


def _run_modelname_job(tmp_path, monkeypatch, runner, request):
    from test_sim_batch import (
        _config,
        _corner_points,
        _measurements_spec,
        _write_body,
    )

    return sb._run_batch_job(
        config=_config(tmp_path),
        job_id="job-1",
        corner_points=_corner_points(1),
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=30.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        request=request,
        measurements_spec=_measurements_spec(),
        runner=runner,
        sleep=runner.sleep,
    )


def test_modelname_abort_with_worker_capture_names_fleet_pdk(tmp_path, monkeypatch):
    """A usable collected worker capture makes the surfaced modelname
    diagnostic attribute the abort to the fleet's observed PDK stamp, the
    stamp comparison, and the staging remedy -- not the submitter's own bin
    cards."""
    from test_sim_batch import _capture, _errored_report_without_include_code

    remote_log = "/var/tmp/eda-job-1/outputs/artifacts/tt_1.800_27/ngspice.log"
    runner = _modelname_job_runner(
        monkeypatch,
        {
            "report.json": _errored_report_without_include_code(remote_log),
            "artifacts/tt_1.800_27/ngspice.log": _MODELNAME_ABORT_LOG,
            "worker_environment.json": _capture(
                pdk={
                    "variant": "gf180mcuD",
                    "root": "/opt/pdk",
                    "stamp_scheme": "sources-sha256",
                    "stamp_sha256": "b" * 64,
                    "stamp_text": "open_pdks f6eeac7bf3e04b1c\n",
                    "models_staged": False,
                }
            ),
        },
    )

    corners, _, environment = _run_modelname_job(
        tmp_path,
        monkeypatch,
        runner,
        {"netlist": "body.spice", "models": {"pdk": "gf180mcuD"}},
    )

    assert environment["worker_environment"]["capture"] == "ok"
    msg = corners[0]["diagnostics"][1]["message"]
    assert "the fleet resolved PDK gf180mcuD" in msg
    assert "b" * 64 in msg
    assert "revision f6eeac7bf3e04b1c" in msg
    assert "pdk_stamp_comparison:" in msg
    assert "options.stage_model_inputs: true" in msg
    assert "library's bin cards" not in msg


def test_modelname_abort_without_worker_capture_keeps_legacy_advice(
    tmp_path, monkeypatch
):
    """No collected capture (legacy worker) keeps the pre-#5593 diagnostic
    byte for byte."""
    from test_sim_batch import _errored_report_without_include_code

    remote_log = "/var/tmp/eda-job-1/outputs/artifacts/tt_1.800_27/ngspice.log"
    runner = _modelname_job_runner(
        monkeypatch,
        {
            "report.json": _errored_report_without_include_code(remote_log),
            "artifacts/tt_1.800_27/ngspice.log": _MODELNAME_ABORT_LOG,
        },
    )

    corners, _, _ = _run_modelname_job(
        tmp_path, monkeypatch, runner, {"netlist": "body.spice"}
    )

    msg = corners[0]["diagnostics"][1]["message"]
    assert msg.endswith("compare this model's w/l against the library's bin cards")


def test_errored_batch_text_output_prints_pdk_stamp_line(tmp_path, monkeypatch, capsys):
    """`klt sim` text output prints the `pdk_stamp:` line on an *errored*
    batch run (no analysis started), not only on a completed one -- the text
    renderer runs before the exit code is derived from the status."""
    import argparse
    import json

    from klayout_tools.cli import sim_cmd
    from test_sim_batch import _errored_report_without_include_code

    report = json.loads(_errored_report_without_include_code())
    report["netlist"] = {"path": "body.spice", "scope": "repo"}
    report["inconclusive"] = 0
    report["environment"].update(
        models_lib={"path": None, "scope": "external"},
        remote={
            "worker_environment": {
                "capture": "ok",
                "pdk": {"variant": "gf180mcuD", "models_staged": False},
                "pdk_stamp_comparison": {
                    "status": "not_comparable",
                    "reason": "client_stamp_unavailable",
                },
            }
        },
    )
    monkeypatch.setattr(sim_cmd, "run_sim", lambda *a, **k: report)
    args = argparse.Namespace(
        request=str(tmp_path / "request.json"),
        outdir=None,
        backend=None,
        max_workers=None,
        hosts=None,
        budget_s=None,
        resume=None,
        plot=None,
        fail_fast_probe=None,
        fail_on_diagnostic=None,
        max_units_per_job=None,
        max_concurrent_jobs=None,
        op_lint=False,
        op_lint_corner=None,
        format="text",
    )

    assert sim_cmd.run(args) == sim_cmd.EXIT_CORNER_ERRORED
    out = capsys.readouterr().out
    assert "pdk_stamp: not_comparable (client_stamp_unavailable)" in out
    assert "models_staged: false" in out


# --------------------------------------------------------------------------- #
# Defensive branches the CI coverage floor caught uncovered (#3312 ratchet).
# --------------------------------------------------------------------------- #


def test_comparison_sentence_is_empty_without_a_usable_comparison():
    from klayout_tools.sim_batch_hints import _worker_comparison_sentence

    assert _worker_comparison_sentence({}) == ""
    assert _worker_comparison_sentence({"pdk_stamp_comparison": {}}) == ""
    assert _worker_comparison_sentence({"pdk_stamp_comparison": "mismatch"}) == ""


def test_worker_block_without_comparison_omits_the_line():
    msg = _attributed_message(_worker_block(comparison={}))
    assert "pdk_stamp_comparison" not in msg
    assert "the fleet resolved PDK gf180mcuD" in msg


def test_modelname_non_instance_card_names_the_card(tmp_path):
    from klayout_tools.sim_batch_hints import attribute_modelname

    log = (
        "Circuit: * deck\n"
        "Error on line 12 : c.x1 a b 1.0e-12 w=1.0e-5\n"
        "Error: could not find a valid modelname\n"
    )
    diag = {"code": "model_bin_range", "message": "could not find a valid modelname"}
    attribute_modelname(diag, log)
    assert "'c.x1' (w=1.0e-5)" in diag["message"]


def test_modelname_instance_card_without_params_omits_the_parenthetical():
    from klayout_tools.sim_batch_hints import attribute_modelname

    log = (
        "Circuit: * deck\n"
        "Error on line 570 : m.x1.m0 d g s b nfet_03v3\n"
        "Error: could not find a valid modelname\n"
    )
    diag = {"code": "model_bin_range", "message": "could not find a valid modelname"}
    attribute_modelname(diag, log)
    assert "instance 'm.x1.m0' model 'nfet_03v3'" in diag["message"]
    assert "(" not in diag["message"].split("ngspice card at expanded-deck line")[1]


def test_modelname_message_with_card_only_log_keeps_the_card():
    from klayout_tools.sim_batch_hints import attribute_modelname

    log = "Error on line 570 : m.x1.m0 d g s b nfet_03v3 w=1.28e-4\n"
    diag = {"code": "model_bin_range", "message": "could not find a valid modelname"}
    attribute_modelname(diag, log)
    assert "instance 'm.x1.m0' model 'nfet_03v3'" in diag["message"]


def test_read_log_head_unreadable_path_is_empty(tmp_path):
    from klayout_tools.sim_batch_hints import _read_log_head

    assert _read_log_head(str(tmp_path / "absent" / "ngspice.log")) == ""
    assert _read_log_head(str(tmp_path)) == ""  # a directory: IsADirectoryError
