"""Batch-backend per-cell concurrency for ``klt characterize`` (issue #5351).

Helpers only -- no simulator. Lives apart from ``test_characterize.py`` so
that file stays under its file-size ratchet.
"""

from __future__ import annotations

import threading
import time

import pytest

from klayout_tools import characterize, characterize_pool


def test_batch_pool_size_only_parallel_for_multi_cell_batch(monkeypatch):
    monkeypatch.delenv("KLT_SIM_BACKEND", raising=False)
    size = characterize_pool.batch_pool_size
    wait = {"batch": {"capacity_wait_s": 600}}
    assert size(1, "batch", wait) == 1
    assert size(8, "local", wait) == 1
    assert size(8, None, wait) == 1
    assert size(8, "batch", wait) == characterize_pool.BATCH_CELL_CONCURRENCY
    assert size(2, None, {"backend": "batch", **wait}) == 2
    monkeypatch.setenv("KLT_SIM_BACKEND", "batch")
    assert size(3, None, wait) == 3


def test_batch_pool_size_serial_without_capacity_wait(monkeypatch):
    monkeypatch.delenv("KLT_SIM_BACKEND", raising=False)
    size = characterize_pool.batch_pool_size
    assert size(8, "batch", {}) == 1
    assert size(8, "batch", {"batch": {}}) == 1
    assert size(8, "batch", {"batch": {"capacity_wait_s": 0}}) == 1


def test_run_cells_serial_off_batch(monkeypatch):
    monkeypatch.delenv("KLT_SIM_BACKEND", raising=False)
    order = []

    def run_one(i):
        order.append(i)
        return {"i": i}

    out = characterize_pool.run_cells(run_one, 3, "local", {})
    assert [r["i"] for r in out] == [0, 1, 2]
    assert order == [0, 1, 2]


def test_run_cells_concurrently_overlaps_and_keeps_order():
    lock = threading.Lock()
    active = peak = 0

    def run_one(i):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return {"i": i}

    start = time.monotonic()
    out = characterize_pool.run_cells_concurrently(run_one, 8, 4)
    elapsed = time.monotonic() - start
    assert [r["i"] for r in out] == list(range(8))
    assert peak == 4
    assert elapsed < 0.05 * 8 * 0.75  # ~2 waves, far below serial


def test_run_cells_concurrently_failure_cancels_pending_and_lowest_index_wins():
    started = []
    gate = threading.Event()

    def run_one(i):
        started.append(i)
        if i == 2:
            raise characterize.CharacterizeError("cell 'c2': boom")
        if i == 1:
            gate.wait(1)
            raise characterize.CharacterizeError("cell 'c1': boom")
        if i == 0:
            gate.wait(1)
        return {"i": i}

    # pool of 3 -> cells 0,1,2 start; cell 2 fails fast, releasing the gate
    # after the queue has been cancelled; 3..5 never start.
    def release_later():
        time.sleep(0.1)
        gate.set()

    threading.Thread(target=release_later).start()
    with pytest.raises(characterize.CharacterizeError, match="cell 'c1'"):
        characterize_pool.run_cells_concurrently(run_one, 6, 3)
    assert sorted(started) == [0, 1, 2]
