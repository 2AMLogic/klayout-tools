"""``--capacity-retries`` on a sharded single request (issue #5567).

Split from ``test_sim_batch.py`` (file-size ceiling); the fake provisioner and
fixtures are imported from there.
"""

from __future__ import annotations

import json

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim
from klayout_tools import sim_batch as sb
from test_sim_batch import (
    _CAP_REFUSAL,
    _corner_points,
    _FakeClock,
    _FakeRunner,
    _measurements_spec,
    _provision_script,
    _SbTime,
    _sim_report,
    _status,
    _write_body,
)

_NO_CAPACITY = "error: no capacity in any of the 30 pools"


class _LaunchAwareRunner(_FakeRunner):
    """The fleet publishes no ``status.json`` for a job whose launch was
    refused; the plain fake answers every status read, which would make a
    never-launched shard look launched."""

    def __init__(self) -> None:
        super().__init__()
        self.launched: set[str] = set()
        #: With a message set, the first job launched is refused forever.
        self.stubborn_message: str | None = None
        self._stubborn_job: str | None = None

    def __call__(self, argv, timeout_s):
        joined = " ".join(argv)
        if "status.json" in joined and "cp" in argv:
            job = next(p for p in joined.split("/") if p.startswith("klt-sim-"))
            if job not in self.launched:
                self.calls.append(list(argv))
                return fake_completed(returncode=1, stderr="NoSuchKey")
        if self.stubborn_message and "launch" in argv:
            job = argv[argv.index("--job") + 1]
            self._stubborn_job = self._stubborn_job or job
            if job == self._stubborn_job:
                self.calls.append(list(argv))
                return fake_completed(returncode=1, stderr=self.stubborn_message)
        result = super().__call__(argv, timeout_s)
        if "launch" in argv and getattr(result, "returncode", 1) == 0:
            self.launched.add(argv[argv.index("--job") + 1])
        return result


def _fleet(
    tmp_path,
    monkeypatch,
    *,
    hosts=3,
    refusals=(),
    stubborn=None,
    retries=0,
    wait_s=0,
    max_concurrent=1,
    clock=None,
):
    """Run a ``hosts``-shard fleet whose first launches the fake provisioner
    refuses with the given messages; returns ``(runner, sleeps, result)``."""
    runner = _LaunchAwareRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report(corner_ids=("tt_1.800_0",))}
    monkeypatch.setattr(sb, "_run_subprocess", runner)
    sleeps: list[float] = []
    if clock is None:
        record = sleeps.append
    else:
        sleeps = clock.sleeps
        record = clock.sleep
        monkeypatch.setattr(sb, "time", _SbTime(clock))
        monkeypatch.setattr(sb.random, "random", lambda: 1.0)
    monkeypatch.setattr(sb, "_abortable_sleep", lambda abort: record)
    runner.stubborn_message = stubborn
    for message in refusals:
        runner.queue("launch", fake_completed(returncode=1, stderr=message))
    batch = {
        "bucket": "fake-bucket",
        "provision_script_path": str(_provision_script(tmp_path)),
        "poll_interval_s": 0.0,
        "max_concurrent_jobs": max_concurrent,
    }
    if wait_s:
        batch["capacity_wait_s"] = wait_s
    if retries:
        batch["capacity_retries"] = retries
    result = sb._run_batch_fleet(
        corner_points=_corner_points(hosts),
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=30.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        request={"netlist": "body.spice", "batch": batch},
        hosts=hosts,
        measurements_spec=_measurements_spec(),
        shard_fingerprint="fp-5567",
    )
    return runner, sleeps, result


def _lost(corners):
    return [bool(c.get("diagnostics")) for c in corners]


def test_a_refused_shard_is_recovered_by_one_retry_pass(tmp_path, monkeypatch):
    runner, sleeps, (corners, _, environment) = _fleet(
        tmp_path, monkeypatch, refusals=[_NO_CAPACITY], retries=1
    )

    assert _lost(corners) == [False, False, False]
    assert len(corners) == 3
    assert all(entry is not None for entry in environment["fleet"])
    assert sleeps == [5.0]  # one backoff, the base interval
    assert len(runner.argvs_matching("launch")) == 4  # 1 refused + 3 launched
    # Nothing is left for ``--resume``.
    assert not environment.get("unlaunched_shards")


def test_a_concurrency_cap_refusal_is_retried_too(tmp_path, monkeypatch):
    _, sleeps, (corners, _, _) = _fleet(
        tmp_path, monkeypatch, refusals=[_CAP_REFUSAL], retries=1
    )
    assert _lost(corners) == [False, False, False]
    assert sleeps == [5.0]


def test_a_shard_refused_on_every_pass_is_lost_after_n_passes(tmp_path, monkeypatch):
    # Shard 0 is refused on the first pass and on both retry passes.
    runner, sleeps, (corners, _, environment) = _fleet(
        tmp_path,
        monkeypatch,
        hosts=2,
        stubborn=_NO_CAPACITY,
        retries=2,
    )

    assert _lost(corners) == [True, False]
    diagnostic = corners[0]["diagnostics"][0]
    assert diagnostic["code"] == "lost_shard"
    assert "batch_no_capacity" in diagnostic["message"]
    assert sleeps == [5.0, 10.0]  # exactly two extra passes, doubling
    assert environment["fleet"][0] is None
    assert environment["unlaunched_shards"] == 1  # still resumable


def test_zero_retries_is_todays_behaviour(tmp_path, monkeypatch):
    runner, sleeps, (corners, _, environment) = _fleet(
        tmp_path, monkeypatch, hosts=2, refusals=[_NO_CAPACITY], retries=0
    )
    assert _lost(corners) == [True, False]
    assert sleeps == []
    assert len(runner.argvs_matching("launch")) == 2


def test_a_shard_lost_for_another_reason_is_never_retried(tmp_path, monkeypatch):
    runner, sleeps, (corners, _, _) = _fleet(
        tmp_path,
        monkeypatch,
        hosts=2,
        refusals=["error: create-fleet exploded for an unrelated reason"],
        retries=3,
    )
    assert _lost(corners) == [True, False]
    assert sleeps == []
    assert len(runner.argvs_matching("launch")) == 2


def test_the_retry_pass_gets_a_fresh_wait_budget(tmp_path, monkeypatch):
    clock = _FakeClock()
    # The refused shard spends the whole 100s run-wide budget waiting in pass
    # one; the retry pass must wait out a fresh budget too (without the reset
    # it would be a single no-wait attempt and total ~105s).
    _, _, (corners, _, _) = _fleet(
        tmp_path,
        monkeypatch,
        hosts=2,
        stubborn=_NO_CAPACITY,
        retries=1,
        wait_s=100,
        clock=clock,
    )
    assert _lost(corners) == [True, False]
    assert sum(clock.sleeps) > 190.0


def test_resume_after_exhausted_retries_launches_the_shard_once(tmp_path, monkeypatch):
    _fleet(tmp_path, monkeypatch, hosts=2, stubborn=_NO_CAPACITY, retries=1)
    shards = json.loads((tmp_path / "artifacts" / "batch-shards.json").read_text())[
        "shards"
    ]
    states = sorted(entry["state"] for entry in shards)
    assert states == ["collected", "submitted"]
    assert len({entry["job_id"] for entry in shards}) == 2  # no second job id


def test_negative_retries_is_refused(tmp_path):
    with pytest.raises(sim.SimError, match="--capacity-retries must be >= 0"):
        sb.resolve_capacity_retries({"capacity_retries": -1})
    with pytest.raises(sim.SimError):
        sb.resolve_capacity_retries({"capacity_retries": True})
    assert sb.resolve_capacity_retries({}) == 0


def test_cli_forwards_capacity_retries_on_a_single_request(tmp_path, monkeypatch):
    from klayout_tools.cli import main

    seen: dict = {}

    def fake_run_sim(path, **kwargs):
        seen.update(kwargs)
        raise sim.SimError("stop here")

    monkeypatch.setattr("klayout_tools.cli.sim_cmd.run_sim", fake_run_sim)
    request = tmp_path / "r.json"
    request.write_text("{}")

    main(["sim", str(request), "--capacity-retries", "2", "--format", "json"])
    assert seen["capacity_retries"] == 2
    main(["sim", str(request), "--format", "json"])
    assert seen["capacity_retries"] is None


def test_capacity_retries_override_folds_into_the_batch_config():
    from klayout_tools.sim_request_overrides import _with_capacity_retries_override

    request = {"netlist": "body.spice", "batch": {"bucket": "fake-bucket"}}
    out = _with_capacity_retries_override(request, 2)
    assert out["batch"]["capacity_retries"] == 2
    assert request["batch"] == {"bucket": "fake-bucket"}  # not mutated


def test_capacity_retries_override_unset_returns_the_request_itself():
    from klayout_tools.sim_request_overrides import _with_capacity_retries_override

    request = {"netlist": "body.spice"}
    assert _with_capacity_retries_override(request, None) is request


def test_capacity_retries_override_creates_the_batch_block():
    from klayout_tools.sim_request_overrides import _with_capacity_retries_override

    assert _with_capacity_retries_override({}, 1)["batch"] == {"capacity_retries": 1}


def test_capacity_retries_override_refuses_invalid_values():
    from klayout_tools.sim_request_overrides import _with_capacity_retries_override

    with pytest.raises(sim.SimError, match="--capacity-retries must be >= 0"):
        _with_capacity_retries_override({}, -1)
    with pytest.raises(sim.SimError, match="--capacity-retries must be >= 0"):
        _with_capacity_retries_override({}, True)


def test_interrupt_during_backoff_returns_the_partial_report(tmp_path, monkeypatch):
    def _interrupted(abort):
        def _sleep(_seconds):
            raise KeyboardInterrupt

        return _sleep

    runner = _LaunchAwareRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report(corner_ids=("tt_1.800_0",))}
    runner.stubborn_message = _NO_CAPACITY
    monkeypatch.setattr(sb, "_run_subprocess", runner)
    monkeypatch.setattr(sb, "_abortable_sleep", _interrupted)
    corners, _, environment = sb._run_batch_fleet(
        corner_points=_corner_points(2),
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=30.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        request={
            "netlist": "body.spice",
            "batch": {
                "bucket": "fake-bucket",
                "provision_script_path": str(_provision_script(tmp_path)),
                "poll_interval_s": 0.0,
                "max_concurrent_jobs": 1,
                "capacity_retries": 3,
            },
        },
        hosts=2,
        measurements_spec=_measurements_spec(),
        shard_fingerprint="fp-5567",
    )
    assert _lost(corners) == [True, False]
    assert environment["interrupted"] is True
