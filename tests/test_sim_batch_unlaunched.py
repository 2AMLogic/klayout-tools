"""Tests for the machine-readable "incomplete, resumable" signal that
`_run_batch_fleet` adds to `environment.remote` (issue #4856). Split out of
`test_sim_batch.py` to keep that file under its size ceiling."""

from __future__ import annotations

from klayout_tools import sim_batch as sb
from test_sim_batch import _shard_resume_fixture


def test_unlaunched_shards_are_a_machine_readable_resumable_signal(
    tmp_path, monkeypatch
):
    """Issue #4856: a capacity-refused shard (capacity_wait_s 0) surfaces
    ``unlaunched_shards`` / ``resumable`` in ``environment`` without any new
    exit code or changed corner message."""
    _, fleet, manifest_path = _shard_resume_fixture(tmp_path, monkeypatch)
    real_launch = sb.launch_job
    seen: list[str] = []

    def _launch(config, job_id, **kwargs):
        if seen:
            raise sb.BatchError("refused", code=sb.BATCH_NO_CAPACITY_CODE)
        seen.append(job_id)
        return real_launch(config, job_id, **kwargs)

    monkeypatch.setattr(sb, "launch_job", _launch)
    _, _, environment = fleet()
    assert environment["unlaunched_shards"] == 1
    assert environment["resumable"] is True
    assert environment["shard_map"] == str(manifest_path)


def test_clean_sharded_run_has_no_unlaunched_signal(tmp_path, monkeypatch):
    _, fleet, _ = _shard_resume_fixture(tmp_path, monkeypatch)
    _, _, environment = fleet()
    assert "unlaunched_shards" not in environment
    assert "resumable" not in environment


def test_unrun_units_are_a_resumable_signal():
    """Issue #5364: lost/interrupted shards carry the same additive signal."""
    from klayout_tools import _batch_common

    env, hint = _batch_common.with_unrun_signal({"job": "x"}, 3, "/o/batch-shards.json")
    assert env == {
        "job": "x",
        "unrun_units": 3,
        "resumable": True,
        "shard_map": "/o/batch-shards.json",
    }
    assert hint is True


def test_unrun_signal_absent_when_nothing_unrun_or_no_state():
    from klayout_tools import _batch_common

    base = {"job": "x"}
    assert _batch_common.with_unrun_signal(base, 0, "/p") == (base, False)
    assert _batch_common.with_unrun_signal(base, 2, None) == (base, False)


def test_unrun_signal_mixed_with_unlaunched_keeps_one_hint():
    from klayout_tools import _batch_common

    base = {"unlaunched_shards": 1, "resumable": True, "shard_map": "/m"}
    env, hint = _batch_common.with_unrun_signal(base, 4, "/other")
    assert env["shard_map"] == "/m" and env["unrun_units"] == 4
    assert hint is False


def _wired(tmp_path, *, manifest, checkpoint, unrun=2):
    from klayout_tools import _batch_common
    from klayout_tools.sim_batch import BATCH_SHARDS_FILENAME

    if manifest:
        (tmp_path / BATCH_SHARDS_FILENAME).write_text("{}")
    ckpt = str(tmp_path / "ckpt.json") if checkpoint else None
    return _batch_common.apply_unrun_signal({"job": "x"}, unrun, str(tmp_path), ckpt)


def test_apply_unrun_signal_prefers_manifest_and_hints_once(tmp_path, capsys):
    env = _wired(tmp_path, manifest=True, checkpoint=True)
    assert env["unrun_units"] == 2 and env["resumable"] is True
    assert env["shard_map"] == str(tmp_path / "batch-shards.json")
    err = capsys.readouterr().err
    assert err.count("rerun with --resume") == 1
    assert "2 unit(s) never ran" in err and env["shard_map"] in err


def test_apply_unrun_signal_checkpoint_only(tmp_path, capsys):
    env = _wired(tmp_path, manifest=False, checkpoint=True)
    assert env["shard_map"] == str(tmp_path / "ckpt.json")
    assert "rerun with --resume" in capsys.readouterr().err


def test_apply_unrun_signal_no_state_or_nothing_unrun(tmp_path, capsys):
    assert _wired(tmp_path, manifest=False, checkpoint=False) == {"job": "x"}
    assert _wired(tmp_path, manifest=True, checkpoint=True, unrun=0) == {"job": "x"}
    assert capsys.readouterr().err == ""
