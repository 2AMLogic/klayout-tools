"""Issue #5014: ``--batch-set --collect-partial`` and the per-shard summary."""

import json
import os

from klayout_tools import sim_set
from klayout_tools.cli import sim_cmd
from klayout_tools.sim_batch_observer import current_job_observer
from test_sim_batch import _fake_report, _write_set


def _shard_map(outdir, stem):
    d = outdir / stem
    d.mkdir(parents=True)
    (d / "batch-shards.json").write_text("{}")
    return str(d / "batch-shards.json")


def _partial_report(unfinished):
    report = _fake_report("error")
    report["environment"]["remote"]["unfinished_shards"] = [
        {"index": i, "job_id": j, "state": s} for i, (j, s) in enumerate(unfinished)
    ]
    return report


def test_partial_member_attaches_with_shard_map_and_exits_nonzero(tmp_path):
    paths = _write_set(tmp_path, 2)
    outdir = tmp_path / "out"
    maps = {"req0": _shard_map(outdir, "req0")}
    calls = {}

    def fake_run(path, **kwargs):
        calls[os.path.basename(path)] = kwargs
        if "attach_job_id" in kwargs:
            return _partial_report([("j4", "interrupted")])
        return _fake_report("pass")

    report = sim_set.run_sim_set(
        paths, outdir=str(outdir), run=fake_run, collect_partial=True
    )
    assert calls["req0.json"]["attach_job_id"] == maps["req0"]
    assert calls["req0.json"]["collect_partial"] is True
    assert "attach_job_id" not in calls["req1.json"]
    assert [r["exit_code"] for r in report["requests"]] == [4, 0]
    assert report["status"] == "error"
    assert report["requests"][0]["shards"] == [
        {"job_id": "j4", "last_state": "interrupted", "interrupted_observations": 0}
    ]
    assert (
        json.loads((outdir / "req0" / "report.json").read_text())["status"] == "error"
    )


def test_without_flag_nothing_changes(tmp_path):
    paths = _write_set(tmp_path, 1)
    outdir = tmp_path / "out"
    _shard_map(outdir, "req0")
    seen = {}

    def fake_run(path, **kwargs):
        seen.update(kwargs)
        return _fake_report("pass")

    sim_set.run_sim_set(paths, outdir=str(outdir), run=fake_run)
    assert "attach_job_id" not in seen and "collect_partial" not in seen


def test_shard_summary_counts_observed_interruptions(tmp_path):
    paths = _write_set(tmp_path, 1)

    def fake_run(path, **kwargs):
        observer = current_job_observer()
        for state in ("running", "interrupted", "running", "interrupted", "running"):
            observer.on_state("j1", state)
        observer.on_state("j2", "done")
        return _fake_report("pass")

    report = sim_set.run_sim_set(paths, outdir=str(tmp_path / "o"), run=fake_run)
    assert report["requests"][0]["shards"] == [
        {"job_id": "j1", "last_state": "running", "interrupted_observations": 2},
        {"job_id": "j2", "last_state": "done", "interrupted_observations": 0},
    ]


def test_cli_accepts_collect_partial_with_batch_set(tmp_path, monkeypatch):
    from klayout_tools.cli import main

    paths = _write_set(tmp_path, 1)
    seen = {}

    def fake_set(request_paths, **kwargs):
        seen.update(kwargs)
        return {
            "schema_version": 1,
            "status": "pass",
            "request_count": 1,
            "max_in_flight": 1,
            "submitted": 0,
            "refused": 0,
            "refused_codes": [],
            "requests": [
                {
                    "index": 0,
                    "request": paths[0],
                    "status": "pass",
                    "exit_code": 0,
                    "job_id": None,
                    "job_ids": [],
                    "report": None,
                    "error": None,
                    "shards": [],
                }
            ],
        }

    monkeypatch.setattr(sim_cmd, "run_sim_set", fake_set)
    code = main(["sim", "--batch-set", *paths, "--collect-partial", "--resume"])
    assert code == 0 and seen["collect_partial"] is True
