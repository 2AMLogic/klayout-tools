"""Issue #4971: ``--batch-set`` records each member's job id at submit time;
unsharded ``--attach`` refuses a request that is not the job's own."""

import json

import pytest

from klayout_tools import sim_batch, sim_set
from klayout_tools.sim import SimError
from klayout_tools.sim_batch_observer import current_job_observer, report_progress
from klayout_tools.sim_batch_receipt import check_attach_receipt, request_sha256
from test_sim_batch import _write_set


def test_manifest_has_job_id_before_member_finishes(tmp_path, capsys):
    paths = _write_set(tmp_path, 1)
    outdir = tmp_path / "out"
    seen = {}

    def fake_run(path, **kwargs):
        observer = current_job_observer()
        assert observer is not None and observer.label == path
        observer.on_job("klt-sim-abc")
        report_progress("klt-sim-abc", "submitted", observer)
        with open(outdir / "set-manifest.json", encoding="utf-8") as handle:
            seen.update(json.load(handle)["requests"][0])
        raise RuntimeError("client killed")

    report = sim_set.run_sim_set(paths, outdir=str(outdir), run=fake_run)

    assert seen["status"] == "running"
    assert seen["job_id"] == "klt-sim-abc"
    assert seen["job_ids"] == ["klt-sim-abc"]
    assert f"batch job klt-sim-abc ({paths[0]}): submitted" in capsys.readouterr().err
    assert report["requests"][0]["status"] == "error"


def test_two_jobs_leave_job_ids_without_single_job_id(tmp_path):
    manifest = sim_set._Manifest(str(tmp_path / "m.json"), ["a"], [str(tmp_path)])
    on_job = sim_set._record_job(manifest, 0)
    on_job("j1")
    on_job("j2")
    entry = json.loads((tmp_path / "m.json").read_text())["requests"][0]
    assert entry["job_ids"] == ["j1", "j2"]
    assert entry["job_id"] is None


def test_single_request_progress_line_unchanged(capsys):
    report_progress("klt-sim-x", "done")
    assert capsys.readouterr().err == "klt sim: batch job klt-sim-x: done\n"


def _receipt(tmp_path, job_id, request):
    (tmp_path / sim_batch.BATCH_RECEIPT_FILENAME).write_text(
        json.dumps({"job_id": job_id, "request_sha256": request_sha256(request)})
    )


def test_attach_refuses_mismatched_request(tmp_path):
    _receipt(tmp_path, "j1", {"netlist": "a"})
    with pytest.raises(SimError) as exc:
        check_attach_receipt(str(tmp_path), "j1", {"netlist": "b"})
    assert exc.value.code == "resume_fingerprint_mismatch"
    assert request_sha256({"netlist": "a"}) in str(exc.value)


def test_attach_proceeds_when_match_absent_or_other_job(tmp_path):
    check_attach_receipt(str(tmp_path), "j1", {"netlist": "b"})  # absent
    _receipt(tmp_path, "j1", {"netlist": "a"})
    check_attach_receipt(str(tmp_path), "j1", {"netlist": "a"})  # match
    check_attach_receipt(str(tmp_path), "j2", {"netlist": "b"})  # other
