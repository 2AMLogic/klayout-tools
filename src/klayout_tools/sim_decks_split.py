"""Splitting one deck list into several batch jobs (issue #5378).

A ``decks`` request on the batch backend normally runs as one job, so a long
list can hit the fleet's whole-job ceiling and lose every completed unit.
With ``batch.max_units_per_job`` / ``--max-units-per-job`` the list is cut
into balanced contiguous slices (the corner path's own
:func:`~klayout_tools.sim_remote._shard_corner_points` arithmetic), one job
per slice, at most ``batch.max_concurrent_jobs`` in flight. The slices are
recorded in the corner path's shard map (``batch-shards.json``, one
``units_sha256`` per shard), the per-slice reports are merged back into the
single deck-report shape in request order, and
``--attach <shard map> [--collect-partial]`` collects them later.

Imports :mod:`sim_decks` at load time; :mod:`sim_decks` imports this module
lazily, inside the functions that use it.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import sim_batch, sim_batch_plan, sim_batch_shards, sim_decks, sim_remote
from .sim_errors import SimError

#: Per-unit diagnostic code of a shard still running under ``--collect-partial``.
SHARD_UNFINISHED_CODE = "batch_shard_unfinished"
#: Per-unit diagnostic code of a shard whose job is gone / never submitted.
SHARD_LOST_CODE = "lost_shard"
#: Shard-map key holding the deck ids of every shard, in shard order.
SLICES_KEY = "deck_slices"


def units_digest(ids: list[str]) -> str:
    """Digest of a shard's ordered deck ids (the map's ``units_sha256``)."""
    return hashlib.sha256(json.dumps(list(ids)).encode("utf-8")).hexdigest()


def plan_slices(
    request: dict[str, Any], units: list[dict[str, Any]]
) -> list[list[dict[str, Any]]]:
    """Balanced contiguous slices of ``units`` under the request's
    ``max_units_per_job``; a single slice (no split) when it is unset or the
    list already fits."""
    config = _plan_config(request, units)
    cap = config.max_units_per_job
    if cap is None or len(units) <= cap:
        return [units]
    return sim_remote._shard_corner_points(units, math.ceil(len(units) / cap))


def _plan_config(request: dict[str, Any], units: list[dict[str, Any]]) -> Any:
    return sim_batch._resolve_batch_config(
        request,
        corner_count=1,
        timeout_s=sim_batch_plan.deck_serial_worst_s(units),
        check_runner=False,
    )


def _slice_request(request: dict[str, Any], ids: list[str]) -> dict[str, Any]:
    wanted = set(ids)
    return {
        **request,
        sim_decks.DECKS_FIELD: [
            entry
            for entry in request[sim_decks.DECKS_FIELD]
            if entry.get("id") in wanted
        ],
    }


def _shard_summary(
    index: int, ids: list[str], job_id: str, state: str
) -> dict[str, Any]:
    return {"index": index, "job_id": job_id, "state": state, "unit_ids": ids}


def merge_reports(
    reports: list[dict[str, Any]],
    shards: list[dict[str, Any]],
    *,
    outdir: str,
    shard_map: str,
    extra_remote: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One deck report from the per-slice reports: units in slice order (the
    request order), recounted, with ``environment.remote`` describing the
    shards."""
    units = [unit for report in reports for unit in report["units"]]
    errored = sum(1 for u in units if u.get("status") == "error")
    timed_out = sum(1 for u in units if u.get("status") == "timeout")
    passed = sum(1 for u in units if u.get("status") == "pass")
    base = reports[0]
    environment = dict(base.get("environment") or {})
    engine_version = next(
        (
            (r.get("environment") or {}).get("engine_version")
            for r in reports
            if (r.get("environment") or {}).get("engine_version")
        ),
        None,
    )
    environment["engine_version"] = engine_version
    environment["remote"] = {
        "provider": "aws-batch-fleet",
        "split": True,
        "shard_count": len(shards),
        "shard_map": shard_map,
        "shards": shards,
        **(extra_remote or {}),
    }
    return {
        **base,
        "status": "error" if (errored or timed_out) else "pass",
        "unit_count": len(units),
        "passed": passed,
        "errored": errored,
        "timed_out": timed_out,
        "units": units,
        "outdir": os.path.abspath(outdir),
        "backend": "batch",
        "environment": environment,
    }


def _failure_report(
    worker_request: dict[str, Any], *, code: str, message: str, outdir: str
) -> dict[str, Any]:
    return sim_decks._synthesized_deck_report(
        worker_request,
        code=code,
        message=message,
        outdir=outdir,
        environment={"provider": "aws-batch-fleet", "error": message},
    )


def run_split(
    request: dict[str, Any],
    pending_request: dict[str, Any],
    slices: list[list[dict[str, Any]]],
    *,
    request_dir: str,
    outdir: str,
) -> dict[str, Any]:
    """Submit one batch job per slice of ``pending_request`` and merge the
    reports. ``request`` is the whole (pre-resume) request: its digest is the
    shard map's identity, so ``--attach`` can be given the original file.

    Every slice is staged and validated before the first S3 write. A shard
    that fails (transport error, failed or timed-out job) is reported on its
    own units and in ``environment.remote.shards[]``; the other shards'
    units are kept."""
    prepared = []
    for index, chunk in enumerate(slices):
        ids = [unit["id"] for unit in chunk]
        sub = _slice_request(pending_request, ids)
        files, worker = sim_decks.stage_deck_job(sub, request_dir=request_dir)
        try:
            config, spec = sim_decks._prepare_decks_batch(sub, files, worker)
        except sim_batch.BatchError as exc:
            raise SimError(f"batch backend failed: {exc}", code=exc.code) from exc
        prepared.append((index, ids, config, spec, worker))
    config0 = prepared[0][2]
    os.makedirs(outdir, exist_ok=True)
    manifest = sim_batch._ShardManifest.open(
        outdir,
        sim_batch_shards.request_sha256(request),
        len(slices),
        resume=False,
        location={
            "jobs_prefix": getattr(config0, "jobs_prefix", None),
            "region": getattr(config0, "region", None),
            "profile": getattr(config0, "profile", None),
            **sim_batch_shards.shard_map_request_fields(request),
            "mode": sim_decks.DECK_MODE,
            SLICES_KEY: [ids for _i, ids, *_rest in prepared],
        },
    )
    for index, ids, *_rest in prepared:
        manifest.record(index, sim_batch.new_job_id(), units_digest(ids))
    print(
        f"klt sim: notice: splitting {len(pending_request[sim_decks.DECKS_FIELD])} "
        f"decks into {len(slices)} batch jobs of at most "
        f"{max(len(ids) for _i, ids, *_r in prepared)} decks each "
        f"(batch.max_units_per_job; shard map: {manifest.path})",
        file=sys.stderr,
    )

    def run_one(item: tuple[Any, ...]) -> tuple[dict[str, Any], dict[str, Any]]:
        index, ids, config, spec, worker = item
        job_id = manifest.planned_job_id(index) or sim_batch.new_job_id()
        try:
            sim_batch.submit_job(config, job_id, spec)
            manifest.mark(index, "submitted")
            manifest.mark(index, "launching")
            sim_batch.launch_job(config, job_id)
            manifest.mark(index, "launched")
            report = sim_decks._collect_decks_batch(
                config, job_id, worker, outdir=outdir
            )
            manifest.mark(index, "collected")
            state = "collected" if report.get("status") == "pass" else "failed"
        except sim_batch.BatchError as exc:
            report = _failure_report(
                worker, code=exc.code, message=str(exc), outdir=outdir
            )
            state = "failed"
        return report, _shard_summary(index, ids, job_id, state)

    cap = (config0.max_concurrent_jobs or len(prepared)) if prepared else 1
    with ThreadPoolExecutor(max_workers=max(1, min(cap, len(prepared)))) as pool:
        results = list(pool.map(run_one, prepared))
    manifest.flush()
    return merge_reports(
        [report for report, _s in results],
        [summary for _r, summary in results],
        outdir=outdir,
        shard_map=str(manifest.path),
    )


def attach_split(
    request: dict[str, Any],
    manifest_path: str,
    *,
    request_dir: str,
    outdir: str,
    collect_partial: bool,
) -> dict[str, Any]:
    """``--attach <shard map>`` for a split deck list: collect every shard
    the map records and merge them like the blocking run. The map must belong
    to this request; with ``collect_partial`` each shard's ``status.json`` is
    read once and a shard not in a terminal state reports its decks as
    unfinished instead of being waited for."""
    if os.path.basename(manifest_path) != sim_batch.BATCH_SHARDS_FILENAME:
        raise SimError(
            f"--attach {manifest_path}: expected a job id or a "
            f"{sim_batch.BATCH_SHARDS_FILENAME} shard map",
            code=sim_batch.BATCH_SHARD_MANIFEST_CODE,
        )
    try:
        with open(manifest_path, encoding="utf-8") as handle:
            document = json.load(handle)
        slices = [list(ids) for ids in document[SLICES_KEY]]
        entries = {int(e["index"]): e for e in document["shards"]}
        for index, ids in enumerate(slices):
            if entries[index]["units_sha256"] != units_digest(ids):
                raise ValueError(f"shard {index} digest does not match its decks")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SimError(
            f"--attach: shard map {manifest_path} is not a deck-list shard map "
            f"or is unreadable ({exc})",
            code=sim_batch.BATCH_SHARD_MANIFEST_CODE,
        ) from exc
    mismatch = sim_batch_shards.shard_map_mismatch(document, request)
    if mismatch is not None:
        raise SimError(
            f"--attach: shard map {manifest_path} was submitted from a "
            f"different request ({mismatch}); pass the request the shards "
            "were submitted from",
            code=sim_batch.BATCH_SHARD_MANIFEST_CODE,
        )
    known = {d.get("id") for d in request[sim_decks.DECKS_FIELD]}
    missing = sorted({i for ids in slices for i in ids} - known)
    if missing:
        raise SimError(
            f"--attach: shard map {manifest_path} names decks {missing} that "
            "this request does not declare",
            code="attach_request_mismatch",
        )
    reports: list[dict[str, Any]] = []
    shards: list[dict[str, Any]] = []
    unfinished: list[dict[str, Any]] = []
    for index, ids in enumerate(slices):
        entry = entries[index]
        job_id = str(entry["job_id"])
        sub = _slice_request(request, ids)
        _files, worker = sim_decks.stage_deck_job(sub, request_dir=request_dir)
        units = worker[sim_decks.DECKS_FIELD]
        try:
            config = sim_batch._resolve_batch_config(
                sub,
                corner_count=1,
                timeout_s=sim_batch_plan.deck_serial_worst_s(units),
            )
            report, state = _attach_shard(
                config, job_id, entry, worker, outdir, collect_partial
            )
        except sim_batch.BatchError as exc:
            raise SimError(f"batch backend failed: {exc}", code=exc.code) from exc
        if state not in ("collected", "failed"):
            unfinished.append(
                {"index": index, "job_id": job_id, "state": state, "unit_ids": ids}
            )
        reports.append(report)
        shards.append(_shard_summary(index, ids, job_id, state))
    extra: dict[str, Any] = {}
    if unfinished:
        extra = {"unfinished_shards": unfinished, "resumable": True}
    return merge_reports(
        reports, shards, outdir=outdir, shard_map=manifest_path, extra_remote=extra
    )


def _attach_shard(
    config: Any,
    job_id: str,
    entry: dict[str, Any],
    worker: dict[str, Any],
    outdir: str,
    collect_partial: bool,
) -> tuple[dict[str, Any], str]:
    """Collect one recorded shard; returns its report and its state
    (``collected`` / ``failed`` / the non-terminal state that was found)."""
    if collect_partial:
        state = "unlaunched" if entry.get("state") == "submitted" else None
        if state is None:
            try:
                status = sim_batch._read_status(
                    config, job_id, sim_batch._run_subprocess
                )
            except Exception:  # noqa: BLE001 - an unreadable status is "unknown"
                status = None
            raw = status.get("state") if status else None
            state = str(raw) if raw else "unknown"
        if state not in sim_batch.BATCH_TERMINAL_STATES:
            message = (
                f"shard still unfinished (--collect-partial): remote job "
                f"{job_id} is {state}; re-run --attach without --collect-partial "
                "to wait for it"
            )
            return (
                _failure_report(
                    worker, code=SHARD_UNFINISHED_CODE, message=message, outdir=outdir
                ),
                state,
            )
    if not sim_batch.job_exists(config, job_id):
        message = f"no batch job {job_id!r} for this shard (expired or wrong bucket)"
        return (
            _failure_report(
                worker, code=SHARD_LOST_CODE, message=message, outdir=outdir
            ),
            "failed",
        )
    report = sim_decks._collect_decks_batch(config, job_id, worker, outdir=outdir)
    return report, ("collected" if report.get("status") == "pass" else "failed")


def run_if_split(
    request: dict[str, Any],
    pending_request: dict[str, Any],
    pending: list[dict[str, Any]],
    *,
    request_dir: str,
    outdir: str,
) -> dict[str, Any] | None:
    """:func:`run_split` when the cap cuts ``pending`` into several slices,
    else ``None`` (the caller runs the one-job path)."""
    slices = plan_slices(pending_request, pending)
    if len(slices) < 2:
        return None
    return run_split(
        request, pending_request, slices, request_dir=request_dir, outdir=outdir
    )


def refuse_submit_only(request: dict[str, Any], units: list[dict[str, Any]]) -> None:
    """``--submit-only`` submits one job; a deck list the split would cut
    into several is refused by name (issue #5378) -- run it blocking."""
    if len(plan_slices(request, units)) > 1:
        raise SimError(
            "--submit-only cannot submit a deck-list request that "
            "batch.max_units_per_job / --max-units-per-job splits into several "
            "jobs; run it without --submit-only (it writes a shard map for "
            "--attach <map> --collect-partial)",
            code="batch_decks_split_unsupported",
        )


def attach(
    request: dict[str, Any],
    worker_request: dict[str, Any],
    attach_job_id: str,
    *,
    request_dir: str,
    outdir: str,
    collect_partial: bool,
) -> dict[str, Any]:
    """``--attach`` for a deck list: a shard map (``*.json``) collects every
    shard of a split run; anything else is the one job id of an unsplit run."""
    if attach_job_id.endswith(".json"):
        return attach_split(
            request,
            attach_job_id,
            request_dir=request_dir,
            outdir=outdir,
            collect_partial=collect_partial,
        )
    return _attach_single_job(request, worker_request, attach_job_id, outdir=outdir)


def _attach_single_job(
    request: dict[str, Any],
    worker_request: dict[str, Any],
    job_id: str,
    *,
    outdir: str,
) -> dict[str, Any]:
    """``--attach <job_id>`` for a deck list (issue #3958): stage, upload and
    launch nothing; confirm the job exists, then poll and collect it exactly
    as a blocking run would. The request must be the one the job was
    submitted from -- it names the unit directories installed under
    ``outdir``; a collected report whose units differ is refused."""
    units = worker_request[sim_decks.DECKS_FIELD]
    serial_worst_s = sim_batch_plan.deck_serial_worst_s(units)
    try:
        config = sim_batch._resolve_batch_config(
            request, corner_count=1, timeout_s=serial_worst_s
        )
        if not sim_batch.job_exists(config, job_id):
            raise sim_batch.BatchError(
                f"no batch job {job_id!r} in the configured bucket under "
                f"prefix {config.jobs_prefix!r} (never submitted, wrong "
                "bucket/profile/region, or expired)",
                code=sim_batch.BATCH_UNKNOWN_JOB_CODE,
            )
        report = sim_decks._collect_decks_batch(
            config, job_id, worker_request, outdir=outdir
        )
    except sim_batch.BatchError as exc:
        raise SimError(f"batch backend failed: {exc}", code=exc.code) from exc
    expected = [unit["id"] for unit in units]
    actual = [unit.get("id") for unit in report["units"]]
    if actual != expected:
        raise SimError(
            f"batch job {job_id!r} ran decks {actual} but this request "
            f"declares {expected}: --attach needs the request the job was "
            "submitted from",
            code="attach_request_mismatch",
        )
    return report
