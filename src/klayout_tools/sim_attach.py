"""``--attach`` routing for ``klt sim`` (issue #2978; ``--collect-partial``
split out of :mod:`sim` to keep it under its size ceiling)."""

from __future__ import annotations

from typing import Any

from .sim_errors import SimError


def apply_attach(
    engine_kwargs: dict[str, Any],
    attach_job_id: str | None,
    backend: str,
    hosts: int,
    submit_only: bool = False,
    collect_partial: bool = False,
) -> None:
    """Issue #2978: route ``--attach <job_id>`` to the batch backend. Only the
    single-job batch path has a job id to re-attach to."""
    if collect_partial and (
        attach_job_id is None or not attach_job_id.endswith(".json")
    ):
        raise SimError(
            "--collect-partial requires --attach <batch-shards.json> (a shard "
            "map); it reads each shard once instead of waiting",
            code="collect_partial_requires_attach",
        )
    if attach_job_id is None:
        return
    if backend != "batch" or hosts != 1:
        raise SimError(
            "--attach requires backend 'batch' with a single job "
            "(hosts = 1); a sharded run has one job id per shard"
        )
    if submit_only:
        raise SimError(
            "--attach and --submit-only are mutually exclusive: attach "
            "resumes an already-submitted job, submit-only launches a new one"
        )
    engine_kwargs["attach_job_id"] = attach_job_id
    if collect_partial:
        engine_kwargs["collect_partial"] = True
