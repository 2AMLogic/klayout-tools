"""Whole-job ceiling / split planning for ``backend: batch`` requests, and the
side-effect-free preflight ``--batch-set`` runs over every member (issue #4972).

Lives apart from :mod:`~klayout_tools.sim` and :mod:`~klayout_tools.sim_batch`
so the per-file size ratchet (``scripts/README.md``) is not asked to grow them.
"""

from __future__ import annotations

import contextlib
import io
from typing import Any

from .sim_batch import (
    BatchConfig,
    _apply_job_split,
    _assess_deck_support,
    _assess_job_ceiling,
    _resolve_batch_config,
    dry_run_runner_check,
)


def deck_serial_worst_s(units: list[dict[str, Any]]) -> float:
    """Fully-serial worst case of a deck list: every unit's own ``timeout_s``,
    one after another. The single definition shared by the submit path
    (``sim_decks._prepare_decks_batch``), the attach path and the
    ``--batch-set`` preflight (issue #5337), so they cannot drift."""
    return sum(unit["timeout_s"] for unit in units)


def plan_batch_job(
    config: BatchConfig,
    *,
    unit_count: int,
    timeout_s: float,
    quiet: bool = False,
) -> tuple[dict[str, Any], int]:
    """Assess one job against the fleet's whole-job ceiling and plan its split
    (issues #2833, #3446): the single implementation the submit paths use and
    ``--batch-set`` preflights with (issue #4972). Raises
    :class:`~klayout_tools.sim.SimError` (``batch_job_ceiling_exceeded`` /
    ``batch_max_units_per_job_invalid``); no S3 write. ``quiet`` suppresses the
    stderr notices (a preflight must not announce twice); it is only used
    single-threaded, before any worker starts."""
    with (
        contextlib.redirect_stderr(io.StringIO()) if quiet else contextlib.nullcontext()
    ):
        job_ceiling = _assess_job_ceiling(
            unit_count=unit_count, per_unit_timeout_s=timeout_s, config=config
        )
        return _apply_job_split(job_ceiling, config)


def dry_run_job_ceiling(
    request: dict[str, Any],
    *,
    corner_count: int,
    timeout_s: float,
    runner_out: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The batch half of ``klt sim --dry-run`` (issue #5674): the runner
    check (:func:`sim_batch.dry_run_runner_check`) followed by the same
    :func:`plan_batch_job` the submit path runs, so the dry run refuses what
    the submit refuses and the report can carry the assessment. Returns
    ``(feature_diagnostic, job_ceiling)``; the ceiling is ``None`` on a host
    with no resolvable batch configuration, which keeps such hosts
    dry-running clean. The plan is *not* quiet: the warn / serial-bound /
    split stderr notices are part of the submit behaviour the dry run
    mirrors (issue #4982), and this path is single-threaded, before any
    worker starts.
    """
    config_out: dict[str, BatchConfig] = {}
    diagnostic = dry_run_runner_check(
        request,
        corner_count=corner_count,
        timeout_s=timeout_s,
        runner_out=runner_out,
        config_out=config_out,
    )
    config = config_out.get("config")
    if config is None:
        return diagnostic, None
    job_ceiling, _hosts = plan_batch_job(
        config, unit_count=corner_count, timeout_s=timeout_s
    )
    return diagnostic, job_ceiling


def print_dry_run_plan_line(report: dict[str, Any]) -> None:
    """The ``--format text`` shard plan (issue #5674): the unit count and
    the planned job count, read from the report's ``job_ceiling``
    assessment. No-op when none was made (non-batch backend, decks or
    command-list request, unresolvable batch configuration)."""
    ceiling = report.get("job_ceiling")
    if not ceiling:
        return
    jobs = ceiling.get("effective_hosts", 1)
    units = ceiling["unit_count"]
    print(
        f"  batch plan: {units} unit(s) in {jobs} job(s) of at most "
        f"{(units + jobs - 1) // jobs} unit(s) each (bound "
        f"{ceiling['job_bound_s']:g}s of the "
        f"{ceiling['max_job_seconds']:g}s ceiling)"
    )


def preflight_batch_request(request_path: str, **overrides: Any) -> dict[str, Any]:
    """Side-effect-free ceiling / split check for one ``backend: batch`` request
    (issue #4972): resolve it exactly as a real run would (CLI overrides
    included), then run the same :func:`plan_batch_job` the submit path runs,
    before any S3 write. Raises :class:`SimError` with the submit path's own
    code; returns the job-ceiling assessment otherwise. ``overrides`` are the
    :func:`~klayout_tools.sim.run_sim` keyword options."""
    from . import sim_commands
    from .sim import (
        _prepare_sim_request,
        _with_pdk_root_override,
        load_request,
    )

    request = load_request(request_path)
    if "decks" in request:
        return _preflight_decks_request(
            _with_pdk_root_override(request, overrides.get("pdk_root")), request_path
        )
    if sim_commands.COMMANDS_FIELD in request:
        # Command-list members (issue #5317) carry no corner grid: one command
        # list is one batch job, never sharded, so there is no ceiling to assess.
        return {}
    # Anything else is a corner request: `load_request` already guarantees
    # `netlist` (and `analysis`) are present, so no further shape check is needed.
    resolved = _prepare_sim_request(
        request_path,
        artifacts_dir=overrides.get("artifacts_dir"),
        backend="batch",
        max_workers=overrides.get("max_workers"),
        hosts=overrides.get("hosts"),
        budget_s=overrides.get("budget_s"),
        resume=overrides.get("resume"),
        plot_dir=overrides.get("plot_dir"),
        fail_fast_probe=overrides.get("fail_fast_probe"),
        fail_on_diagnostic=overrides.get("fail_on_diagnostic"),
        max_units_per_job=overrides.get("max_units_per_job"),
        max_concurrent_jobs=overrides.get("max_concurrent_jobs"),
        capacity_wait_s=overrides.get("capacity_wait_s"),
        dry_run=True,
        pdk_root=overrides.get("pdk_root"),
    )
    points = resolved.corner_points
    config = _resolve_batch_config(
        resolved.request,
        corner_count=len(points),
        timeout_s=resolved.timeout_s,
        check_runner=False,
    )
    job_ceiling, _hosts = plan_batch_job(
        config, unit_count=len(points), timeout_s=resolved.timeout_s, quiet=True
    )
    return job_ceiling


def _preflight_decks_request(
    request: dict[str, Any], request_path: str
) -> dict[str, Any]:
    """Ceiling check for a ``decks[]`` member (issue #5337): a deck list is one
    batch job whose bound is the serial worst case of its units, sized exactly
    as :func:`~klayout_tools.sim_decks._prepare_decks_batch` sizes it, via the
    shared :func:`deck_serial_worst_s`, and runs the same image-support
    assessment the submit path does."""
    import os

    from . import sim_decks

    units = sim_decks.plan_deck_units(
        request,
        request_dir=os.path.dirname(os.path.abspath(request_path)),
        allow_worker_form=False,
    )
    serial_worst_s = deck_serial_worst_s(units)
    config = _resolve_batch_config(
        request, corner_count=1, timeout_s=serial_worst_s, check_runner=False
    )
    _assess_deck_support(request, config)
    job_ceiling, _hosts = plan_batch_job(
        config, unit_count=1, timeout_s=serial_worst_s, quiet=True
    )
    return job_ceiling
