"""``--wait-for-capacity`` for ``klt characterize`` (issue #5348).

Split out of :mod:`klayout_tools.characterize` (size ratchet).
"""

from __future__ import annotations

from typing import Any


def capacity_shard(
    capacity_wait_s: float | None, backend: str | None
) -> dict[str, Any]:
    """The ``run_sim`` kwargs for ``capacity_wait_s`` (empty when omitted).

    A bad value, or a non-batch backend, is refused before any I/O, by the
    rule ``klt pex`` applies (#5280).
    """
    from .characterize import CharacterizeError
    from .pex import PexError
    from .pex_async import batch_shard_flags, validate_capacity_wait

    try:
        validate_capacity_wait(capacity_wait_s, backend)
    except PexError as exc:
        raise CharacterizeError(str(exc)) from exc
    return batch_shard_flags(capacity_wait_s=capacity_wait_s)


def with_batch_passthrough(
    sim_request: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any]:
    """Copy ``request.batch`` into ``sim_request`` (issue #5367).

    Pass-through only: ``klt sim`` validates the values (and ignores the
    block on a local run), so ``batch.capacity_wait_s`` is honored per cell.
    """
    batch = request.get("batch")
    if batch is not None:
        if not isinstance(batch, dict):
            from .characterize import CharacterizeError

            raise CharacterizeError("request.batch must be an object")
        sim_request["batch"] = dict(batch)
    return sim_request
