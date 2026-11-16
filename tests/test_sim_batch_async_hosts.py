"""``--submit-only`` with client-side ``--hosts N`` sharding (issue #5281)."""

from __future__ import annotations

import json

from klayout_tools import sim_batch as sb
from klayout_tools import sim_batch_async as sba
from test_sim_batch import (
    _ceiling_request,
    _corner_points,
    _split_runner,
    _write_body,
)


def test_submit_only_client_hosts_shards_the_submit(tmp_path, monkeypatch):
    """Issue #5281: ``hosts=3`` submits 3 shard jobs and writes the shard map."""
    runner, shard_sizes = _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)

    payload = sba.submit_only(
        request=_ceiling_request(tmp_path),
        corner_points=_corner_points(10),
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=120.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        shard_fingerprint="fp-async",
        hosts=3,
    )

    assert payload["sharded"] is True
    assert len(payload["shards"]) == 3
    assert sorted(shard_sizes) == [3, 3, 4]
    assert len(runner.argvs_matching("launch", "--apply")) == 3
    document = json.loads(open(payload["manifest_path"]).read())
    assert document["hosts"] == 3
