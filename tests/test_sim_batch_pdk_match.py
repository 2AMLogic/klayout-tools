"""Batch PDK revision gate tests."""

import json
from pathlib import Path

import pytest

from klayout_tools import sim, sim_batch_pdk
from klayout_tools import sim_batch as sb
from klayout_tools.cli import main
from test_sim_batch import (
    FAKE_BUCKET,
    _batch_request,
    _capture,
    _capture_uploads,
    _corner_points,
    _FakeRunner,
    _install_fake_batch_transport,
    _local_pdk,
    _measurements_spec,
    _pin_client_version,
    _provision_script,
    _run_one_job,
    _sim_report,
    _status,
    _worker_env,
    _write_body,
    _write_request,
    fake_completed,
    uploaded_request_of,
)

_WORKER_SHA = "a" * 64  # what `_capture()`'s worker stamp reports
_OTHER_SHA = "b" * 64


def _gated_run(tmp_path, monkeypatch, *, pin, tree, enforced, worker_sha=_WORKER_SHA):
    """One batch job under the gate. ``tree``: the local PDK tree's stamp text
    (``None`` for no local tree at all)."""
    monkeypatch.setenv("PDK_ROOT", str(tmp_path / "missing-install"))
    request = {"netlist": "body.spice", "models": {"pdk": "sky130A"}}
    if tree is not None:
        request = _local_pdk(tmp_path, monkeypatch, tree)
    if pin is not None:
        request["models"]["pdk_expect"] = {"stamp_sha256": pin}
    if enforced:
        request["options"] = {"require_pdk_match": True}
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    pdk = json.loads(_capture())["pdk"]
    runner.outputs = {
        "report.json": _sim_report(),
        "worker_environment.json": _capture(pdk={**pdk, "stamp_sha256": worker_sha}),
    }
    return _run_one_job(tmp_path, monkeypatch, runner, request=request)


def _tree_sha(text):
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.parametrize("enforced", [False, True])
@pytest.mark.parametrize(
    ("pin", "tree", "status"),
    [
        (_WORKER_SHA, None, "match"),  # pin alone, no local tree
        (_OTHER_SHA, None, "mismatch"),
        (None, None, "not_comparable"),  # nothing to compare against
        (_WORKER_SHA, "open_pdks other", "match"),  # the pin wins over the tree
        (None, "open_pdks other", "mismatch"),
        (None, None, "not_comparable"),
    ],
)
def test_require_pdk_match_matrix(tmp_path, monkeypatch, pin, tree, status, enforced):
    corners, _, environment = _gated_run(
        tmp_path, monkeypatch, pin=pin, tree=tree, enforced=enforced
    )
    comparison = environment["worker_environment"]["pdk_stamp_comparison"]
    assert comparison["status"] == status
    assert ("required" in comparison) is enforced
    if pin is not None:
        assert comparison["expected"] == {"stamp_sha256": pin}
    if enforced and status != "match":
        diag = corners[0]["diagnostics"][0]
        assert corners[0]["status"] == "error"
        assert diag["code"] == f"pdk_stamp_{status}"
        assert _WORKER_SHA in diag["message"]  # names the worker stamp...
        if pin is not None:
            assert pin in diag["message"]  # ...and the expected one
        if pin is None and tree is not None:
            assert _tree_sha(tree) in diag["message"]
    else:
        assert corners[0]["status"] != "error"  # unenforced / match: untouched


def test_require_pdk_match_fails_closed_on_a_legacy_worker(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}  # no worker_environment.json
    request = {
        "netlist": "body.spice",
        "models": {"pdk": "sky130A", "pdk_expect": {"stamp_sha256": _WORKER_SHA}},
        "options": {"require_pdk_match": True},
    }
    corners, _, environment = _run_one_job(
        tmp_path, monkeypatch, runner, request=request
    )
    assert environment["worker_environment"]["capture"] == "absent"
    assert corners[0]["diagnostics"][0]["code"] == "pdk_stamp_not_comparable"


def test_pdk_pin_for_another_variant_is_not_comparable(tmp_path, monkeypatch):
    request = {
        "netlist": "body.spice",
        "models": {"pdk": "gf180mcuD", "pdk_expect": {"stamp_sha256": _WORKER_SHA}},
    }
    env = _worker_env(
        tmp_path, monkeypatch, {"worker_environment.json": _capture()}, request=request
    )
    assert (
        env["worker_environment"]["pdk_stamp_comparison"]["status"] == "not_comparable"
    )


@pytest.mark.parametrize(
    "pin", ["abc", {"stamp_sha256": "abc"}, {"stamp_sha256": 5}, {"commit": "abc1234"}]
)
def test_malformed_pdk_expect_is_a_request_error(pin):
    with pytest.raises(sim.SimError, match="pdk_expect"):
        sim_batch_pdk.pdk_expectation({"models": {"pdk_expect": pin}})


def test_require_pdk_match_option_must_be_boolean():
    with pytest.raises(sim.SimError, match="require_pdk_match"):
        sim_batch_pdk.require_pdk_match_requested(
            {"options": {"require_pdk_match": "yes"}}
        )


def _gate_request(tmp_path, backend, **options):
    request = _batch_request(tmp_path, backend=backend)
    request["models"]["pdk_expect"] = {"stamp_sha256": _WORKER_SHA}
    if options:
        request["options"] = options
    return str(_write_request(tmp_path, request))


@pytest.mark.parametrize("backend", ["local", "local-parallel", "remote"])
@pytest.mark.parametrize("via_flag", [True, False])
def test_require_pdk_match_refuses_non_batch_backends(tmp_path, backend, via_flag):
    _write_body(tmp_path)
    path = _gate_request(
        tmp_path, backend, **({} if via_flag else {"require_pdk_match": True})
    )
    with pytest.raises(sim.SimError, match="'batch' backend"):
        sim.run_sim(path, require_pdk_match=True if via_flag else None)


def test_require_pdk_match_end_to_end_flag_overrides_option(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {
        "report.json": _sim_report(),
        "worker_environment.json": _capture(
            pdk={**json.loads(_capture())["pdk"], "stamp_sha256": _OTHER_SHA}
        ),
    }
    _install_fake_batch_transport(monkeypatch, runner=runner)
    uploaded = _capture_uploads(monkeypatch, runner)
    _write_body(tmp_path)

    # Option off, flag on: the flag wins and the run fails closed.
    report = sim.run_sim(
        _gate_request(tmp_path, "batch", require_pdk_match=False),
        require_pdk_match=True,
    )
    assert report["corners"][0]["diagnostics"][0]["code"] == "pdk_stamp_mismatch"
    assert report["status"] != "pass"
    remote = report["environment"]["remote"]["worker_environment"]
    assert remote["pdk_stamp_comparison"]["required"] is True
    # The gate is submit-side: it is not shipped to the local-parallel job.
    assert not any(b"require_pdk_match" in blob for blob in uploaded.values())

    # Option on, flag off: the option still gates.
    report = sim.run_sim(_gate_request(tmp_path, "batch", require_pdk_match=True))
    assert report["corners"][0]["diagnostics"][0]["code"] == "pdk_stamp_mismatch"

    # No gate: a mismatch stays a warning and the run is not failed.
    report = sim.run_sim(_gate_request(tmp_path, "batch"))
    assert report["corners"][0]["status"] != "error"


def _host_default_gate_request(tmp_path, **options):
    """A gated single-corner request whose backend comes from the host default
    (``KLT_SIM_BACKEND``), not from the request or a flag."""
    request = _batch_request(tmp_path, options={"require_pdk_match": True, **options})
    del request["backend"]
    request["models"]["pdk_expect"] = {"stamp_sha256": _WORKER_SHA}
    return str(_write_request(tmp_path, request))


def test_require_pdk_match_single_corner_stays_on_host_default_batch(
    tmp_path, monkeypatch
):
    # A single-unit run under a host default would normally step aside to
    # `local`; under the gate that would pass ungated (no worker stamp).
    monkeypatch.setenv("KLT_SIM_BACKEND", "batch")
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {
        "report.json": _sim_report(),
        "worker_environment.json": _capture(
            pdk={**json.loads(_capture())["pdk"], "stamp_sha256": _OTHER_SHA}
        ),
    }
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _write_body(tmp_path)

    report = sim.run_sim(_host_default_gate_request(tmp_path))

    assert "worker_environment" in report["environment"]["remote"]
    assert report["corners"][0]["diagnostics"][0]["code"] == "pdk_stamp_mismatch"
    assert report["status"] != "pass"


def test_require_pdk_match_resume_stays_gated_on_host_default_batch(
    tmp_path, monkeypatch
):
    # `resume` would normally step a host-default batch run back to `local`;
    # under the gate it must stay on batch, where the worker stamp is checked.
    monkeypatch.setenv("KLT_SIM_BACKEND", "batch")
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {
        "report.json": _sim_report(),
        "worker_environment.json": _capture(
            pdk={**json.loads(_capture())["pdk"], "stamp_sha256": _OTHER_SHA}
        ),
    }
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _write_body(tmp_path)

    report = sim.run_sim(_host_default_gate_request(tmp_path, resume=True))

    assert "worker_environment" in report["environment"]["remote"]
    assert report["corners"][0]["diagnostics"][0]["code"] == "pdk_stamp_mismatch"
    assert report["status"] != "pass"


def test_require_pdk_match_checks_every_sharded_member(tmp_path, monkeypatch):
    _pin_client_version(monkeypatch)
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    _install_fake_batch_transport(monkeypatch, runner=runner)
    pdk = json.loads(_capture())["pdk"]

    ids = {point.temperature_c: point.corner_id for point in _corner_points(2)}

    def _register(argv, timeout_s):
        captured = uploaded_request_of(argv)
        if captured is not None:
            document, job_id = captured
            points = document["_explicit_points"]
            first = int(points[0]["temperature_c"])
            sha = _WORKER_SHA if first == 0 else _OTHER_SHA  # one bad member
            runner.outputs_by_job[job_id] = {
                "report.json": _sim_report(
                    corner_ids=tuple(ids[p["temperature_c"]] for p in points)
                ),
                "worker_environment.json": _capture(pdk={**pdk, "stamp_sha256": sha}),
            }
        return runner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _register)
    corners, _, _ = sb._run_batch_fleet(
        corner_points=_corner_points(2),
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=30.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        request={
            "netlist": "body.spice",
            "models": {"pdk": "sky130A", "pdk_expect": {"stamp_sha256": _WORKER_SHA}},
            "options": {"require_pdk_match": True},
            "batch": {
                "bucket": FAKE_BUCKET,
                "provision_script_path": str(_provision_script(tmp_path)),
                "poll_interval_s": 0.0,
            },
        },
        hosts=2,
        measurements_spec=_measurements_spec(),
    )
    assert [c["status"] for c in corners] == ["pass", "error"], [
        c.get("diagnostics") for c in corners
    ]
    assert corners[1]["diagnostics"][0]["code"] == "pdk_stamp_mismatch"


@pytest.mark.parametrize("kind", ["decks", "commands"])
def test_pdk_gate_refuses_non_corner_requests(kind, tmp_path):
    entries = [{"id": "one", "deck": "one.sp"}] if kind == "decks" else []
    path = _write_request(
        tmp_path, {kind: entries, "options": {"require_pdk_match": True}}
    )
    with pytest.raises(sim.SimError, match="only to batch corner requests"):
        sim.run_sim(str(path))


def test_staged_files_cannot_prove_executed_pdk_revision(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    pdk = json.loads(_capture())["pdk"]
    model = {"name": "model.lib", "sha256": _OTHER_SHA}
    runner.outputs = {
        "report.json": _sim_report(),
        "worker_environment.json": _capture(
            pdk={**pdk, "stamp_sha256": _WORKER_SHA, "models_staged": True},
            model_inputs=[model],
        ),
    }
    request = {
        "netlist": "body.spice",
        "models": {"pdk": "sky130A", "pdk_expect": {"stamp_sha256": _WORKER_SHA}},
        "options": {"require_pdk_match": True, "stage_model_inputs": False},
    }
    corners, _, env = _run_one_job(tmp_path, monkeypatch, runner, request=request)
    sb.attach_staged_model_comparison(env, [model], request)
    worker = env["worker_environment"]
    assert corners[0]["diagnostics"][0]["code"] == "pdk_stamp_not_comparable"
    assert worker["pdk_stamp_comparison"] == {
        "required": True,
        "status": "not_comparable",
        "reason": "models_staged",
        "expected": {"stamp_sha256": _WORKER_SHA},
    }
    assert worker["staged_model_comparison"]["status"] == "match"


def test_requested_staging_cannot_use_a_legacy_workers_image_stamp():
    worker = {"pdk_stamp_comparison": {"status": "match"}, "pdk": {}}
    sim_batch_pdk.mark_required(
        worker, {"options": {"stage_model_inputs": True, "require_pdk_match": True}}
    )
    assert worker["pdk_stamp_comparison"] == {
        "required": True,
        "status": "not_comparable",
        "reason": "models_staged",
    }


def test_resume_requires_the_same_revision_evidence(tmp_path, monkeypatch):
    request = _local_pdk(tmp_path, monkeypatch, text="build 1")
    path = str(_write_body(tmp_path))
    inputs = dict(
        netlist_path=path,
        models_lib=None,
        analysis={},
        measurements_spec=[],
        corner_points=_corner_points(1),
        timeout_s=30,
        engine="ngspice",
    )

    def fingerprint():
        return sim._checkpoint_fingerprint(
            **inputs, pdk_requirement=sim_batch_pdk.checkpoint_requirement(request)
        )

    advisory = fingerprint()
    assert advisory == sim._checkpoint_fingerprint(**inputs)
    request["options"] = {"require_pdk_match": True}
    local_first = fingerprint()
    (tmp_path / "pdkroot/sky130A/SOURCES").write_text("build 2")
    local_second = fingerprint()
    request["models"]["pdk_expect"] = {"stamp_sha256": _WORKER_SHA}
    pinned = fingerprint()
    request["models"]["pdk_expect"] = {"stamp_sha256": _OTHER_SHA}
    assert len({advisory, local_first, local_second, pinned, fingerprint()}) == 5


@pytest.mark.parametrize(
    "mode",
    [
        ["--list-jobs"],
        ["--job-status", "job"],
        ["--cancel", "job"],
        ["sample.json", "--op-lint"],
    ],
)
def test_inventory_and_status_modes_refuse_a_run_gate(mode, capsys):
    assert main(["sim", *mode, "--require-pdk-match", "--format", "json"]) == 2
    assert "--require-pdk-match" in capsys.readouterr().err


def test_cli_grades_the_batch_stamp(tmp_path, monkeypatch, capsys):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {
        "report.json": _sim_report(),
        "worker_environment.json": _capture(),
    }
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _write_body(tmp_path)
    path = _gate_request(tmp_path, "batch", require_pdk_match=False)
    request = json.loads(Path(path).read_text())
    request["models"]["pdk_expect"]["stamp_sha256"] = _OTHER_SHA
    path = _write_request(tmp_path, request)
    assert main(["sim", str(path), "--require-pdk-match", "--format", "json"]) == 4
    report = json.loads(capsys.readouterr().out)
    assert report["corners"][0]["diagnostics"][0]["code"] == "pdk_stamp_mismatch"


def test_invalid_worker_digest_is_unavailable_for_a_pin(tmp_path, monkeypatch):
    corners, _, environment = _gated_run(
        tmp_path,
        monkeypatch,
        pin=_WORKER_SHA,
        tree=None,
        enforced=True,
        worker_sha=_WORKER_SHA + "0",
    )
    worker = environment["worker_environment"]
    assert worker["pdk"]["stamp_sha256"] is None
    assert worker["pdk_stamp_comparison"]["reason"] == "worker_stamp_unavailable"
    assert corners[0]["diagnostics"][0]["code"] == "pdk_stamp_not_comparable"
