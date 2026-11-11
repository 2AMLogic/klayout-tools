"""``--batch-set`` with a member stuck pending / an interrupt (issue #5324)."""

import json
import threading

from klayout_tools import sim_set
from klayout_tools.sim_batch_observer import current_job_observer
from test_sim_batch import _fake_report, _write_set


def _fast_poll(monkeypatch):
    monkeypatch.setattr(sim_set, "SET_POLL_INTERVAL_S", 0.01)


def _stuck_run(release: threading.Event, stuck_name: str = "req2"):
    def fake_run(path, **kwargs):
        observer = current_job_observer()
        if stuck_name in path:
            observer.on_job("job-stuck")
            observer.on_state("job-stuck", "pending")
            release.wait(30)
        return _fake_report()

    return fake_run


def test_stuck_member_becomes_not_launched_entry(tmp_path, monkeypatch):
    _fast_poll(monkeypatch)
    paths = _write_set(tmp_path, 4)
    release = threading.Event()
    try:
        report = sim_set.run_sim_set(
            paths,
            outdir=str(tmp_path / "out"),
            run=_stuck_run(release),
            capacity_wait_s=0.05,
        )
    finally:
        release.set()
    entries = report["requests"]
    assert [e["status"] for e in entries].count("pass") == 3
    stuck = entries[2]
    assert stuck["status"] == "error"
    assert stuck["exit_code"] == 4
    assert stuck["error"]["code"] == sim_set.NOT_LAUNCHED_CODE
    assert "job-stuck" in stuck["error"]["message"]
    assert stuck["job_ids"] == ["job-stuck"]
    assert report["status"] == "error"
    manifest = json.loads((tmp_path / "out" / sim_set.MANIFEST_FILENAME).read_text())
    assert manifest["requests"][2]["job_ids"] == ["job-stuck"]
    assert manifest["requests"][2]["error_code"] == sim_set.NOT_LAUNCHED_CODE


def test_stuck_member_with_max_in_flight_one_still_runs_the_rest(tmp_path, monkeypatch):
    _fast_poll(monkeypatch)
    paths = _write_set(tmp_path, 3)
    release = threading.Event()
    try:
        report = sim_set.run_sim_set(
            paths,
            outdir=str(tmp_path / "out"),
            max_in_flight=1,
            run=_stuck_run(release, "req0"),
            capacity_wait_s=0.05,
        )
    finally:
        release.set()
    assert [e["status"] for e in report["requests"]] == ["error", "pass", "pass"]


def test_single_member_set_stuck(tmp_path, monkeypatch):
    _fast_poll(monkeypatch)
    paths = _write_set(tmp_path, 1)
    release = threading.Event()
    try:
        report = sim_set.run_sim_set(
            paths,
            outdir=str(tmp_path / "out"),
            run=_stuck_run(release, "req0"),
            capacity_wait_s=0.05,
        )
    finally:
        release.set()
    assert report["requests"][0]["error"]["code"] == sim_set.NOT_LAUNCHED_CODE


def test_keyboard_interrupt_mid_set_emits_partial_envelope(tmp_path, monkeypatch):
    _fast_poll(monkeypatch)
    paths = _write_set(tmp_path, 3)
    release = threading.Event()

    def fake_run(path, **kwargs):
        if "req1" in path:
            current_job_observer().on_job("job-1")
            raise KeyboardInterrupt
        if "req2" in path:
            release.wait(30)
        return _fake_report()

    try:
        report = sim_set.run_sim_set(
            paths, outdir=str(tmp_path / "out"), run=fake_run, max_in_flight=3
        )
    finally:
        release.set()
    by_index = {e["index"]: e for e in report["requests"]}
    assert by_index[1]["error"]["code"] == sim_set.SET_INTERRUPTED_CODE
    assert by_index[1]["job_ids"] == ["job-1"]
    assert report["status"] == "error"
    assert (tmp_path / "out" / sim_set.MANIFEST_FILENAME).is_file()


def test_no_budget_means_no_bound(tmp_path):
    paths = _write_set(tmp_path, 2)
    report = sim_set.run_sim_set(
        paths, outdir=str(tmp_path / "out"), run=lambda p, **k: _fake_report()
    )
    assert report["status"] == "pass"


def test_cli_batch_set_writes_report_out(tmp_path, monkeypatch, capsys):
    from klayout_tools.cli import main
    from test_sim_batch_set_forwarding import _patch_set_run

    _patch_set_run(monkeypatch, lambda path, **kw: _fake_report())
    paths = _write_set(tmp_path, 2)
    out = tmp_path / "agg.json"
    code = main(
        ["sim", "--batch-set", *paths, "--report-out", str(out), "--format", "json"]
    )
    assert code == 0
    assert json.loads(out.read_text())["request_count"] == 2
    assert json.loads(capsys.readouterr().out)["request_count"] == 2
