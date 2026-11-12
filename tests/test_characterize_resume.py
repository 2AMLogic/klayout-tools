"""``klt characterize --resume`` (issue #5350): collected legs are reloaded,
unsharded batch legs re-attach to the job their receipt names, changed inputs
resubmit."""

from __future__ import annotations

import json

import pytest

from klayout_tools import characterize_leg, sim, sim_batch, sim_batch_receipt
from klayout_tools.cli.parser import create_parser

REQUEST = {"netlist": "tb.spice", "corners": {"process": ["tt"]}}
PASS_REPORT = {"corners": [{"corner_id": "c0", "status": "pass", "diagnostics": []}]}


def _leg(tmp_path, calls, monkeypatch, **overrides):
    def fake_run_sim(path, **kwargs):
        calls.append(kwargs)
        return PASS_REPORT

    monkeypatch.setattr(sim, "run_sim", fake_run_sim)
    kwargs = dict(
        testbench_path=str(tmp_path / "leg" / "testbench.spice"),
        testbench_text="* tb\n",
        sim_request=REQUEST,
        sim_request_path=str(tmp_path / "leg" / "sim-request.json"),
        sim_report_path=str(tmp_path / "leg" / "sim-report.json"),
        sim_dir=str(tmp_path / "leg" / "sim"),
        backend="batch",
        resume=True,
    )
    kwargs.update(overrides)
    return characterize_leg.run_sim_leg(**kwargs)


def test_resume_skips_a_collected_leg(tmp_path, monkeypatch):
    calls: list = []
    _leg(tmp_path, calls, monkeypatch, resume=False)
    assert calls[-1]["resume"] is None
    assert _leg(tmp_path, calls, monkeypatch) == PASS_REPORT
    assert len(calls) == 1  # reloaded, not re-simulated


def test_resume_reruns_with_resume_when_report_missing(tmp_path, monkeypatch):
    calls: list = []
    _leg(tmp_path, calls, monkeypatch, resume=False)
    (tmp_path / "leg" / "sim-report.json").unlink()
    _leg(tmp_path, calls, monkeypatch)
    assert len(calls) == 2 and calls[-1]["resume"] is True


def test_changed_inputs_never_reuse_a_stale_report(tmp_path, monkeypatch):
    calls: list = []
    _leg(tmp_path, calls, monkeypatch, resume=False)
    _leg(tmp_path, calls, monkeypatch, testbench_text="* changed\n")
    assert len(calls) == 2


def test_unrun_corner_report_is_not_reused(tmp_path, monkeypatch):
    calls: list = []
    _leg(tmp_path, calls, monkeypatch, resume=False)
    path = tmp_path / "leg" / "sim-report.json"
    path.write_text(
        json.dumps(
            {
                "corners": [
                    {
                        "status": "error",
                        "diagnostics": [{"code": "lost_shard"}],
                    }
                ]
            }
        )
    )
    _leg(tmp_path, calls, monkeypatch)
    assert len(calls) == 2


def _receipt(tmp_path, **fields):
    receipt = {
        "job_id": "job-7",
        "request_sha256": sim_batch_receipt.receipt_request_sha256(REQUEST),
        "unit_count": 1,
    }
    receipt.update(fields)
    (tmp_path / sim_batch.BATCH_RECEIPT_FILENAME).write_text(json.dumps(receipt))


def test_resumable_job_id_matches_receipt(tmp_path):
    _receipt(tmp_path)
    assert (
        sim_batch_receipt.resumable_job_id(str(tmp_path), REQUEST, 1, True) == "job-7"
    )


@pytest.mark.parametrize(
    "fields",
    [{"request_sha256": "0" * 64}, {"unit_count": 3}, {"unit_count": None}],
)
def test_resumable_job_id_refuses_mismatch(tmp_path, fields):
    _receipt(tmp_path, **fields)
    assert sim_batch_receipt.resumable_job_id(str(tmp_path), REQUEST, 1, True) is None


def test_resumable_job_id_without_receipt(tmp_path):
    assert sim_batch_receipt.resumable_job_id(str(tmp_path), REQUEST, 1, True) is None


def test_cli_accepts_resume():
    args = create_parser().parse_args(["characterize", "req.json", "--resume"])
    assert args.resume is True
    assert create_parser().parse_args(["characterize", "req.json"]).resume is None


def _run_batch_with(tmp_path, monkeypatch, checkpoint):
    seen: dict = {}

    def fake_job(**kwargs):
        seen.update(kwargs)
        return [], None, None

    monkeypatch.setattr(sim_batch, "_run_batch_job", fake_job)
    monkeypatch.setattr(sim_batch, "new_job_id", lambda: "fresh-job")
    monkeypatch.setattr(sim_batch, "_resolve_batch_config", lambda *a, **k: object())
    from klayout_tools import sim_batch_plan

    monkeypatch.setattr(sim_batch_plan, "plan_batch_job", lambda *a, **k: ({}, 1))
    monkeypatch.setattr(sim_batch, "_feature_diagnostic_field", lambda c: {})
    sim_batch._run_batch(
        corner_points=[object()],
        netlist_path="tb.spice",
        models_lib=None,
        analysis={},
        measurements_spec=[],
        timeout_s=10,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path),
        max_workers=1,
        request=REQUEST,
        checkpoint=checkpoint,
    )
    return seen


def test_batch_resume_reattaches_to_receipt_job(tmp_path, monkeypatch):
    _receipt(tmp_path)
    seen = _run_batch_with(tmp_path, monkeypatch, checkpoint=object())
    assert seen["job_id"] == "job-7" and seen["attach"] is True


def test_batch_without_resume_submits_fresh(tmp_path, monkeypatch):
    _receipt(tmp_path)
    seen = _run_batch_with(tmp_path, monkeypatch, checkpoint=None)
    assert seen["job_id"] == "fresh-job" and seen["attach"] is False
