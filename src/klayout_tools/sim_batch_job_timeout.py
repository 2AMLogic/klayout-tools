"""Caller-stated whole-job wall bound for ``backend: batch`` (issue #5544).

Split out of :mod:`klayout_tools.sim_batch` to keep that module under its
per-file size ratchet (same shape as :mod:`klayout_tools.sim_batch_warn`).

``request.batch.job_timeout_s`` lets the caller state one batch job's wall
bound directly. Every job shape today is sized from the fully-serial worst
case (``options.timeout_s x units + 120``,
:func:`~klayout_tools.sim_batch_contract_jobspec.default_batch_job_timeout_s`)
-- `job.json`'s ``timeout_seconds``, the pre-submit ceiling assessment and
the default poll budget all derive from it -- but a job's units run
*concurrently* on the job instance, so a caller covering per-unit contention
has to raise ``options.timeout_s``, and the serial formula multiplies that
inflated number by the unit count again. Stating the bound breaks that
coupling: ``options.timeout_s`` keeps its single meaning (the per-unit
engine timeout) and the whole-job wall becomes a separate, honest knob.
The key mirrors ``request.batch.job_timeout_s`` on ``klt place-and-route``.
Unset, nothing here is reached and behavior is the serial bound, unchanged.

This module does not import :mod:`klayout_tools.sim_batch` at runtime (only
for typing), so :mod:`klayout_tools.sim_batch` can import it freely.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._jsonnum import is_finite_number, is_int
from .sim_batch_contract import (
    _BATCH_PROVISION_SLACK_S,
    BATCH_MAX_UNITS_CODE,
    DEFAULT_POLL_INTERVAL_S,
    JOB_CEILING_BASIS_CALLER,
    JOB_CEILING_CHECK_MODES,
)
from .sim_errors import SimError

if TYPE_CHECKING:
    from .sim_batch import BatchConfig

#: Machine-readable ``error.code`` of an invalid ``request.batch.poll_interval_s``
#: or ``poll_timeout_s`` (issue #7543). Defined here, beside the resolvers that
#: raise it, so ``sim_batch_contract`` (at its 500-line limit) does not grow.
#: ``sim_batch.dry_run_runner_check`` re-raises it even on a host with no
#: configured batch runner.
BATCH_POLL_KNOB_CODE = "batch_poll_knob_invalid"


def resolve_job_timeout_s(batch_spec: dict[str, Any]) -> float | None:
    """``request.batch.job_timeout_s`` (issue #5544): the caller's stated
    wall bound for **one** batch job, in seconds. A finite, positive number
    -- a ``bool``, a non-finite, non-numeric, zero or negative value raises
    :class:`~klayout_tools.sim.SimError` before any S3 write, the same
    early-refusal discipline as the other ``batch.*`` resolvers. ``None``
    (the key absent or ``null``) keeps the serial upper bound."""
    value = batch_spec.get("job_timeout_s")
    if value is None:
        return None
    if not is_finite_number(value) or value <= 0:
        raise SimError(
            "request.batch.job_timeout_s must be a positive number of "
            f"seconds (got {value!r})"
        )
    return float(value)


def resolve_poll_interval_s(batch_spec: dict[str, Any]) -> float:
    """``request.batch.poll_interval_s`` (issue #7543): how often the client
    re-reads ``status.json`` while polling a batch job -- a finite,
    non-negative number of seconds (default
    :data:`~klayout_tools.sim_batch_contract.DEFAULT_POLL_INTERVAL_S`),
    else :class:`~klayout_tools.sim.SimError` raised from
    :func:`~klayout_tools.sim_batch.resolve_batch_config`, i.e. before any
    S3 write, the same early-refusal discipline as the other ``batch.*``
    resolvers. A ``bool`` is refused even though it is an ``int`` subclass,
    and ``0`` is accepted (a poll-every-loop request). Replaces the bare
    ``float(batch_spec.get(...))`` that let a malformed value escape as an
    uncaught ``ValueError``/``TypeError`` traceback and silently accepted
    ``true``/non-finite/negative values."""
    value = batch_spec.get("poll_interval_s", DEFAULT_POLL_INTERVAL_S)
    if not is_finite_number(value) or value < 0:
        raise SimError(
            "request.batch.poll_interval_s must be a non-negative number of "
            f"seconds (got {value!r})",
            code=BATCH_POLL_KNOB_CODE,
        )
    return float(value)


def resolve_poll_timeout_s(batch_spec: dict[str, Any], default: float) -> float:
    """Validate the explicit polling timeout or the caller's derived budget.

    Both must be finite, non-negative seconds. Zero stops at the first poll.
    """
    value = batch_spec.get("poll_timeout_s", default)
    if not is_finite_number(value) or value < 0:
        source = (
            "request.batch.poll_timeout_s"
            if "poll_timeout_s" in batch_spec
            else "derived batch poll timeout"
        )
        raise SimError(
            f"{source} must be a non-negative number of seconds (got {value!r})",
            code=BATCH_POLL_KNOB_CODE,
        )
    return float(value)


def caller_poll_timeout_s(job_timeout_s: float) -> float:
    """Default wall-clock budget :func:`~klayout_tools.sim_batch.poll_status`
    waits for a terminal ``status.json`` when the caller stated the job
    bound: the bound itself plus
    :data:`~klayout_tools.sim_batch_contract._BATCH_PROVISION_SLACK_S` for
    Spot capacity acquisition, boot, and cloud-init -- none of which have
    started when the client begins polling (issue #5544). Overridable via
    ``request.batch.poll_timeout_s`` as before."""
    return job_timeout_s + _BATCH_PROVISION_SLACK_S


def assess_caller_job_timeout(
    *, unit_count: int, per_unit_timeout_s: float, config: BatchConfig
) -> dict[str, Any]:
    """The ``environment.remote.job_ceiling`` assessment for a request that
    states ``batch.job_timeout_s`` (issue #5544): the same object
    :func:`~klayout_tools.sim_batch.assess_job_ceiling` returns under the
    serial bound, with every field defined by the caller's stated bound.

    The stated bound is unit-count-independent, which fixes every field the
    serial formula derived from the unit count:

    - ``max_units_per_job`` is ``unit_count`` when the bound fits (every
      shard of a split carries the same stated bound, so the largest shard
      whose bound stays within the ceiling is the whole run) and ``0`` when
      it exceeds (splitting cannot help: every shard would carry the same
      over-ceiling bound). Both values are exactly what
      :func:`~klayout_tools.sim_batch._plan_job_split`'s existing branches
      expect -- no special case there.
    - ``recommended_hosts`` is ``1`` when the bound fits and ``None`` when
      it exceeds, the existing "``null`` when no ``hosts`` value fits" rule.
    - ``overhead_s`` is ``0``: the stated bound already includes whatever
      slack the caller chose. It stays a number, so no consumer sees a
      type change.
    - ``warning`` (only when the bound exceeds the ceiling) names
      ``batch.job_timeout_s`` as the value to lower. It must not recommend
      more hosts -- no ``hosts`` value changes the per-job bound -- and must
      not recommend a lower ``options.timeout_s``, which no longer feeds
      the bound at all.
    """
    units = max(1, unit_count)
    ceiling = config.max_job_seconds
    bound_value = config.job_timeout_s
    if bound_value is None:
        raise SimError(
            "request.batch.job_timeout_s must be set to assess the "
            f"caller-stated job ceiling (got {bound_value!r})"
        )
    bound = float(bound_value)
    exceeds = bound > ceiling
    assessment: dict[str, Any] = {
        "basis": JOB_CEILING_BASIS_CALLER,
        "unit_count": units,
        "per_unit_timeout_s": per_unit_timeout_s,
        "overhead_s": 0.0,
        "job_bound_s": bound,
        "max_job_seconds": ceiling,
        "max_job_seconds_source": config.max_job_seconds_source,
        "exceeds_ceiling": exceeds,
        "check": config.job_ceiling_check,
        "max_units_per_job": 0 if exceeds else units,
        "recommended_hosts": None if exceeds else 1,
    }
    if exceeds:
        assessment["warning"] = (
            f"batch.job_timeout_s {bound:g}s exceeds the fleet's whole-job "
            f"ceiling of {ceiling:g}s (max_job_seconds from "
            f"{config.max_job_seconds_source}); a job still running at the "
            "ceiling is killed and every unit is reported "
            "batch_job_timeout -- lower request.batch.job_timeout_s to at "
            f"most {ceiling:g}s (splitting does not help: every shard "
            "would carry the same stated bound)"
        )
    return assessment


def _resolve_job_ceiling_check(batch_spec: dict[str, Any]) -> str:
    """``request.batch.job_ceiling_check``: ``"warn"`` (default),
    ``"enforce"`` or ``"split"`` (issue #3446), else
    :class:`~klayout_tools.sim.SimError` before any S3 write (issue #2833).

    Moved verbatim from :mod:`klayout_tools.sim_batch` (issue #5544) so its
    size ratchet stays green; re-imported there under the same name."""
    value = batch_spec.get("job_ceiling_check", "warn")
    if value not in JOB_CEILING_CHECK_MODES:
        raise SimError(
            "request.batch.job_ceiling_check must be one of "
            f"{', '.join(repr(m) for m in JOB_CEILING_CHECK_MODES)} "
            f"(got {value!r})"
        )
    return value


def _resolve_max_units_per_job(
    batch_spec: dict[str, Any], job_ceiling_check: str
) -> int | None:
    """``request.batch.max_units_per_job`` (issue #3446): a positive integer
    cap on the units of one job, which opts the request into splitting; else
    :class:`~klayout_tools.sim.SimError` before any S3 write. Combining it
    with ``job_ceiling_check: "enforce"`` is contradictory (one refuses an
    over-ceiling request, the other splits it) and is refused.

    Moved verbatim from :mod:`klayout_tools.sim_batch` (issue #5544) so its
    size ratchet stays green; re-imported there under the same name."""
    value = batch_spec.get("max_units_per_job")
    if value is None:
        return None
    if not is_int(value) or value < 1:
        raise SimError(
            "request.batch.max_units_per_job must be a positive integer "
            f"(got {value!r})",
            code=BATCH_MAX_UNITS_CODE,
        )
    if job_ceiling_check == "enforce":
        raise SimError(
            "request.batch.max_units_per_job splits an over-ceiling request "
            'and cannot be combined with job_ceiling_check "enforce", which '
            'refuses it; use "warn" or "split"',
            code=BATCH_MAX_UNITS_CODE,
        )
    return value
