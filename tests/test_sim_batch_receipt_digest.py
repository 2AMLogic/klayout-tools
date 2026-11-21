"""Sync submit receipt digest vs. flag-less ``--attach`` (issue #5194). Split
out of `test_sim_batch.py` to keep that file under its size ceiling."""

from __future__ import annotations

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim_batch as sb
from klayout_tools.sim import SimError
from klayout_tools.sim_batch_receipt import check_attach_receipt
from test_sim_batch import (
    _FakeRunner,
    _install_fake_batch_transport,
    _receipt_job_kwargs,
    _sim_report,
    _status,
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
