"""Bounded in-invocation retry of capacity-refused shards (issue #5567).

A sharded ``klt sim --backend batch`` request that exhausts
``--wait-for-capacity`` on some shards reports those shards lost
(``lost_shard`` / ``batch_no_capacity``). ``--capacity-retries N`` -- the
single-request twin of ``--batch-set``'s member retry (#3520) -- makes up to
``N`` further passes over exactly those shards, *after* every sibling has
reached a terminal outcome (the condition the refusal clears on), with
doubling backoff between passes and a fresh capacity-wait budget per pass.
The recovered shards are spliced over their lost entries by shard index, so
the report is that of a run that never lost them. Shards lost for any other
reason are never retried.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from .sim_batch_contract import BatchClientInterrupted
from .sim_remote import _lost_shard_corner

if TYPE_CHECKING:
    from .sim import CornerPoint
    from .sim_batch import _BatchFleetRun

Merged = tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]


def retry_capacity_refused(
    fleet: _BatchFleetRun,
    shards: list[list[CornerPoint]],
    merged: Merged,
    *,
    retries: int,
    measurements_spec: list[dict[str, Any]],
    max_concurrent: int | None,
) -> Merged:
    """Run up to ``retries`` retry passes and return the re-merged
    ``(corners, engine_version, remote_environment)``."""
    from .sim_set import CAPACITY_RETRY_BACKOFF_S

    corners, engine_version, environment = merged
    offsets = [0]
    for points in shards:
        offsets.append(offsets[-1] + len(points))
    fleet_entries: list[dict[str, Any] | None] = (
        list(environment["fleet"])
        if environment is not None and "fleet" in environment
        else [None] * len(shards)
    )
    attempt = 0
    while attempt < retries and fleet.capacity_refused and not fleet.abort.is_set():
        attempt += 1
        try:
            fleet.abortable_sleep(CAPACITY_RETRY_BACKOFF_S * 2 ** (attempt - 1))
            refused = fleet.start_retry_pass()
            outcomes = _run_pass(fleet, shards, refused, max_concurrent)
        except (KeyboardInterrupt, BatchClientInterrupted) as exc:
            fleet.on_interrupt(exc)
            break
        for index, (result, error) in outcomes.items():
            lo, hi = offsets[index], offsets[index + 1]
            if result is None:
                corners[lo:hi] = [
                    _lost_shard_corner(p, measurements_spec, f"shard lost: {error}")
                    for p in shards[index]
                ]
                continue
            shard_corners, shard_engine, shard_remote = result
            corners[lo:hi] = shard_corners
            fleet_entries[index] = shard_remote
            if shard_engine is not None and engine_version is None:
                engine_version = shard_engine
    if any(entry is not None for entry in fleet_entries):
        environment = {**(environment or {}), "fleet": fleet_entries}
    return corners, engine_version, environment


def _run_pass(
    fleet: _BatchFleetRun,
    shards: list[list[CornerPoint]],
    refused: list[int],
    max_concurrent: int | None,
) -> dict[int, tuple[Any, str | None]]:
    workers = max(1, len(refused))
    if max_concurrent is not None:
        workers = min(workers, max(1, max_concurrent))
    futures: dict[int, Future[Any]] = {}
    outcomes: dict[int, tuple[Any, str | None]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index in refused:
            futures[index] = pool.submit(fleet.retry_shard_points, shards[index])
        try:
            for index, future in futures.items():
                outcomes[index] = _outcome(future)
        except (KeyboardInterrupt, BatchClientInterrupted):
            # Keep what already finished; the caller records the interrupt.
            fleet.abort.set()
            for index, future in futures.items():
                if index not in outcomes and future.done():
                    outcomes[index] = _outcome(future)
            raise
    return outcomes


def _outcome(future: Future[Any]) -> tuple[Any, str | None]:
    try:
        return future.result(), None
    except Exception as exc:  # noqa: BLE001 - "shard never returned"
        return None, str(exc)
