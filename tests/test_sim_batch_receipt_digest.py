"""Sync submit receipt digest vs. flag-less ``--attach`` (issue #5194). Split
out of `test_sim_batch.py` to keep that file under its size ceiling."""

from __future__ import annotations

import hashlib
import json

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim, sim_batch_shards
from klayout_tools import sim_batch as sb
from klayout_tools.sim import SimError
from klayout_tools.sim_batch_receipt import check_attach_receipt
from test_sim_batch import (
    FAKE_BUCKET,
    INPUTS_UPLOAD_ARGV,
    _batch_request,
    _FakeRunner,
    _install_fake_batch_transport,
    _receipt_job_kwargs,
    _sim_report,
    _status,
    _write_body,
    _write_request,
)


def test_sync_receipt_ignores_submission_only_batch_fields_on_attach(
    tmp_path, monkeypatch
):
    """--wait-for-capacity folds into request.batch.capacity_wait_s; --attach
    cannot repeat it, so the receipt digest must not depend on it (#5194)."""
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}
    _install_fake_batch_transport(monkeypatch, runner=runner)
    submitted = {
        "netlist": "body.spice",
        "corners": {"process": ["tt"]},
        "batch": {"capacity_wait_s": 600, "max_concurrent_jobs": 2},
    }
    sb._run_batch_job(**_receipt_job_kwargs(tmp_path, runner, request=submitted))
    artifacts = str(tmp_path / "artifacts")

    original = {"netlist": "body.spice", "corners": {"process": ["tt"]}}
    check_attach_receipt(artifacts, "job-1", original)
    check_attach_receipt(artifacts, "job-1", submitted)
    with pytest.raises(SimError) as exc:
        check_attach_receipt(
            artifacts, "job-1", {**original, "corners": {"process": ["ff"]}}
        )
    assert exc.value.code == "resume_fingerprint_mismatch"


@pytest.mark.parametrize("capacity_wait_s", [None, 45])
def test_corner_job_attach_verifies_the_inputs_before_collection(
    tmp_path, monkeypatch, capacity_wait_s
):
    runner = _FakeRunner()
    _install_fake_batch_transport(monkeypatch, runner=runner)
    netlist = _write_body(tmp_path)
    process = ["tt", "ss", "ff"]
    (tmp_path / "models.lib").write_text(
        "".join(f".lib {p}\n.endl {p}\n" for p in process)
    )
    request = _batch_request(
        tmp_path,
        models={"lib": "models.lib"},
        corners={"process": process},
        options={"stage_model_inputs": True},
        measurements=[{"name": "vpeak", "expr": "v(out)", "reduce": "max"}],
    )
    batch = request.pop("batch")
    monkeypatch.setenv("KLT_BATCH_PROVISION_SCRIPT", batch["provision_script_path"])
    monkeypatch.setenv("KLT_BATCH_JOB_BUCKET", FAKE_BUCKET)
    path = _write_request(tmp_path, request)
    artifacts = str(tmp_path / "out")
    submitted = sim.run_sim(
        str(path),
        artifacts_dir=artifacts,
        submit_only=True,
        capacity_wait_s=capacity_wait_s,
    )
    job_id = submitted["job_id"]
    receipt_path = tmp_path / "out" / sb.BATCH_RECEIPT_FILENAME
    receipt = json.loads(receipt_path.read_text())
    assert receipt["request_sha256"] == submitted["request_sha256"]
    runner.calls.clear()
    runner.default = fake_completed(_status("done", job_id=job_id))
    runner.outputs = {"report.json": _sim_report([f"{p}_1.800_27" for p in process])}

    report = sim.run_sim(str(path), artifacts_dir=artifacts, attach_job_id=job_id)
    assert report["corner_count"] == 3
    assert not runner.argvs_matching("launch")
    assert not runner.argvs_matching("s3", "cp", INPUTS_UPLOAD_ARGV)
    assert receipt["netlist_sha256"] == hashlib.sha256(netlist.read_bytes()).hexdigest()

    runner.calls.clear()
    netlist.write_text(netlist.read_text().replace("1k", "2k"))
    with pytest.raises(SimError, match="netlist") as error:
        sim.run_sim(str(path), artifacts_dir=artifacts, attach_job_id=job_id)
    assert error.value.code == "resume_fingerprint_mismatch"
    assert not runner.calls

    _write_body(tmp_path)
    _write_request(tmp_path, {**request, "corners": {"process": ["tt"]}})
    with pytest.raises(SimError) as error:
        sim.run_sim(str(path), artifacts_dir=artifacts, attach_job_id=job_id)
    assert error.value.code == "resume_fingerprint_mismatch"
    assert not runner.calls


def test_legacy_async_receipt_accepts_submit_created_empty_batch(tmp_path):
    request = {"netlist": "body.spice", "corners": {"process": ["tt", "ss", "ff"]}}
    submitted = {**request, "batch": {"capacity_wait_s": 45}}
    digest = sim_batch_shards.request_sha256(submitted)
    assert digest != sim_batch_shards.request_sha256(request)
    (tmp_path / sb.BATCH_RECEIPT_FILENAME).write_text(
        json.dumps({"job_id": "job-1", "request_sha256": digest})
    )
    check_attach_receipt(str(tmp_path), "job-1", request)
    with pytest.raises(SimError) as error:
        check_attach_receipt(
            str(tmp_path), "job-1", {**request, "corners": {"process": ["ss"]}}
        )
    assert error.value.code == "resume_fingerprint_mismatch"
