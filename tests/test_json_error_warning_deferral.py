"""Pre-error stderr warnings defer into the JSON error envelope (issue #7203).

Every ``klt`` warning emitter that can fire ahead of an application error
under ``--format json`` routes through ``klayout_tools._warn.warn`` (the
issue #6935 deferral): ``sim``, ``sim_batch``, ``sim_batch_warn``,
``sim_batch_features``, ``sim_environment_report``, ``extract_batch``,
``cli.netlist_cmd`` and ``erc``/``_devices``. These tests pin both halves
of that contract on the real emitter functions: a warning ahead of a JSON
error is dropped so ``json.loads(stderr)`` parses the whole stream as one
envelope, and the same warning keeps its byte-identical line on every other
path (a report-producing run, text mode, an unarmed library call).
"""

from __future__ import annotations

import json

import pytest

from klayout_tools import (
    _warn,
    erc,
    extract_batch,
    sim,
    sim_batch,
    sim_batch_features,
    sim_batch_warn,
    sim_transient_noise,
)
from klayout_tools import sim_environment_report as sim_env_report
from klayout_tools._devices import _warn_device_body_missed_role
from klayout_tools.cli import netlist_cmd
from klayout_tools.cli.output import emit_error, emit_success
from klayout_tools.erc_antenna_names import match_antenna_layers


def _fire_sim_meas_at_lint(tmp_path, monkeypatch) -> None:
    sim._attach_meas_lint(
        [{"diagnostics": []}],
        {"kind": "dc", "args": "V1 0 1 0.1"},
        [{"name": "vmid", "spice": "meas dc vmid find v(out) at=5"}],
    )


def _fire_sim_worker_cap_notice(tmp_path, monkeypatch) -> None:
    sim._reset_worker_cap_notices()
    sim._worker_cap_notice(
        "klt: notice: max_workers=16 exceeds this host's "
        f"{sim.MAX_WORKERS_ENV}=2 cap; clamping the local-parallel worker "
        "pool to 2."
    )


def _fire_sim_unseeded_noise(tmp_path, monkeypatch) -> None:
    netlist = tmp_path / "noise.cir"
    netlist.write_text("Vnoise out 0 trnoise(1 1n 0 0 0 0)\n", encoding="utf-8")
    sim_transient_noise.with_unseeded_noise(
        {}, str(netlist), request={}, repo_root=None
    )


def _fire_sim_batch_receipt(tmp_path, monkeypatch) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("in the way")
    sim_batch._write_batch_receipt(str(blocker / "sub" / "receipt.json"), {})


def _fire_sim_serial_bound(tmp_path, monkeypatch) -> None:
    sim_batch_warn.SERIAL_BOUND_WARNED.clear()
    sim_batch_warn.warn_over_serial_bound_once(4, 10, 8, {"max_job_seconds": 14400.0})


def _fire_sim_unverified_features(tmp_path, monkeypatch) -> None:
    sim_batch_features.warn_unverified_features(["analysis_steps"], "0.8.0")


def _fire_extract_ceiling(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        sim_batch,
        "_assess_job_ceiling",
        lambda **kw: {
            "exceeds_ceiling": True,
            "max_job_seconds": 60.0,
            "job_bound_s": 120.0,
            "overhead_s": 10.0,
            "max_job_seconds_source": "the test",
            "check": "warn",
        },
    )
    extract_batch._check_ceiling(None, 110.0)


def _fire_netlist_warning(tmp_path, monkeypatch) -> None:
    netlist_cmd._warn("ambient ngspice init file ignored")


def _fire_devices_body_layer(tmp_path, monkeypatch) -> None:
    _warn_device_body_missed_role("klt erc", "nfet_lvt", "licon.dg", "source")


def _fire_erc_label_layer(tmp_path, monkeypatch) -> None:
    erc._warn_label_layer_empty("klt erc", "poly", "met1.li")


def _fire_erc_antenna_names(tmp_path, monkeypatch) -> None:
    match_antenna_layers({"met1": (400.0, "um")}, ["li1", "licon"], "sky130")


def _fire_device_multiplier(tmp_path, monkeypatch) -> None:
    netlist = tmp_path / "netlist.cir"
    netlist.write_text("* synthetic netlist\n")
    monkeypatch.setattr(
        sim_env_report, "_multiplier_sources", lambda *a, **k: (set(), set())
    )
    monkeypatch.setattr(
        sim_env_report.device_multiplier_lint, "scan_closure", lambda *a: ([], {})
    )
    monkeypatch.setattr(
        sim_env_report.device_multiplier_lint,
        "instance_report",
        lambda *a: {"findings": [{"instance": "X1"}]},
    )
    sim_env_report.device_multiplier_environment(
        str(netlist), request={}, repo_root=None
    )


#: One representative real emitter per routed module (issue #7203 scope).
#: Each fires exactly one warning line; EXPECTED is the verbatim wording the
#: unarmed (library/text) path must keep producing.
EMITTERS = {
    "sim_meas_at_lint": _fire_sim_meas_at_lint,
    "sim_worker_cap_notice": _fire_sim_worker_cap_notice,
    "sim_unseeded_noise": _fire_sim_unseeded_noise,
    "sim_batch_receipt": _fire_sim_batch_receipt,
    "sim_serial_bound": _fire_sim_serial_bound,
    "sim_unverified_features": _fire_sim_unverified_features,
    "extract_ceiling": _fire_extract_ceiling,
    "netlist_warning": _fire_netlist_warning,
    "devices_body_layer": _fire_devices_body_layer,
    "erc_label_layer": _fire_erc_label_layer,
    "erc_antenna_names": _fire_erc_antenna_names,
    "device_multiplier": _fire_device_multiplier,
}

EXPECTED = {
    "sim_meas_at_lint": (
        "warning: measurement 'vmid' will likely report 'out of interval': "
        "AT=5 lies outside the dc sweep [0, 1]"
    ),
    "sim_worker_cap_notice": (
        "klt: notice: max_workers=16 exceeds this host's "
        f"{sim.MAX_WORKERS_ENV}=2 cap; clamping the local-parallel worker "
        "pool to 2."
    ),
    "sim_unseeded_noise": f"warning: {sim_transient_noise._UNSEEDED_NOISE_NOTE}",
    "sim_batch_receipt": "klt sim: warning: could not write batch receipt ",
    "sim_serial_bound": (
        "klt sim: warning: request.batch.max_units_per_job 4 leaves jobs of 10"
    ),
    "sim_unverified_features": (
        "klt sim: warning: the client could not verify support for "
        "analysis_steps before launch"
    ),
    "extract_ceiling": (
        "klt extract: warning: extract job bound 120s (--timeout-s 110 + 10s "
        "overhead) exceeds the fleet's whole-job ceiling of 60s "
        "(max_job_seconds from the test)"
    ),
    "netlist_warning": "klt netlist: warning: ambient ngspice init file ignored",
    "devices_body_layer": (
        "klt erc: warning: devices[] entry 'nfet_lvt' declares body_layer "
        "licon.dg on role 'source'"
    ),
    "erc_label_layer": (
        "klt erc: warning: stackup entry 'poly' declares label_layer met1.li"
    ),
    "erc_antenna_names": (
        "klt erc: warning: no non-gate stackup layer (li1, licon) matches "
        "the sky130 antenna table (met1)"
    ),
    "device_multiplier": ("warning: 1 device-multiplier finding(s) for this netlist"),
}


@pytest.fixture(autouse=True)
def _disarm_warn_deferral():
    """The deferral never leaks across tests, whatever the test did to it."""
    yield
    _warn.discard()


@pytest.mark.parametrize("emitter_name", sorted(EMITTERS))
def test_warning_dropped_before_json_error_stderr_parses_whole(
    tmp_path, monkeypatch, capsys, emitter_name
):
    """Issue #7203: with the deferral armed (a ``--format json`` invocation),
    any routed warning ahead of an application error is dropped with it, so
    the entire stderr stream is exactly one JSON error envelope and stdout
    stays empty."""
    _warn.arm()
    EMITTERS[emitter_name](tmp_path, monkeypatch)
    assert emit_error("sim", "boom", "json") == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert EXPECTED[emitter_name] not in captured.err
    assert "warning" not in captured.err
    envelope = json.loads(captured.err)
    assert envelope["schema_version"] == 1
    assert envelope["error"]["command"] == "sim"
    assert envelope["error"]["message"] == "boom"


def test_two_warnings_one_json_error_then_report_flush(capsys):
    """The parent #7199 reproduction at the shared output boundary: two
    distinct deferred warnings ahead of one JSON error are dropped with it,
    and a report-producing invocation in the same process afterwards still
    delivers its own warnings, in order, ahead of the report."""
    _warn.arm()
    _warn.warn("klt: warning: first unrelated advisory")
    _warn.warn("klt: warning: second unrelated advisory")
    assert emit_error("sim", "boom", "json") == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["message"] == "boom"

    _warn.arm()
    _warn.warn("klt sim: warning: first flushes on success")
    _warn.warn("klt sim: warning: second flushes on success")
    emit_success({"schema_version": 1, "status": "ok"}, "json", lambda payload: None)

    captured = capsys.readouterr()
    err_lines = captured.err.splitlines()
    assert err_lines == [
        "klt sim: warning: first flushes on success",
        "klt sim: warning: second flushes on success",
    ]
    assert json.loads(captured.out)["status"] == "ok"


@pytest.mark.parametrize("emitter_name", sorted(EMITTERS))
def test_warning_flushed_ahead_of_json_report(
    tmp_path, monkeypatch, capsys, emitter_name
):
    """Issue #7203: on a report-producing ``--format json`` run the deferral
    flushes, so the routed warning still reaches stderr verbatim while
    stdout parses as the report."""
    _warn.arm()
    EMITTERS[emitter_name](tmp_path, monkeypatch)
    emit_success({"schema_version": 1, "status": "ok"}, "json", lambda payload: None)

    captured = capsys.readouterr()
    assert EXPECTED[emitter_name] in captured.err
    assert json.loads(captured.out)["status"] == "ok"


@pytest.mark.parametrize("emitter_name", sorted(EMITTERS))
def test_warning_verbatim_when_deferral_unarmed(
    tmp_path, monkeypatch, capsys, emitter_name
):
    """Issue #7203: text invocations never arm the deferral, and neither do
    direct library callers -- every routed emitter keeps printing its exact
    line to stderr, nothing on stdout."""
    EMITTERS[emitter_name](tmp_path, monkeypatch)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert EXPECTED[emitter_name] in captured.err


def test_warning_flushed_ahead_of_text_error_line(capsys):
    """The text-mode boundary: a deferred warning is flushed ahead of the
    plain-text error line (text is not the contract, but the advisory stays
    visible), matching the issue #6935 drc behavior."""
    _warn.arm()
    _warn.warn("klt: warning: advisory stays visible in text mode")
    assert emit_error("sim", "boom", "text") == 1

    err = capsys.readouterr().err
    assert err.index("klt: warning:") < err.index("klt sim: boom")
