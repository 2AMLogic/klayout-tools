"""The whole-job ceiling assessment of a sharded request (issue #8011).

``remote.hosts`` runs one job per shard, each sized from its own units, but
the planner assessed the whole request as one job: three corners at 1800 s
are bound at 5520 s, so ``--submit-only`` refused an ``enforce`` request
whose ``remote.hosts: 3`` shards it into three 1920 s jobs before any S3
write. The planner now receives the resolved host count, so the assessment
describes the largest shard -- the bound that job's own ``job.json``
``timeout_seconds`` carries -- on ``--submit-only`` as on ``--dry-run`` and
the ``--batch-set`` preflight.
"""

from __future__ import annotations

import pytest

from klayout_tools import sim_batch as sb
from klayout_tools import sim_batch_async as sba
from klayout_tools.sim import SimError
from klayout_tools.sim_remote import _shard_corner_points
from test_sim_batch import (
    _ceiling_request,
    _corner_points,
    _split_runner,
    _write_body,
)

TIMEOUT_S = 1800.0


def _submit_sharded(tmp_path, monkeypatch, *, hosts, **batch):
    runner, shard_sizes = _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    request = _ceiling_request(tmp_path, max_job_seconds=3600, **batch)
    payload = sba.submit_only(
        request=request,
        corner_points=_corner_points(3),
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=TIMEOUT_S,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        shard_fingerprint="fp-8011",
        hosts=hosts,
    )
    return payload, runner, shard_sizes


def test_submit_only_assesses_the_jobs_it_launches(tmp_path, monkeypatch):
    payload, runner, shard_sizes = _submit_sharded(
        tmp_path, monkeypatch, hosts=3, job_ceiling_check="enforce"
    )

    assert payload["sharded"] is True
    assert sorted(shard_sizes) == [1, 1, 1]
    assert len(runner.argvs_matching("launch", "--apply")) == 3
    ceiling = payload["job_ceiling"]
    assert ceiling["job_bound_s"] == 1920.0
    assert ceiling["job_unit_count"] == 1
    assert ceiling["unit_count"] == payload["unit_count"] == 3
    assert ceiling["exceeds_ceiling"] is False
    assert ceiling["effective_hosts"] == 3


def test_submit_only_refuses_an_overflowing_shard_before_any_s3_write(
    tmp_path, monkeypatch
):
    with pytest.raises(SimError) as excinfo:
        _submit_sharded(tmp_path, monkeypatch, hosts=2, job_ceiling_check="enforce")

    assert excinfo.value.code == sb.BATCH_JOB_CEILING_CODE
    assert "batch job bound 3720s (options.timeout_s 1800s x 2 units" in str(
        excinfo.value
    )


def test_submit_only_warns_with_the_largest_shards_bound(tmp_path, monkeypatch):
    payload, _, shard_sizes = _submit_sharded(tmp_path, monkeypatch, hosts=2)

    assert sorted(shard_sizes) == [1, 2]
    ceiling = payload["job_ceiling"]
    assert ceiling["job_bound_s"] == 3720.0
    assert ceiling["exceeds_ceiling"] is True
    assert ceiling["recommended_hosts"] == 3
    assert ceiling["effective_hosts"] == 2


@pytest.mark.parametrize(
    ("units", "hosts"), [(3, 2), (3, 3), (63, 2), (63, 3), (10, 7), (7, 10)]
)
def test_bound_is_the_one_the_largest_shard_job_really_carries(
    tmp_path, monkeypatch, units, hosts
):
    """Checked against the shard engine's real split, as
    ``test_sim_batch.test_recommended_hosts_keeps_every_shard_within_the_ceiling``
    checks the recommendation."""
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    config = sb._resolve_batch_config(
        _ceiling_request(tmp_path),
        corner_count=units,
        timeout_s=TIMEOUT_S,
        check_runner=False,
    )
    assessment = sb._assess_job_ceiling(
        unit_count=units, per_unit_timeout_s=TIMEOUT_S, config=config, hosts=hosts
    )

    shards = _shard_corner_points(_corner_points(units), hosts)
    sizes = [len(shard) for shard in shards if shard]
    assert assessment["job_unit_count"] == max(sizes)
    assert assessment["job_bound_s"] == max(
        sb._default_batch_job_timeout_s(size, TIMEOUT_S) for size in sizes
    )
    assert assessment["unit_count"] == units
