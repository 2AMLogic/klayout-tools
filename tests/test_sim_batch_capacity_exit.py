"""The fleet uses exit 75 for temporary capacity ceilings."""

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim_batch as sb
from test_sim_batch import _CAP_REFUSAL, _config, _FakeClock, _FakeRunner

PROFILE_REFUSAL = (
    "error: 64 vCPU already running in the simlong slice + 1 instance(s) "
    "of up to 32 vCPU exceeds the simlong slice BATCH_SIMLONG_MAX_VCPU=64"
)


@pytest.mark.parametrize("message", [PROFILE_REFUSAL, "a future capacity diagnostic"])
def test_capacity_exit_retries_same_job(tmp_path, message):
    runner, clock = _FakeRunner(), _FakeClock()
    runner.queue("launch", fake_completed(returncode=75, stderr=message))
    sb.launch_job(
        _config(tmp_path, capacity_wait_s=600),
        "job-1",
        runner=runner,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        jitter=lambda: 0.0,
    )
    first, second = runner.argvs_matching("launch")
    assert first == second
    assert first[first.index("--job") + 1] == "job-1"
    assert clock.sleeps == [15.0]


@pytest.mark.parametrize(
    ("status", "message", "code"),
    [
        (75, PROFILE_REFUSAL, "batch_no_capacity"),
        (75, "a future capacity diagnostic", "batch_no_capacity"),
        (75, _CAP_REFUSAL, "batch_concurrency_cap"),
        (1, "unrelated failure", None),
    ],
)
def test_capacity_exit_without_wait(tmp_path, status, message, code):
    runner, clock = _FakeRunner(), _FakeClock()
    runner.queue("launch", fake_completed(returncode=status, stderr=message))
    with pytest.raises(sb.BatchError) as exc:
        sb.launch_job(
            _config(tmp_path),
            "job-1",
            runner=runner,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
    assert exc.value.code == code
    assert len(runner.argvs_matching("launch")) == 1
    assert clock.sleeps == []
