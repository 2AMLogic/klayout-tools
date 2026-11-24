"""Shard grouping in the batch convergence diagnostic."""

from test_sim_batch_hints import _SHA, _attach, _hints_in, _unit, _worker_entry


def test_sharded_run_lists_each_shard_and_collapses_identical_ones():
    corners = [_unit(f"c{i}", "singular_matrix") for i in range(4)]
    remote = {
        "fleet": [
            _worker_entry(source="none", job_id="j0"),
            _worker_entry(source="none", job_id="j1"),
            _worker_entry(source="home", sha=_SHA, job_id="j2"),
        ]
    }
    [hint] = _hints_in(_attach(corners, remote))
    assert [w["fleet_index"] for w in hint["workers"]] == [0, 1, 2]
    assert [w["ngspice_init"]["source"] for w in hint["workers"]] == [
        "none",
        "none",
        "home",
    ]
    message = hint["message"]
    assert "fleet[0], fleet[1]" in message and "fleet[2]" in message
    assert message.count(_SHA) == 1


def test_distinct_worker_launch_pins_split_otherwise_identical_shards():
    corners = [_unit("a", "singular_matrix"), _unit("b", "nonconvergence")]
    fleet = [_worker_entry(threads=pin) for pin in (1, 4)]
    [hint] = _hints_in(_attach(corners, {"fleet": fleet}))

    assert [worker["threads_per_worker"] for worker in hint["workers"]] == [1, 4]
    for index, pin in enumerate((1, 4)):
        group = hint["message"].split(f"fleet[{index}]: ", 1)[1].split(";", 1)[0]
        assert f"omp_num_threads=null, threads_per_worker={pin}" in group
