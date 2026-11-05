"""Explicit ``max_units_per_job`` above the serial ceiling bound (issue #4982)."""

from types import SimpleNamespace

import klayout_tools.sim_batch as sb
from test_sim_batch import (
    _ceiling_request,
    _corner_points,
    _measurements_spec,
    _split_runner,
    _write_body,
)


def test_max_units_per_job_over_the_ceiling_limit_is_honored_with_warning(
    tmp_path, monkeypatch, capsys
):
    """The ceiling bound is a serial upper bound, so an explicit cap whose
    shards exceed it is honored (and warned about), not refused."""
    _, shard_sizes = _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)

    sb._run_batch(
        corner_points=_corner_points(63),
        netlist_path=str(_write_body(tmp_path)),
        models_lib=None,
        analysis={"kind": "tran", "args": "1n 1u"},
        measurements_spec=_measurements_spec(),
        timeout_s=120.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        max_workers=None,
        request=_ceiling_request(tmp_path, max_units_per_job=40),
    )  # 120 x 32 + 120 > 3600

    assert sorted(shard_sizes) == [31, 32]
    err = capsys.readouterr().err
    assert "serial upper bound" in err
    assert "max_units_per_job 40" in err


def test_plan_job_split_250_units_cap_150_with_3000s_timeout(capsys):
    config = SimpleNamespace(
        max_job_seconds=3600.0,
        max_job_seconds_source="default",
        job_ceiling_check="warn",
        max_units_per_job=150,
    )
    assessment = sb._assess_job_ceiling(
        unit_count=250, per_unit_timeout_s=3000.0, config=config
    )
    assert assessment["max_units_per_job"] == 1
    assert sb._plan_job_split(assessment, config) == 2  # 2 shards of 125
    assert "serial upper bound" in capsys.readouterr().err
