"""``--batch-set`` with a member stuck pending / an interrupt (issue #5324)."""

import json
import os
import threading
import time

from klayout_tools import sim_set, sim_set_runtime
from klayout_tools.sim_batch_observer import current_job_observer
from klayout_tools.sim_set_runtime import (
    _Manifest,
    _MemberScheduler,
    _ShardTracker,
)
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


class _FakeClock:
    """Injectable monotonic clock for scheduler unit tests (issue #6324)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _scheduler(tmp_path, clock):
    manifest = _Manifest(
        str(tmp_path / "set-manifest.json"),
        ["/fake/a.json", "/fake/b.json"],
        ["/fake/a.json", "/fake/b.json"],
    )
    sched = _MemberScheduler(
        ["/fake/a.json", "/fake/b.json"],
        manifest,
        launch_budget=10.0,
        poll_interval_s=0.01,
        run_member=lambda index, tracker: {},
    )
    sched.trackers = [_ShardTracker(clock=clock) for _ in sched.paths]
    return sched


def _abandon_first(sched, clock, job_id):
    """Scan-abandon member 0 the way ``_scan_stalled`` does at the first scan
    past its budget: the held job has been ``pending`` one full window."""
    tracker = sched.trackers[0]
    tracker.on_job(job_id)  # accepted at the clock's start
    clock.now += 10.5
    sched.abandoned.add(0)
    sched.abandoned_at[0] = clock.now
    sched.results[0] = {"index": 0}
    sched.pending = [1]


def test_termination_keeps_one_window_after_the_abandonment(tmp_path, monkeypatch):
    """Issue #6324: the job already held when its member is abandoned is
    stalled by its own ``since`` at that very moment, so the termination test
    must floor its window at the abandonment -- otherwise the documented one
    more ``--wait-for-capacity`` window after the abandonment collapses to
    zero and the remaining members are dropped on the abandonment scan."""
    clock = _FakeClock()
    monkeypatch.setattr(sim_set_runtime.time, "monotonic", clock)
    sched = _scheduler(tmp_path, clock)
    _abandon_first(sched, clock, "job-held")

    def admit():
        with sched.cond:
            sched._admit_replacements(10.0)

    admit()
    assert sched.pending == [1]  # inside the window: nothing terminated
    clock.now += 10.5  # one budget past the abandonment
    admit()
    assert sched.pending == []
    assert sched.results[1]["error"]["code"] == sim_set.NOT_LAUNCHED_CODE
    assert "job-held" in sched.results[1]["error"]["message"]
    assert sched.results[1]["job_ids"] == []


def test_new_shard_of_abandoned_member_gets_its_own_grace_window(tmp_path, monkeypatch):
    """Issue #6324 case 1: an abandoned multi-shard member's worker keeps
    submitting shards and each registers as ``pending``. A shard accepted long
    after the abandonment must not satisfy the termination test by inheritance
    -- it gets a full grace window from its own acceptance."""
    clock = _FakeClock()
    monkeypatch.setattr(sim_set_runtime.time, "monotonic", clock)
    sched = _scheduler(tmp_path, clock)
    tracker = sched.trackers[0]
    _abandon_first(sched, clock, "job-s1")
    tracker.on_state("job-s1", "running")  # the fleet eventually launched it
    clock.now = 1100.0  # long past the abandonment
    tracker.on_job("job-s2")  # the worker submitted the next shard

    def admit():
        with sched.cond:
            sched._admit_replacements(10.0)

    admit()
    assert sched.pending == [1]  # transient shard: no termination
    clock.now = 1110.5  # job-s2 pending past its own window
    admit()
    assert sched.pending == []
    assert sched.results[1]["error"]["code"] == sim_set.NOT_LAUNCHED_CODE
    assert "job-s2" in sched.results[1]["error"]["message"]


def test_termination_rearms_for_a_shard_accepted_after_the_gate_reopens(
    tmp_path, monkeypatch
):
    """Issue #6324 case 2: once the held job launches, a later shard of the
    long-abandoned member going ``pending`` must get a full grace window
    instead of inheriting the already-expired one (zero grace)."""
    clock = _FakeClock()
    monkeypatch.setattr(sim_set_runtime.time, "monotonic", clock)
    sched = _scheduler(tmp_path, clock)
    tracker = sched.trackers[0]
    _abandon_first(sched, clock, "job-held")

    def admit():
        with sched.cond:
            sched._admit_replacements(10.0)

    clock.now = 1015.0  # halfway into the held job's post-abandonment window
    admit()
    assert sched.pending == [1]
    tracker.on_state("job-held", "running")  # launched: the gate reopens
    clock.now = 1200.0  # much later, a new shard of the abandoned member
    tracker.on_job("job-s2")
    admit()
    assert sched.pending == [1]  # full grace from 1200: nothing terminated
    clock.now = 1210.5
    admit()
    assert sched.pending == []
    assert "job-s2" in sched.results[1]["error"]["message"]
