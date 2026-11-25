"""Batch dispatch: ``options.max_workers`` rides the staged request (#5332)."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from klayout_tools import sim, sim_decks
from klayout_tools.cli import main
from test_sim_decks import (
    _FakeFleet,
    batch_deck_request,
    deck_request,
    install_fake_batch,
    run_local,
    write_request,
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


@pytest.mark.parametrize(
    ("backend", "requested", "flag", "cap", "expected"),
    [
        ("local-parallel", 8, None, 8, 8),
        ("local-parallel", 8, 2, 8, 2),
        ("local-parallel", None, None, 8, 3),
        ("local-parallel", 8, None, 4, 4),
        ("local", 8, None, 8, 1),
    ],
)
def test_deck_cli_selects_the_actual_pool(
    tmp_path, monkeypatch, capsys, backend, requested, flag, cap, expected
):
    request = deck_request(tmp_path, backend=backend)
    request["decks"] = [{"id": f"u{i}", "deck": "deck.spice"} for i in range(8)]
    if requested is not None:
        request["options"]["max_workers"] = requested
    monkeypatch.setenv(sim.MAX_WORKERS_ENV, str(cap))
    monkeypatch.setattr(sim, "_default_max_workers", lambda: 3)
    args = ["sim", str(write_request(tmp_path, request)), "--format", "json"]
    if flag is not None:
        args += ["--max-workers", str(flag)]
    with patch.object(
        sim_decks, "ThreadPoolExecutor", wraps=ThreadPoolExecutor
    ) as pool:
        assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] == 8
    assert report["environment"]["max_workers"] == expected
    if backend == "local":
        pool.assert_not_called()
    else:
        pool.assert_called_once_with(max_workers=expected)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "4"])
def test_invalid_json_count_is_refused_before_running_a_deck(tmp_path, value):
    request = deck_request(tmp_path, backend="local-parallel")
    request["options"]["max_workers"] = value
    with patch.object(sim_decks, "run_deck_unit") as run_unit:
        with pytest.raises(
            sim.SimError, match=r"options\.max_workers must be a positive integer"
        ):
            run_local(tmp_path, request)
        run_unit.assert_not_called()


@pytest.mark.parametrize(("flag", "expected"), [(None, 8), (3, 3)])
def test_staged_worker_consumes_the_effective_count(
    tmp_path, monkeypatch, flag, expected
):
    fleet = install_fake_batch(
        monkeypatch, _FakeFleet({"state": "done", "exit_code": "0"})
    )
    request = batch_deck_request(tmp_path)
    request["options"]["max_workers"] = 8
    monkeypatch.setenv(sim.MAX_WORKERS_ENV, "8")
    monkeypatch.setattr(sim, "_default_max_workers", lambda: 2)
    report = sim.run_sim(str(write_request(tmp_path, request)), max_workers=flag)
    staged = json.loads(
        next(item.content for item in fleet.spec.inputs if item.name == "request.json")
    )
    assert staged["options"]["max_workers"] == expected
    assert report["environment"]["max_workers"] == expected
