"""Batch dispatch: ``options.max_workers`` rides the staged request (#5332)."""

from __future__ import annotations

import json

import pytest

from test_sim_decks import (
    _FakeFleet,
    batch_deck_request,
    install_fake_batch,
    run_local,
    write_stub_engine,
)


@pytest.mark.parametrize("max_workers", [None, 1, 8])
def test_max_workers_rides_the_staged_request_not_the_job_shape(
    tmp_path, monkeypatch, max_workers
):
    """An explicit options.max_workers changes only the staged request's
    options -- the job command and sizing are identical."""
    write_stub_engine(tmp_path)
    fleet = install_fake_batch(
        monkeypatch, _FakeFleet({"state": "done", "exit_code": "0"})
    )
    request = batch_deck_request(tmp_path)
    if max_workers is not None:
        request.setdefault("options", {})["max_workers"] = max_workers
    run_local(tmp_path, request, outdir="batch-out")
    spec = fleet.spec
    staged = json.loads(
        next(i.content for i in spec.inputs if i.name == "request.json")
    )
    assert staged["options"].get("max_workers") == max_workers
    assert spec.cores_per_job == 8
    assert spec.timeout_seconds == 30 + 120 + 120
    assert 'klt sim "$EDA_INPUT_DIR/request.json"' in spec.cmd
    assert "--backend local-parallel --format json" in spec.cmd
