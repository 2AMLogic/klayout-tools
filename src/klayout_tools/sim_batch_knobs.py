"""The ``request.batch`` knob resolvers, moved out of
:mod:`klayout_tools.sim_batch` (issue #6437), plus the request-only knob
validator the batch dry run runs before its host-dependent configuration
resolution.

Split out of :mod:`klayout_tools.sim_batch` to keep that module under its
per-file size ratchet (same shape as :mod:`klayout_tools.sim_batch_warn`
and :mod:`klayout_tools.sim_batch_job_timeout`, issue #5544's precedent
for a behaviour-preserving move). The bodies are unchanged except for two
changes to ``sim_batch`` that landed after this move was branched and are
carried forward here: the three finiteness guards (``max_job_seconds``,
``capacity_wait_s``, ``interrupted_grace_s``) go through
:func:`~klayout_tools._jsonnum.is_finite_number` (issue #6115), and
:class:`~klayout_tools.sim_errors.SimError` is imported once at module level
rather than inside each function.

Every resolver here reads only ``request.batch`` (plus, for
:func:`_resolve_max_job_seconds`, the ``$KLT_BATCH_MAX_JOB_SECONDS``
environment variable and the ``batch-fleet.env`` beside the provision
script, which are host config) and raises
:class:`~klayout_tools.sim.SimError` before any S3 write, never warns,
never touches the network -- which is what makes
:func:`_validate_batch_request_knobs` safe to call twice: once on the dry
run, before the host-dependent resolution, and again inside
:mod:`klayout_tools.sim_batch._resolve_batch_config`.

This module does not import :mod:`klayout_tools.sim_batch` at runtime
(only for typing), so :mod:`klayout_tools.sim_batch` can import it freely.
"""

from __future__ import annotations

import os
from typing import Any

from ._jsonnum import is_finite_number, is_int
from .sim_batch_contract import (
    BATCH_MAX_CONCURRENT_CODE,
    DEFAULT_BATCH_MAX_JOB_SECONDS,
    DEFAULT_INTERRUPTED_GRACE_S,
    FLEET_CONFIG_FILENAME,
    FLEET_MAX_JOB_SECONDS_KEY,
    MAX_JOB_SECONDS_ENV,
    RUNNER_VERSION_CHECK_MODES,
)
from .sim_batch_job_timeout import (
    _resolve_job_ceiling_check,
    _resolve_max_units_per_job,
    resolve_job_timeout_s,
)
from .sim_errors import SimError


def _resolve_max_job_seconds(
    batch_spec: dict[str, Any], fleet: dict[str, str]
) -> tuple[float, str]:
    """The fleet's whole-job ceiling and its provenance (issue #2833).

    Resolution order: ``request.batch.max_job_seconds`` (``"request"``),
    ``$KLT_BATCH_MAX_JOB_SECONDS`` (``"env"``), ``BATCH_MAX_JOB_SECONDS`` in
    the ``batch-fleet.env`` beside the provision script (``"fleet_config"``,
    the value 2am's ``launch`` tags the instance with), else
    :data:`DEFAULT_BATCH_MAX_JOB_SECONDS` (``"default"``, the harness's own
    fallback). A present but not positive-finite number is a SimError
    naming its source, raised before any S3 write, never a silent fall
    through to a less specific source.

    Moved from :mod:`klayout_tools.sim_batch` (issue #6437) so its size
    ratchet stays green; re-imported there under the same name. Unchanged
    by the move apart from the module-level ``SimError`` import and the
    finiteness guard, which is
    :func:`~klayout_tools._jsonnum.is_finite_number` (issue #6115) rather
    than the pre-#6115 four-clause ``math.isfinite`` test.
    """
    candidates: tuple[tuple[str, str, Any], ...] = (
        ("request", "request.batch.max_job_seconds", batch_spec.get("max_job_seconds")),
        ("env", f"${MAX_JOB_SECONDS_ENV}", os.environ.get(MAX_JOB_SECONDS_ENV)),
        (
            "fleet_config",
            f"{FLEET_MAX_JOB_SECONDS_KEY} in {FLEET_CONFIG_FILENAME}",
            fleet.get(FLEET_MAX_JOB_SECONDS_KEY),
        ),
    )
    for source, label, raw in candidates:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        value: Any = raw
        if isinstance(raw, str):
            try:
                value = float(raw.strip())
            except ValueError:
                value = None
        if not is_finite_number(value) or value <= 0:
            raise SimError(
                f"{label} must be a positive number of seconds (got {raw!r})"
            )
        return float(value), source
    return DEFAULT_BATCH_MAX_JOB_SECONDS, "default"


def _resolve_max_concurrent_jobs(batch_spec: dict[str, Any]) -> int | None:
    """``request.batch.max_concurrent_jobs`` (issue #3914): a positive integer
    bound on the shard jobs in flight at once, else
    :class:`~klayout_tools.sim.SimError` before any S3 write.

    Moved from :mod:`klayout_tools.sim_batch` (issue #6437) so its size
    ratchet stays green; re-imported there under the same name. Unchanged
    by the move apart from the module-level ``SimError`` import."""
    value = batch_spec.get("max_concurrent_jobs")
    if value is None:
        return None
    if not is_int(value) or value < 1:
        raise SimError(
            "request.batch.max_concurrent_jobs must be a positive integer "
            f"(got {value!r})",
            code=BATCH_MAX_CONCURRENT_CODE,
        )
    return value


def _resolve_runner_version_check(batch_spec: dict[str, Any]) -> str:
    """``request.batch.runner_version_check``: ``"enforce"`` (default) or
    ``"warn"``, else :class:`~klayout_tools.sim.SimError` before any S3 write.

    Moved from :mod:`klayout_tools.sim_batch` (issue #6437) so its size
    ratchet stays green; re-imported there under the same name. Unchanged
    by the move apart from the module-level ``SimError`` import."""
    value = batch_spec.get("runner_version_check", "enforce")
    if value not in RUNNER_VERSION_CHECK_MODES:
        raise SimError(
            "request.batch.runner_version_check must be one of "
            f"{', '.join(repr(m) for m in RUNNER_VERSION_CHECK_MODES)} "
            f"(got {value!r})"
        )
    return value


def resolve_capacity_wait_s(batch_spec: dict[str, Any]) -> float:
    """``request.batch.capacity_wait_s``: a finite, non-negative number of
    seconds (default ``0``), else :class:`~klayout_tools.sim.SimError` --
    raised from :func:`~klayout_tools.sim_batch._resolve_batch_config`,
    i.e. before any S3 write.

    A ``bool`` is refused even though it is an ``int`` subclass: ``true``
    reading as "wait one second" is never what a request author meant.

    Moved from :mod:`klayout_tools.sim_batch` (issue #6437) so its size
    ratchet stays green; re-imported there under the same name. Unchanged
    by the move apart from the module-level ``SimError`` import and the
    finiteness guard, which is
    :func:`~klayout_tools._jsonnum.is_finite_number` (issue #6115) rather
    than the pre-#6115 four-clause ``math.isfinite`` test."""
    value = batch_spec.get("capacity_wait_s", 0)
    if not is_finite_number(value) or value < 0:
        raise SimError(
            "request.batch.capacity_wait_s must be a non-negative number of "
            f"seconds (got {value!r})"
        )
    return float(value)


def resolve_capacity_retries(batch_spec: dict[str, Any]) -> int:
    """The ``--capacity-retries`` value for a sharded request (issue #5567):
    a non-negative integer (default ``0``, off), else
    :class:`~klayout_tools.sim.SimError`. A ``bool`` is refused.

    Moved from :mod:`klayout_tools.sim_batch` (issue #6437) so its size
    ratchet stays green; re-imported there under the same name. Unchanged
    by the move apart from the module-level ``SimError`` import."""
    value = batch_spec.get("capacity_retries", 0)
    if not is_int(value) or value < 0:
        raise SimError(f"--capacity-retries must be >= 0 (got {value!r})")
    return value


def _resolve_interrupted_grace_s(batch_spec: dict[str, Any]) -> float:
    """``request.batch.interrupted_grace_s``: a finite, non-negative number of
    seconds (default :data:`DEFAULT_INTERRUPTED_GRACE_S`; ``0`` disables the
    fail-fast), else :class:`~klayout_tools.sim.SimError` -- raised before any
    S3 write. A ``bool`` is refused, as for ``capacity_wait_s``.

    Moved from :mod:`klayout_tools.sim_batch` (issue #6437) so its size
    ratchet stays green; re-imported there under the same name. Unchanged
    by the move apart from the module-level ``SimError`` import and the
    finiteness guard, which is
    :func:`~klayout_tools._jsonnum.is_finite_number` (issue #6115) rather
    than the pre-#6115 four-clause ``math.isfinite`` test."""
    value = batch_spec.get("interrupted_grace_s", DEFAULT_INTERRUPTED_GRACE_S)
    if not is_finite_number(value) or value < 0:
        raise SimError(
            "request.batch.interrupted_grace_s must be a non-negative number "
            f"of seconds (got {value!r})"
        )
    return float(value)


def _validate_batch_request_knobs(batch_spec: dict[str, Any]) -> None:
    """Run the request-only batch resolvers in submit order (issue #6437).

    These checks can report the same errors as submit preflight even on a
    host without a provision script or bucket. Polling values
    (``poll_interval_s``, ``poll_timeout_s``) are parsed later by
    :func:`~klayout_tools.sim_batch._resolve_batch_config` once both resolve.
    Check ``max_job_seconds`` here only when the request supplies it, since
    its fallback values come from the environment and the fleet file.
    Keep the resolver calls in the same order as the submit path.
    """
    resolve_capacity_wait_s(batch_spec)
    _resolve_runner_version_check(batch_spec)
    raw_max_job_seconds = batch_spec.get("max_job_seconds")
    if raw_max_job_seconds is not None and not (
        isinstance(raw_max_job_seconds, str) and not raw_max_job_seconds.strip()
    ):
        _resolve_max_job_seconds(batch_spec, {})
    job_ceiling_check = _resolve_job_ceiling_check(batch_spec)
    _resolve_max_units_per_job(batch_spec, job_ceiling_check)
    _resolve_max_concurrent_jobs(batch_spec)
    resolve_job_timeout_s(batch_spec)
    resolve_capacity_retries(batch_spec)
    _resolve_interrupted_grace_s(batch_spec)
