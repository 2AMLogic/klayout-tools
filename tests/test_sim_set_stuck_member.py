"""``--batch-set`` with a member stuck pending / an interrupt (issue #5324)."""

import json
import os
import threading
import time

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


def test_never_launching_job_ends_unstarted_members_as_not_launched(
    tmp_path, monkeypatch
):
    """Issue #5849: with ``max_in_flight=1`` the unstarted members must not be
    submitted while an abandoned member's job still holds fleet capacity. A
    job that never leaves ``pending`` ends them as ``not_launched`` entries
    (naming the held job) instead of piling more submissions onto the fleet."""
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
    entries = report["requests"]
    assert [e["status"] for e in entries] == ["error", "error", "error"]
    assert entries[0]["error"]["code"] == sim_set.NOT_LAUNCHED_CODE
    for entry in entries[1:]:
        assert entry["error"]["code"] == sim_set.NOT_LAUNCHED_CODE
        assert "job-stuck" in entry["error"]["message"]
        assert entry["job_ids"] == []
    assert report["status"] == "error"
    manifest = json.loads((tmp_path / "out" / sim_set.MANIFEST_FILENAME).read_text())
    for i in (1, 2):
        assert manifest["requests"][i]["error_code"] == sim_set.NOT_LAUNCHED_CODE
        assert manifest["requests"][i]["job_ids"] == []


def _manifest_entry(tmp_path, index: int) -> dict:
    manifest = json.loads((tmp_path / "out" / sim_set.MANIFEST_FILENAME).read_text())
    return manifest["requests"][index]


def test_replacement_waits_for_held_pending_job_then_admits(tmp_path, monkeypatch):
    """Issue #5849: the admission gate. While an abandoned member's job is
    last seen ``pending``, no replacement is admitted; once it leaves
    ``pending``, the set resumes and runs the rest."""
    _fast_poll(monkeypatch)
    paths = _write_set(tmp_path, 3)
    held = threading.Event()
    proceed = threading.Event()
    admitted: list[str] = []

    def fake_run(path, **kwargs):
        admitted.append(os.path.basename(path))
        observer = current_job_observer()
        if "req0" in path:
            observer.on_job("job-held")
            observer.on_state("job-held", "pending")
            held.set()
            proceed.wait(30)
            observer.on_state("job-held", "running")
        return _fake_report()

    result: dict = {}

    def drive() -> None:
        result["report"] = sim_set.run_sim_set(
            paths,
            outdir=str(tmp_path / "out"),
            max_in_flight=1,
            run=fake_run,
            capacity_wait_s=0.2,
        )

    worker = threading.Thread(target=drive, daemon=True)
    worker.start()
    try:
        assert held.wait(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (
                _manifest_entry(tmp_path, 0).get("error_code")
                == sim_set.NOT_LAUNCHED_CODE
            ):
                break
            time.sleep(0.01)
        assert _manifest_entry(tmp_path, 0)["error_code"] == sim_set.NOT_LAUNCHED_CODE
        time.sleep(0.08)  # several scheduler scans with the gate closed
        assert admitted == ["req0.json"]
        assert _manifest_entry(tmp_path, 1)["status"] == "pending"
    finally:
        proceed.set()
    worker.join(10)
    assert [e["status"] for e in result["report"]["requests"]] == [
        "error",
        "pass",
        "pass",
    ]
    assert admitted == ["req0.json", "req1.json", "req2.json"]


def test_interrupt_while_gate_held_still_terminates(tmp_path, monkeypatch):
    """Issue #5849 edge: an interrupt landing while another member is held
    pending still gives every member an entry and returns (no hang). A member
    admitted through the normal pipeline before the gate closed keeps its
    result; only replacement admissions wait."""
    _fast_poll(monkeypatch)
    paths = _write_set(tmp_path, 3)
    held = threading.Event()
    release = threading.Event()

    def fake_run(path, **kwargs):
        observer = current_job_observer()
        if "req0" in path:
            observer.on_job("job-held")
            observer.on_state("job-held", "pending")
            held.set()
            release.wait(30)
            return _fake_report()
        if "req1" in path:
            raise KeyboardInterrupt
        return _fake_report()

    try:
        report = sim_set.run_sim_set(
            paths,
            outdir=str(tmp_path / "out"),
            max_in_flight=2,
            run=fake_run,
            capacity_wait_s=0.05,
        )
    finally:
        release.set()
    assert len(report["requests"]) == 3
    by_index = {e["index"]: e for e in report["requests"]}
    assert by_index[0]["error"]["code"] == sim_set.NOT_LAUNCHED_CODE
    assert by_index[1]["error"]["code"] == sim_set.SET_INTERRUPTED_CODE
    assert by_index[2]["status"] == "pass"
    assert report["status"] == "error"


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


def test_manifest_ignores_writes_after_member_finalized(tmp_path):
    """An abandoned worker must not clobber the member's final error entry."""
    from klayout_tools.sim_set import _Manifest

    manifest = _Manifest(str(tmp_path / "m.json"), ["a.json"], [str(tmp_path)])
    manifest.finalize(0, status="error", error_code="not_launched")
    manifest.update(0, status="done")
    manifest.append_job(0, "job-late")
    assert manifest._entries[0]["status"] == "error"
    assert manifest._entries[0]["error_code"] == "not_launched"
    assert manifest.job_ids(0) == []
