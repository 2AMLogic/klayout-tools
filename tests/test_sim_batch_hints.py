"""Runner-image gap hint on off-host ``expr()`` ngspice_fatal (issue #4891), and
the all-units-failed-to-converge run-level hint (issue #6720)."""

from __future__ import annotations

from pathlib import Path

import pytest

from klayout_tools import sim, sim_spiceinit
from klayout_tools import sim_batch as sb
from klayout_tools import sim_batch_hints as hints
from test_sim_batch import _capture, _fake_corner, _resume_request, _run

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
    from klayout_tools.sim import classify_diagnostics

    found = classify_diagnostics(_EXPR_LOG)
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


# --- all-units-failed-to-converge run-level hint (issue #6720) ----------------
#
# Synthetic reports only: no fleet, no ngspice grid.

_HINT_CODE = "batch_convergence_all_failed"
_SHA = "sha256:" + "ab" * 32
_CLIENT_SHA = "sha256:" + "cd" * 32


def _unit(corner_id, *codes, status="error"):
    return {
        "corner_id": corner_id,
        "status": status,
        "diagnostics": [
            {"code": code, "severity": "error", "message": "m"} for code in codes
        ],
    }


def _worker_entry(
    *,
    status="match",
    source="none",
    sha=None,
    omp=None,
    threads=None,
    capture="ok",
    **extra,
):
    """One ``environment.remote`` / ``fleet[]`` entry, as klt reports it."""
    return {
        "worker_environment": {
            "capture": capture,
            "engine_build_comparison": {"status": status},
            "ngspice_init": {"source": source, "lines": None, "sha256": sha},
            "omp_num_threads": omp,
            "threads_per_worker": threads,
        },
        **extra,
    }


def _attach(corners, remote=None, *, backend="batch", **environment):
    report = {"environment": {**environment}}
    if remote is not None:
        report["environment"]["remote"] = remote
    hints.attach_convergence_hint(report, corners, backend=backend)
    return report


def _hints_in(report):
    return [d for d in report.get("diagnostics", []) if d["code"] == _HINT_CODE]


@pytest.mark.parametrize("code", ["singular_matrix", "nonconvergence"])
def test_all_units_failing_to_converge_get_one_hint_with_worker_values(code):
    corners = [_unit("a", code), _unit("b", code)]
    remote = _worker_entry(
        status="mismatch", source="home", sha=_SHA, omp="1", threads=1
    )
    report = _attach(corners, remote, ngspice_options={"gmin": 1e-10})

    [hint] = _hints_in(report)
    assert hint["severity"] == "warning"
    assert hint["unit_count"] == 2
    message = hint["message"]
    # Every field the issue asks for is named, with the worker's value.
    for field in (
        "environment.remote.worker_environment",
        "engine_build_comparison.status",
        "ngspice_init.source",
        "ngspice_init.sha256",
        "omp_num_threads",
        "threads_per_worker=1",
        "environment.ngspice_options",
    ):
        assert field in message
    assert '"mismatch"' in message and '"home"' in message
    assert _SHA in message and '"1"' in message
    assert '{"gmin": 1e-10}' in message
    # The digest is comparable to a local run's, and the canary is named.
    assert "environment.ngspice_init.sha256" in message
    assert "--backend local" in message
    assert "keep_artifacts" in message
    assert "Transient convergence differs between local and batch" in message
    assert hint["workers"] == [
        {
            "fleet_index": None,
            "capture": "ok",
            "engine_build_comparison": "mismatch",
            "ngspice_init": {"source": "home", "sha256": _SHA},
            "omp_num_threads": "1",
            "threads_per_worker": 1,
        }
    ]


def test_mixed_convergence_codes_and_extra_diagnostics_still_count():
    corners = [
        _unit("a", "singular_matrix", "measurement"),
        _unit("b", "measurement", "nonconvergence"),
    ]
    assert len(_hints_in(_attach(corners, _worker_entry()))) == 1


def test_one_unit_ok_gets_no_hint():
    corners = [_unit("a", "singular_matrix"), _unit("b", status="pass")]
    assert "diagnostics" not in _attach(corners, _worker_entry())


def test_a_unit_that_errored_for_another_reason_gets_no_hint():
    corners = [_unit("a", "singular_matrix"), _unit("b", "ngspice_fatal")]
    assert "diagnostics" not in _attach(corners, _worker_entry())


def test_non_convergence_codes_alone_get_no_hint():
    corners = [_unit("a", "ngspice_fatal"), _unit("b", "ngspice_fatal")]
    assert "diagnostics" not in _attach(corners, _worker_entry())


def test_an_errored_unit_with_no_diagnostics_gets_no_hint():
    corners = [_unit("a", "singular_matrix"), _unit("b")]
    assert "diagnostics" not in _attach(corners, _worker_entry())


@pytest.mark.parametrize("backend", ["local", "remote", "local-parallel", None])
def test_non_batch_backends_get_no_hint(backend):
    corners = [_unit("a", "singular_matrix"), _unit("b", "singular_matrix")]
    assert "diagnostics" not in _attach(corners, _worker_entry(), backend=backend)


@pytest.mark.parametrize("ids", [[], ["a"]])
def test_fewer_than_two_units_get_no_hint(ids):
    corners = [_unit(i, "nonconvergence") for i in ids]
    assert "diagnostics" not in _attach(corners, _worker_entry())


def test_hint_is_attached_once_however_often_it_is_called():
    corners = [_unit("a", "singular_matrix"), _unit("b", "singular_matrix")]
    report = _attach(corners, _worker_entry())
    hints.attach_convergence_hint(report, corners, backend="batch")
    assert len(_hints_in(report)) == 1


def test_hint_appends_to_existing_report_diagnostics():
    corners = [_unit("a", "singular_matrix"), _unit("b", "singular_matrix")]
    report = {"environment": {}, "diagnostics": [{"code": "other", "message": "x"}]}
    hints.attach_convergence_hint(report, corners, backend="batch")
    assert [d["code"] for d in report["diagnostics"]] == ["other", _HINT_CODE]


def _attach_with_client_values(remote):
    corners = [_unit("a", "singular_matrix"), _unit("b", "singular_matrix")]
    return _attach(
        corners,
        remote,
        ngspice_init={"source": "home", "lines": None, "sha256": _CLIENT_SHA},
        engine_build={"version_banner": "CLIENT-BANNER"},
        threads_per_worker=8,
    )


@pytest.mark.parametrize(
    "remote",
    [
        None,  # a resumed run that kept no remote block
        {},  # a block with no worker_environment (legacy worker)
        {"worker_environment": None},
    ],
)
def test_missing_worker_environment_still_hints_without_worker_values(remote):
    [hint] = _hints_in(_attach_with_client_values(remote))
    # The client's own values must never be presented as the worker's.
    assert _CLIENT_SHA not in hint["message"]
    assert "CLIENT-BANNER" not in hint["message"]
    assert all(w["ngspice_init"] is None for w in hint["workers"])
    assert all(w["omp_num_threads"] is None for w in hint["workers"])
    assert all(w["threads_per_worker"] is None for w in hint["workers"])
    assert all(w["engine_build_comparison"] is None for w in hint["workers"])
    assert "Transient convergence differs between local and batch" in hint["message"]


def test_capture_absent_reports_the_capture_state_and_no_preflight_values():
    remote = _worker_entry(capture="absent", status="not_comparable")
    [hint] = _hints_in(_attach_with_client_values(remote))
    assert hint["workers"][0]["capture"] == "absent"
    assert hint["workers"][0]["omp_num_threads"] is None
    assert '"absent"' in hint["message"]
    assert _CLIENT_SHA not in hint["message"]
    assert "CLIENT-BANNER" not in hint["message"]


def test_ngspice_options_not_set_is_stated_not_invented():
    corners = [_unit("a", "singular_matrix"), _unit("b", "singular_matrix")]
    [hint] = _hints_in(_attach(corners, _worker_entry()))
    assert "environment.ngspice_options" in hint["message"]
    assert "not set" in hint["message"]


def test_many_distinct_shards_keep_the_message_bounded():
    corners = [_unit(f"c{i}", "singular_matrix") for i in range(20)]
    remote = {
        "fleet": [_worker_entry(omp=str(i), sha=f"sha256:{i:064x}") for i in range(20)]
    }
    [hint] = _hints_in(_attach(corners, remote))
    assert len(hint["workers"]) == 20  # the structured field is complete
    assert len(hint["message"]) < 2000  # three shard groups, not twenty
    assert "17 more" in hint["message"]


def test_malformed_remote_blocks_never_raise():
    corners = [_unit("a", "singular_matrix"), _unit("b", "singular_matrix")]
    for remote in ("x", 3, [], {"fleet": "x"}, {"fleet": [None, 4]}):
        assert len(_hints_in(_attach(corners, remote))) == 1
    junk = [None, "x", {"status": "error"}, _unit("a", "singular_matrix")]
    assert "diagnostics" not in _attach(junk, _worker_entry())


def test_runner_hints_entry_point_applies_both_hints():
    corners = [_unit("a", "singular_matrix"), _unit("b", "singular_matrix")]
    for corner in corners:
        corner["measurements"] = [{"name": "v", "value": None}]
    report = {"environment": {"remote": _worker_entry()}}
    hints.attach_runner_hints(
        report, [{"name": "v", "expr": "v(out)"}], corners, backend="batch"
    )
    assert sorted(d["code"] for d in report["diagnostics"]) == sorted(
        ["expr_runner_may_predate", _HINT_CODE]
    )


# --- through run_sim: the hint is built from the finished report --------------


def _fake_batch_dispatch(monkeypatch, *, worker_environment, first_converges=False):
    """Swap the batch dispatch for a fake whose every corner fails to converge
    (except the first, with ``first_converges``) and which reports
    ``worker_environment``."""

    def fake(*, corner_points, **kwargs):
        corners = [
            _fake_corner(
                point,
                code=None if first_converges and i == 0 else "singular_matrix",
            )
            for i, point in enumerate(corner_points)
        ]
        remote = {
            "provider": "aws-batch-fleet",
            "worker_environment": worker_environment,
        }
        return corners, "46", remote

    monkeypatch.setitem(sim._BACKENDS, "batch", fake)


_RUN_SIM_FIXTURES = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)


@_RUN_SIM_FIXTURES
def test_run_sim_batch_grid_that_never_converges_carries_the_hint(
    tmp_path, monkeypatch
):
    worker_environment = {
        "capture": "ok",
        "simulator": {"version_banner": "ngspice-46 WORKER"},
        "ngspice_init": {"source": "none", "lines": None, "sha256": None},
        "omp_num_threads": None,
    }
    _fake_batch_dispatch(monkeypatch, worker_environment=worker_environment)
    report = _run(_resume_request(tmp_path), tmp_path)

    assert report["errored"] == report["corner_count"] == 3
    [hint] = _hints_in(report)
    # The fake worker block carries no comparison; klt attaches one after the
    # dispatch. The hint must quote that attached status (whatever this host's
    # own ngspice makes it: match / mismatch / not_comparable), which a hint
    # built before the attach could not -- it would read None.
    attached = report["environment"]["remote"]["worker_environment"][
        "engine_build_comparison"
    ]["status"]
    assert attached in ("match", "mismatch", "not_comparable")
    assert hint["workers"][0]["engine_build_comparison"] == attached
    assert f'engine_build_comparison.status="{attached}"' in hint["message"]
    assert hint["unit_count"] == 3


@_RUN_SIM_FIXTURES
def test_run_sim_batch_grid_with_one_converging_corner_has_no_hint(
    tmp_path, monkeypatch
):
    _fake_batch_dispatch(
        monkeypatch, worker_environment={"capture": "ok"}, first_converges=True
    )
    report = _run(_resume_request(tmp_path), tmp_path)
    assert report["errored"] == report["corner_count"] - 1 == 2
    assert _hints_in(report) == []


def test_hint_points_at_a_docs_section_that_exists():
    # The hint tells the reader to open this section by name; a rename must
    # not strand the pointer.
    docs = Path(__file__).resolve().parents[1] / "docs" / "cli" / "sim.md"
    assert "### Transient convergence differs between local and batch\n" in (
        docs.read_text(encoding="utf-8")
    )


# --- the hint reads what klt's own producers emit, not hand-built dicts -------


def _real_job_identity(tmp_path, monkeypatch, *, client_banner):
    """``environment.remote`` as the real producers build it: the on-disk
    worker capture and the worker's own ``ngspice_init`` through
    ``_read_job_identity``, then klt's ``engine_build_comparison``."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".spiceinit").write_text("set wnflag=1\n")
    monkeypatch.setenv("HOME", str(home))
    init = sim_spiceinit.effective_init((), (), engine="ngspice", offhost=False)
    capture_dir = tmp_path / "collected"
    capture_dir.mkdir()
    (capture_dir / sb.BATCH_WORKER_ENVIRONMENT_FILENAME).write_text(_capture())
    identity = sb._read_job_identity(
        str(capture_dir), None, report={"environment": {"ngspice_init": init}}
    )
    sb.attach_engine_build_comparison(identity, {"version_banner": client_banner})
    return identity, init


def test_hint_reads_the_real_worker_environment_shape(tmp_path, monkeypatch):
    identity, init = _real_job_identity(
        tmp_path, monkeypatch, client_banner="ngspice-46 build B"
    )
    assert init["source"] == "home"  # produced by the real effective_init
    corners = [_unit("a", "singular_matrix"), _unit("b", "nonconvergence")]
    [hint] = _hints_in(_attach(corners, identity))
    assert hint["workers"] == [
        {
            "fleet_index": None,
            "capture": "ok",
            "engine_build_comparison": "mismatch",
            "ngspice_init": {"source": "home", "sha256": init["sha256"]},
            "omp_num_threads": "1",
            "threads_per_worker": None,
        }
    ]
    assert init["sha256"] in hint["message"]
    assert 'engine_build_comparison.status="mismatch"' in hint["message"]
    assert 'omp_num_threads="1"' in hint["message"]


def test_hint_reads_a_real_absent_capture_beside_a_real_one(tmp_path, monkeypatch):
    ok, init = _real_job_identity(tmp_path, monkeypatch, client_banner="x")
    absent = sb._read_job_identity(None)  # what a legacy worker leaves behind
    sb.attach_engine_build_comparison(absent, {"version_banner": "x"})
    corners = [_unit(f"c{i}", "singular_matrix") for i in range(3)]
    [hint] = _hints_in(_attach(corners, {"fleet": [ok, absent]}))
    first, second = hint["workers"]
    assert (first["fleet_index"], first["capture"]) == (0, "ok")
    assert first["ngspice_init"]["sha256"] == init["sha256"]
    assert (second["fleet_index"], second["capture"]) == (1, "absent")
    assert second["ngspice_init"] is None and second["omp_num_threads"] is None
    # Two distinct value sets, so both are spelled out per shard.
    assert "fleet[0]" in hint["message"] and "fleet[1]" in hint["message"]
