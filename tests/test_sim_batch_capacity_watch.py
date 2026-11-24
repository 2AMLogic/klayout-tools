"""``_CapacityWatch.start_retry_pass`` bookkeeping (issue #6535).

``finished`` runs once per shard *attempt*, so ``--capacity-retries`` passes
used to count a retried shard done again and the ``K of M shards pending``
line went to 0 and then negative. Split from ``test_sim_batch.py`` (file-size
ceiling), whose ``test_capacity_watch_*`` tests cover the rest of the class.
Every test injects ``monotonic``: the 300s heartbeat throttle would otherwise
hide the later lines.
"""

from __future__ import annotations

import re

from klayout_tools import sim_batch as sb

_REFUSED = sb.BatchError("refused", code="batch_no_capacity")
_HEARTBEAT = sb.BATCH_HEARTBEAT_INTERVAL_S


def _watch(total: int):
    now = [0.0]
    lines: list[str] = []
    watch = sb._CapacityWatch(total, monotonic=lambda: now[0], emit=lines.append)
    return watch, now, lines


def _pending(lines: list[str]) -> list[str]:
    """``"K of M"`` from each capacity-wait line, in emission order."""
    found = [re.search(r"(-?\d+ of \d+) shards pending", line) for line in lines]
    return [m.group(1) for m in found if m]


def test_capacity_watch_retry_passes_report_the_one_shard_still_pending():
    watch, now, lines = _watch(2)
    # Pass 1: shard 0 is refused and lost, shard 1 completes.
    watch.refused(0, _REFUSED)
    watch.finished(0)
    watch.finished(1)
    # Two retry passes in which shard 0 is refused again.
    for _ in range(2):
        now[0] += _HEARTBEAT
        watch.start_retry_pass([0])
        watch.refused(0, _REFUSED)
        watch.finished(0)

    assert _pending(lines) == ["2 of 2", "1 of 2", "1 of 2"]


def test_capacity_watch_retry_pass_counts_queued_shards_and_skips_siblings():
    watch, now, lines = _watch(4)
    for index in range(4):  # pass 1: shards 0-2 lost, shard 3 completed
        watch.finished(index)
    now[0] += _HEARTBEAT
    watch.start_retry_pass([0, 1, 2])  # all three queued, one worker running

    watch.refused(0, _REFUSED)  # only the running shard is waiting on a launch
    assert "3 of 4 shards pending, 1 waiting on a launch" in lines[-1]
    watch.finished(0)
    now[0] += _HEARTBEAT
    watch.refused(1, _REFUSED)
    assert "2 of 4 shards pending, 1 waiting on a launch" in lines[-1]
    watch.finished(1)
    now[0] += _HEARTBEAT
    watch.refused(2, _REFUSED)
    assert "1 of 4 shards pending, 1 waiting on a launch" in lines[-1]

    assert _pending(lines) == ["3 of 4", "2 of 4", "1 of 4"]


def test_capacity_watch_retry_pass_drops_stale_waiting_membership():
    watch, now, lines = _watch(3)
    watch.refused(0, _REFUSED)
    watch.refused(1, _REFUSED)
    assert "2 waiting on a launch" in lines[-1]
    watch.finished(2)
    now[0] += _HEARTBEAT
    # Shard 0 never reported back before the next pass began.
    watch.start_retry_pass([1])
    watch.refused(1, _REFUSED)

    # One shard outstanding, and shard 0's stale entry is not counted waiting.
    assert "1 of 3 shards pending, 1 waiting on a launch" in lines[-1]


def test_capacity_watch_retry_pass_keeps_the_heartbeat_throttle():
    watch, now, lines = _watch(2)
    watch.refused(0, _REFUSED)
    watch.finished(0)
    watch.finished(1)
    watch.start_retry_pass([0])

    now[0] += _HEARTBEAT - 1
    watch.refused(0, _REFUSED)  # same waiting count, interval not elapsed
    assert len(lines) == 1
    now[0] += 1
    watch.refused(0, _REFUSED)
    assert _pending(lines) == ["2 of 2", "1 of 2"]


def test_capacity_watch_retry_pass_leaves_progress_states_and_rate_alone():
    def drive(*, reset: bool) -> list[str]:
        watch, now, lines = _watch(3)
        watch.phase(0, "launching")
        watch.phase(0, "launched")
        watch.phase(1, "launching")
        now[0] += _HEARTBEAT
        watch.phase(1, "launched")
        watch.finished(0)
        watch.finished(1)
        watch.phase(2, "launching")
        watch.phase(2, "launch_refused")  # back to pending, as in a refusal
        watch.finished(2)
        now[0] += _HEARTBEAT
        if reset:
            watch.start_retry_pass([2])
        watch.phase(2, "launching")
        now[0] += _HEARTBEAT
        watch.phase(2, "launched")
        return lines

    with_reset = drive(reset=True)
    assert with_reset == drive(reset=False)
    assert "launch rate 0.2/min" in " ".join(with_reset)
    assert "3/3 launched" in with_reset[-1]


def test_capacity_watch_retry_pass_with_no_refused_shards_counts_all_finished():
    watch, now, lines = _watch(2)
    watch.finished(0)
    watch.finished(1)
    watch.start_retry_pass([])
    watch.refused(0, _REFUSED)
    assert _pending(lines) == ["0 of 2"]
