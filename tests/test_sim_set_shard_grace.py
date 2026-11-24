"""Grace windows for new shards of an abandoned batch member."""

from klayout_tools import sim_set, sim_set_runtime
from klayout_tools.sim_set_runtime import (
    _Manifest,
    _MemberScheduler,
    _ShardTracker,
)


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
