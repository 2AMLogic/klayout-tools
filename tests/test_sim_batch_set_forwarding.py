"""``klt sim --batch-set`` forwarding of sharding / capacity-wait options
(issue #4889); split from test_sim_batch.py to keep it small."""

from test_sim_batch import _fake_report, _write_set


def _patch_set_run(monkeypatch, fake_run):
    from klayout_tools import sim_set

    monkeypatch.setattr(
        sim_set.run_sim_set,
        "__kwdefaults__",
        {**sim_set.run_sim_set.__kwdefaults__, "run": fake_run},
    )


def test_cli_batch_set_forwards_sharding_and_capacity_wait(
    tmp_path, monkeypatch, capsys
):
    from klayout_tools.cli import main

    seen: list[tuple] = []

    def fake_run(path, **kwargs):
        seen.append(
            (
                kwargs.get("max_units_per_job"),
                kwargs.get("max_concurrent_jobs"),
                kwargs.get("capacity_wait_s"),
            )
        )
        return _fake_report()

    _patch_set_run(monkeypatch, fake_run)
    paths = _write_set(tmp_path, 2)

    code = main(
        [
            "sim", "--batch-set", *paths,
            "--max-units-per-job", "5",
            "--max-concurrent-jobs", "3",
            "--wait-for-capacity", "30",
            "--format", "json",
        ]
    )  # fmt: skip

    assert code == 0
    assert seen == [(5, 3, 30.0), (5, 3, 30.0)]


def test_cli_batch_set_leaves_sharding_unset_when_omitted(
    tmp_path, monkeypatch, capsys
):
    from klayout_tools.cli import main

    seen: list[tuple] = []

    def fake_run(path, **kwargs):
        seen.append(
            (
                kwargs.get("max_units_per_job"),
                kwargs.get("max_concurrent_jobs"),
                kwargs.get("capacity_wait_s"),
            )
        )
        return _fake_report()

    _patch_set_run(monkeypatch, fake_run)
    paths = _write_set(tmp_path, 1)

    assert main(["sim", "--batch-set", *paths, "--format", "json"]) == 0
    assert seen == [(None, None, None)]
