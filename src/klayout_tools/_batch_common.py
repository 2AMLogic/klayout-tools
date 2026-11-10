"""Helpers shared by the batch-offload modules (``netlist_batch``,
``extract_batch``, ``place_and_route_batch``, ``sim_batch``,
``signoff_generator_batch``).

Error strings and ``details`` keys built here are part of the JSON contract
(``docs/json-contract.md``); change them here, once.
"""

from __future__ import annotations

import os
import tarfile
import uuid
from collections.abc import Sequence
from typing import Any

BATCH_TREE_ROOT_FILENAME = "tree_root.txt"
BATCH_JOB_FAILED_CODE = "batch_job_failed"
BATCH_JOB_TIMEOUT_CODE = "batch_job_timeout"
BATCH_POLL_TIMEOUT_CODE = "batch_poll_timeout"


def make_job_id(kind: str) -> str:
    """A fresh ``klt-<kind>-<12 hex>`` job id, in the ``[A-Za-z0-9._-]+``
    alphabet the launch script accepts (it becomes an S3 prefix and an EC2
    tag), recognisable by ``kind`` in the bucket."""
    return f"klt-{kind}-{uuid.uuid4().hex[:12]}"


def scrub_tarinfo(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """No submitting-host user or group names, and a zeroed mtime so the same
    input tree always yields the same archive bytes."""
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    return info


def read_runner_roots(collected: str) -> list[str]:
    """The runner-side stage roots recorded in ``tree_root.txt``: absolute
    paths without a trailing ``/``, longest first so a resolved path is
    rewritten before a prefix of it."""
    try:
        with open(
            os.path.join(collected, BATCH_TREE_ROOT_FILENAME), encoding="utf-8"
        ) as handle:
            lines = [line.strip().rstrip("/") for line in handle.read().splitlines()]
    except OSError:
        return []
    return sorted(
        {line for line in lines if line.startswith("/")}, key=len, reverse=True
    )


def job_failure_parts(
    *,
    job_id: str,
    state: str,
    exit_code: int | None,
    detail: str,
    runner_error: dict[str, Any] | None,
    note: str,
    stderr_tail: str,
    harness_log: str | None,
) -> tuple[str, str, dict[str, Any]]:
    """``(message, code, details)`` for a job that did not finish ``done``.

    ``message`` has no trailing "no local fallback" clause; the caller appends
    its own. ``details`` is empty when the runner's own klt error was
    recovered. ``harness_log`` is the full-log location, or ``None`` to point
    at the job's S3 prefix generically.
    """
    head = f"batch job {job_id} finished {state!r}" + (
        f" (exit {exit_code})" if exit_code is not None else ""
    )
    if detail:
        head += f" ({detail})"
    code: str | None
    if runner_error is not None:
        message = f"{head}: the runner's klt reported: {runner_error['message']}"
        code = runner_error.get("code")
    else:
        message = f"{head} without a usable report"
        if note:
            message += f" [output recovery: {note}]"
        message += (
            f" -- full log: {harness_log}"
            if harness_log
            else " -- see harness.log under the job's S3 prefix"
        )
        if stderr_tail:
            message += f"\nlast of the runner's stderr.log:\n{stderr_tail}"
        code = None
    if code is None:
        code = BATCH_JOB_TIMEOUT_CODE if state == "timeout" else BATCH_JOB_FAILED_CODE
    details: dict[str, Any] = {}
    if runner_error is None:
        details["remote_exit_code"] = exit_code
        details["remote_state"] = state
        if harness_log:
            details["harness_log"] = harness_log
        if stderr_tail:
            details["remote_stderr_tail"] = stderr_tail
    return message, code, details


def resumable(
    environment: dict[str, Any] | None, manifest: Any
) -> dict[str, Any] | None:
    """``environment`` plus the additive "incomplete, resumable" signal
    (issue #4856) when ``manifest`` (a shard manifest) still has shards that
    were submitted but never launched; ``environment`` unchanged otherwise."""
    count = manifest.unlaunched() if manifest is not None else 0
    if not count:
        return environment
    return {
        **(environment or {}),
        "unlaunched_shards": count,
        "resumable": True,
        "shard_map": str(manifest.path),
    }


def resolve_backend_value(
    flag: str | None,
    env_name: str,
    supported: Sequence[str],
    err_cls: type[Exception],
) -> str:
    """``--backend`` > ``$<env_name>`` > ``"local"``; an unknown value raises
    ``err_cls`` naming the flag or variable it came from."""
    if flag is not None:
        value, source = flag, "--backend"
    else:
        value = os.environ.get(env_name, "").strip() or "local"
        source = f"${env_name}"
    if value not in supported:
        raise err_cls(f"{source} must be one of {', '.join(supported)} (got {value!r})")
    return value


def resolve_batch_config(
    pdk: str, timeout_s: float, err_cls: type[Exception], no_fallback: str
) -> Any:
    """:func:`sim_batch._resolve_batch_config` fed a synthetic one-unit
    request, so the fleet config resolves exactly as for ``klt sim``. Its
    ``SimError``/``BatchError`` becomes ``err_cls``."""
    from . import sim_batch
    from .sim import SimError

    try:
        return sim_batch._resolve_batch_config(
            {"models": {"pdk": pdk}}, corner_count=1, timeout_s=timeout_s
        )
    except (SimError, sim_batch.BatchError) as exc:
        raise err_cls(
            f"batch backend: {exc} {no_fallback}", code=getattr(exc, "code", None)
        ) from exc


def transport_error(
    job_id: str, exc: Any, err_cls: type[Exception], no_fallback: str
) -> Exception:
    """``err_cls`` for a ``sim_batch.BatchError`` raised while submitting,
    launching or polling; the poll timeout says the job may still finish."""
    from . import sim_batch

    if isinstance(exc, sim_batch.BatchPollTimeout):
        return err_cls(
            f"batch job {job_id}: {exc} -- it may still finish on the fleet, but "
            f"no netlist was collected {no_fallback}",
            code=exc.code or BATCH_POLL_TIMEOUT_CODE,
            job_id=job_id,
        )
    return err_cls(
        f"batch job {job_id}: {exc} {no_fallback}", code=exc.code, job_id=job_id
    )


def job_failure(
    *,
    job_id: str,
    status: dict[str, Any],
    runner_error: dict[str, Any] | None,
    note: str,
    stderr_tail: str,
    harness_log: str | None,
    err_cls: type[Exception],
    no_fallback: str,
) -> Exception:
    """:func:`job_failure_parts` as an ``err_cls`` with ``no_fallback``
    appended to the message."""
    from . import sim_batch

    message, code, details = job_failure_parts(
        job_id=job_id,
        state=str(status.get("state") or ""),
        exit_code=sim_batch._status_exit_code(status),
        detail=str(status.get("detail") or "").strip(),
        runner_error=runner_error,
        note=note,
        stderr_tail=stderr_tail,
        harness_log=harness_log,
    )
    return err_cls(
        f"{message} {no_fallback}", code=code, job_id=job_id, details=details or None
    )
