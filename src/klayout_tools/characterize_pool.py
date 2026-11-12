"""Per-cell concurrency for ``klt characterize`` on the ``batch`` backend
(issue #5351).

Split out of :mod:`klayout_tools.characterize` so the pool logic (and its
tests) do not grow that module past its file-size ratchet. Pure library: no
printing, no CLI concerns.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import FIRST_EXCEPTION, Future, ThreadPoolExecutor, wait
from typing import Any

from . import sim, sim_batch

#: Cells characterized at once on the ``batch`` backend (issue #5351). Each
#: cell blocks on one fleet job (~minutes), so serial wall-clock is
#: cells x per-cell time; the fleet's own vCPU budget and
#: ``batch.capacity_wait_s`` bound the real concurrency. Deliberately not a
#: user-facing knob.
BATCH_CELL_CONCURRENCY = 4


def batch_pool_size(
    n_cells: int,
    backend: str | None,
    request: dict[str, Any],
    capacity_wait_s: float | None = None,
) -> int:
    """Worker count for the per-cell loop: >1 only for a multi-cell run whose
    backend resolves to ``batch`` **and** that opts into waiting out fleet
    capacity (``--wait-for-capacity`` or ``batch.capacity_wait_s`` > 0).
    Without that wait a burst of concurrent submits hits
    ``batch_no_capacity`` and fails (issue #5348), so the default stays serial. Local/other backends stay serial so a
    shared host is never oversubscribed."""
    if n_cells <= 1:
        return 1
    resolved_backend, _ = sim.resolve_backend(backend, request)
    if resolved_backend != "batch":
        return 1
    batch_spec = request.get("batch") or {}
    if (capacity_wait_s or 0) <= 0 and sim_batch.resolve_capacity_wait_s(
        batch_spec
    ) <= 0:
        return 1
    return min(BATCH_CELL_CONCURRENCY, n_cells)


def run_cells_concurrently(
    run_one: Callable[[int], dict[str, Any]], n_cells: int, pool_size: int
) -> list[dict[str, Any]]:
    """Run ``run_one(i)`` for every cell in a bounded pool; return results in
    cell order. On failure, cancel cells not yet started and re-raise the
    lowest-index failing cell's error (deterministic, matches serial order)."""
    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        futures: list[Future[dict[str, Any]]] = [
            pool.submit(run_one, index) for index in range(n_cells)
        ]
        wait(futures, return_when=FIRST_EXCEPTION)
        for future in futures:
            future.cancel()  # no-op for running/finished; drops queued cells
        # Let running cells finish (pool exit) before deciding, so the
        # lowest-index failure wins deterministically.
    for future in futures:
        if future.cancelled():
            continue
        exc = future.exception()
        if exc is not None:
            raise exc
    return [future.result() for future in futures]


def run_cells(
    run_one: Callable[[int], dict[str, Any]],
    n_cells: int,
    backend: str | None,
    request: dict[str, Any],
    capacity_wait_s: float | None = None,
) -> list[dict[str, Any]]:
    """Run every cell, serially unless the backend is ``batch`` and there is
    more than one cell; results are in cell order either way."""
    pool_size = batch_pool_size(n_cells, backend, request, capacity_wait_s)
    if pool_size <= 1:
        return [run_one(index) for index in range(n_cells)]
    return run_cells_concurrently(run_one, n_cells, pool_size)
