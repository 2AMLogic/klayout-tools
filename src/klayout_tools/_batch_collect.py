"""The *collect* half of the batch lifecycle, shared by ``netlist_batch``,
``extract_batch`` and ``place_and_route_batch``: recover the runner's own
error from a failed job's collected ``outputs/``, collect them, and read the
collected ``report.json``.

The submit half lives in :mod:`._batch_common`; this module is its sibling
rather than a part of it because ``_batch_common`` sits at the file-size
ratchet's hard limit. Like ``_batch_common`` it has **no** module-level
``sim_batch`` import (``sim_batch`` imports ``_batch_common``); every helper
resolves it function-locally.

Error strings, ``code`` and ``details`` keys built here are part of the JSON
contract (``docs/json-contract.md``).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any, TypeVar

from . import _batch_common
from .sim_exit_reason import status_exit_code

_E = TypeVar("_E", bound=Exception)

#: Bytes of ``stderr.log`` quoted back when the job left no klt envelope.
REMOTE_STDERR_TAIL_BYTES = 2048

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def runner_error(collected: str) -> tuple[dict[str, Any] | None, str]:
    """The runner klt's own error envelope (``message`` and maybe ``code``):
    from ``report.json`` (where the preflight writes its rejection), else
    from the tail of ``stderr.log`` (where the runner's klt writes one)."""
    from . import sim_batch

    note = ""
    try:
        kind, error = sim_batch._classify_collected_report(
            sim_batch.read_collected_report(collected)
        )
        if kind == "error":
            return error, ""
    except sim_batch.BatchError as exc:
        note = str(exc)
    error, stderr_note = sim_batch._read_stderr_envelope(collected)
    if error is not None:
        return error, ""
    return None, "; ".join(item for item in (note, stderr_note) if item)


def remote_stderr_tail(collected: str) -> str:
    """The last :data:`REMOTE_STDERR_TAIL_BYTES` of the collected
    ``stderr.log`` as sanitized text: invalid UTF-8 replaced, ANSI escapes and
    control characters (other than newline and tab) dropped. ``""`` when the
    file is missing or empty. Only used when no klt envelope was recovered."""
    from . import sim_batch

    path = os.path.join(collected, sim_batch.BATCH_STDERR_FILENAME)
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - REMOTE_STDERR_TAIL_BYTES))
            raw = handle.read()
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    return _CONTROL_CHARS.sub("", _ANSI_ESCAPE.sub("", text)).strip()


def collect(
    config: Any,
    job_id: str,
    status: dict[str, Any],
    collected: str,
    *,
    runner: Any,
    err_cls: type[_E],
    no_fallback: str,
    job_failure: Callable[..., _E],
) -> None:
    """Collect ``outputs/``. A job that did not finish ``done`` raises
    ``err_cls`` carrying the runner's own error, else the tail of its
    ``stderr.log`` (the ``--progress`` lines show how far it got).

    ``job_failure`` is :func:`._batch_common.job_failure` with the
    caller's ``err_cls`` / ``no_fallback`` pre-bound, called with the
    remaining keyword arguments."""
    from . import sim_batch

    done = status.get("state") == "done" and (status_exit_code(status) in (0, None))
    if done:
        try:
            sim_batch.collect_outputs(config, job_id, collected, runner=runner)
        except sim_batch.BatchError as exc:
            raise _batch_common.transport_error(
                job_id, exc, err_cls, no_fallback
            ) from exc
        return
    error: dict[str, Any] | None = None
    stderr_tail = ""
    try:
        sim_batch.collect_outputs(config, job_id, collected, runner=runner)
        error, note = runner_error(collected)
        if error is None:
            stderr_tail = remote_stderr_tail(collected)
    except sim_batch.BatchError as exc:
        note = str(exc)
    raise job_failure(
        job_id=job_id,
        status=status,
        runner_error=error,
        note=note,
        stderr_tail=stderr_tail,
        harness_log=sim_batch.report_job_uri(config, job_id).rstrip("/")
        + "/harness.log",
    )


def read_collected_unit_report(
    collected: str,
    job_id: str,
    *,
    err_cls: type[_E],
    no_fallback: str,
    validate: Callable[[dict[str, Any]], bool],
    what: str,
) -> dict[str, Any]:
    """The collected ``report.json``, or ``err_cls`` when it is missing,
    unparseable, the runner klt's error envelope, or not the report this
    command produces (``validate(report)`` false; ``what`` names the expected
    report in the message)."""
    from . import sim_batch

    ctor: Any = err_cls
    try:
        report = sim_batch.read_collected_report(collected)
    except sim_batch.BatchError as exc:
        raise ctor(
            f"batch job {job_id}: {exc} {no_fallback}",
            code=_batch_common.BATCH_JOB_FAILED_CODE,
            job_id=job_id,
        ) from exc
    kind, error = sim_batch._classify_collected_report(report)
    if kind == "error" and error is not None:
        raise ctor(
            f"batch job {job_id}: the runner's klt reported: "
            f"{error['message']} {no_fallback}",
            code=error.get("code") or _batch_common.BATCH_JOB_FAILED_CODE,
            job_id=job_id,
        )
    if not validate(report):
        raise ctor(
            f"batch job {job_id}: the collected {sim_batch.BATCH_REPORT_FILENAME} "
            f"is not {what} {no_fallback}",
            code=_batch_common.BATCH_JOB_FAILED_CODE,
            job_id=job_id,
        )
    return report
