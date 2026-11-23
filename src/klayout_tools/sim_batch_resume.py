"""``--resume``'s harvest of a retained batch shard map (issue #5775), plus
the batch-checkpoint recording helpers it shares with the dispatch path.

A sharded batch run keeps ``<artifacts_dir>/batch-shards.json`` even without
``--resume`` (issue #3896). On ``--resume`` this module harvests that map
*before* the dispatch set is computed: every shard whose remote job is
already terminal is collected through the plain attach machinery, the
harvested corners are recorded into the live checkpoint, and the dispatch
that follows covers only the genuinely outstanding units -- never a re-bill
of work the map already accounted for.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from . import sim_batch
from .sim_batch_shards import mismatch_error, shard_map_mismatch
from .sim_errors import SimError

#: Diagnostic codes of a ``batch`` corner that never ran: a lost Spot shard,
#: a job that failed or was stopped by the harness, or a poll that gave up
#: while the job may still be running (``sim_batch._unrun_corner_report``).
UNRUN_BATCH_CODES = frozenset(
    {
        "lost_shard",
        "batch_job_failed",
        "batch_job_timeout",
        "batch_poll_timeout",
        "batch_job_interrupted",
    }
)


def _is_unrun_batch_corner(corner: dict[str, Any]) -> bool:
    """True when a corner report is a synthesized batch one (never ran)."""
    return any(diag["code"] in UNRUN_BATCH_CODES for diag in corner["diagnostics"])


def _record_batch_checkpoint(
    checkpoint: Any,
    backend: str,
    corners_new: list[dict[str, Any]],
    engine_version: Any,
) -> None:
    """Record a ``batch`` run's returned corner reports into the checkpoint.

    Issue #3367: `_run_batch` collects a shard's reports in one
    ``report.json``, so there is nothing to record incrementally; record the
    returned set after dispatch. A corner that never ran (``lost_shard``,
    ``batch_job_failed``, ``batch_job_timeout``, ``batch_poll_timeout``,
    ``batch_job_interrupted``) is left out and stays outstanding for the next
    ``--resume``; one that ran and genuinely errored is recorded as locally.
    A no-op without a checkpoint or off ``batch``.
    """
    if checkpoint is None or backend != "batch":
        return
    for corner in corners_new:
        if not _is_unrun_batch_corner(corner):
            checkpoint.record(corner["corner_id"], corner, engine_version)


def _entry_index(entry: dict[str, Any]) -> int:
    """A shard map entry's index (``0`` when absent or malformed)."""
    return int(entry.get("index") or 0)


def _entry_unit_ids(
    entry: dict[str, Any],
    by_id: dict[str, Any],
    partition: list[list[Any]],
) -> list[str] | None:
    """The ordered corner ids one shard map entry's job ran (issue #5775):
    from the entry's own recorded ``units``, else -- a map written before
    #5775 -- the one partition slice whose digest matches its recorded
    ``units_sha256``. ``None`` when the grouping cannot be recovered."""
    units = entry.get("units")
    if isinstance(units, list) and all(
        isinstance(unit, str) and unit in by_id for unit in units
    ):
        return list(units)
    wanted = entry.get("units_sha256")
    for points in partition:
        if points and sim_batch._units_digest(points) == wanted:
            return [point.corner_id for point in points]
    return None


def _load_resume_shard_map(
    path: str, fingerprint: str, request: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]], int] | None:
    """Read + validate the retained shard map at ``path`` (issue #5775):
    ``None`` when there is no map, or a well-formed one listing no shards --
    an inert file, not a stale one. An unreadable map, or one written for a
    different fingerprint/request, refuses with
    :data:`sim_batch.BATCH_SHARD_MANIFEST_CODE` -- never a silent attach of
    stale results."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        entries = [dict(entry) for entry in document["shards"]]
        hosts = int(document["hosts"])
        map_fingerprint = str(document["fingerprint"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SimError(
            f"--resume: shard map {path} is unreadable ({exc}); delete it "
            "or drop --resume",
            code=sim_batch.BATCH_SHARD_MANIFEST_CODE,
        ) from exc
    if not entries:
        return None
    if map_fingerprint != fingerprint:
        raise mismatch_error(path, document, fingerprint, False)
    mismatch = shard_map_mismatch(document, request)
    if mismatch is not None:
        raise SimError(
            f"--resume: shard map {path} was submitted from a different "
            f"request ({mismatch}); re-run with exactly the request the "
            "shards were submitted from",
            code=sim_batch.BATCH_SHARD_MANIFEST_CODE,
        )
    return document, entries, hosts


# A harvest's status probe re-reads an unreadable status this many times,
# pausing between attempts (zero in tests; #7115).
_STATUS_READ_ATTEMPTS = 3
_STATUS_READ_BACKOFF_S = 2.0


def _shard_status(config: sim_batch.BatchConfig, job_id: str) -> dict[str, Any] | None:
    """A shard job's remote ``status.json``, re-read a small fixed number of
    times when a read fails (issue #7115) -- ``None`` only when every
    attempt fails (an unreadable status is "unknown", never an error)."""
    for attempt in range(_STATUS_READ_ATTEMPTS):
        if attempt:
            time.sleep(_STATUS_READ_BACKOFF_S)
        try:
            status = sim_batch._read_status(config, job_id, sim_batch._run_subprocess)
        except Exception:
            status = None
        if status is not None:
            return status
    return None


def _skip_notice(entry: dict[str, Any], reason: str) -> None:
    """One stderr line for a shard the harvest leaves for the dispatch
    (issue #7115): a silent skip is indistinguishable from no probe."""
    print(
        f"klt sim: notice: --resume did not harvest shard "
        f"{_entry_index(entry)} (job {entry.get('job_id')}): {reason}",
        file=sys.stderr,
    )


def _collect_resume_entry(
    entry: dict[str, Any],
    config: sim_batch.BatchConfig,
    by_id: dict[str, Any],
    partition: list[list[Any]],
    known: frozenset[str] | set[str],
    *,
    netlist_path: str,
    timeout_s: float,
    keep_artifacts: bool,
    want_waveforms: bool,
    artifacts_dir: str,
    request: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    request_dir: str | None,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None, int] | None:
    """One entry's disposition and collection (issues #5775, #7115). ``None``
    unless the entry is a terminal job whose units are genuinely
    outstanding: an unrecoverable grouping, units the checkpoint already
    holds, a never-launched (``submitted``) shard and a non-terminal status
    all stay untouched for the dispatch, each skip but the deliberate
    submitted/known ones announced on stderr. A terminal entry is collected
    through the attach machinery: probe, never wait, never launch."""
    unit_ids = _entry_unit_ids(entry, by_id, partition)
    if unit_ids is None:
        _skip_notice(entry, "shard grouping not recoverable")
        return None
    if all(unit in known for unit in unit_ids):
        return None
    if entry.get("state") == "submitted":
        # Never launched: no results exist to harvest, and launching is the
        # dispatch's decision (`launch_if_unlaunched`), never a harvest's (a
        # shard the map says was submitted is launched, not re-minted, when
        # the partition is unchanged).
        return None
    job_id = str(entry["job_id"])
    status = _shard_status(config, job_id)
    state = str((status or {}).get("state") or "unknown")
    if state not in sim_batch.BATCH_TERMINAL_STATES:
        reason = "status unreadable" if status is None else f"remote state {state}"
        _skip_notice(entry, reason)
        return None
    points = [by_id[unit] for unit in unit_ids]
    try:
        result = sim_batch._run_batch_job(
            config=config,
            job_id=job_id,
            corner_points=points,
            netlist_path=netlist_path,
            timeout_s=timeout_s,
            keep_artifacts=keep_artifacts,
            want_waveforms=want_waveforms,
            artifacts_dir=artifacts_dir,
            request=request,
            measurements_spec=measurements_spec,
            explicit_points=points,
            request_dir=request_dir,
            attach=True,
        )
        corners, engine_version, environment = sim_batch._require_shard_environment(
            result
        )
    except sim_batch.BatchError as exc:
        print(
            f"klt sim: notice: --resume could not re-attach to shard "
            f"job {job_id} ({exc}); its units stay outstanding",
            file=sys.stderr,
        )
        return None
    return corners, engine_version, environment, _entry_index(entry)


def _report_harvest(
    corners_out: list[dict[str, Any]], harvested_indexes: list[int], path: str
) -> None:
    """One stderr line for a non-empty harvest. ``harvested_indexes`` names
    the same entries whose corners ran, so it can never disagree with the
    unit count -- a terminally failed shard's unrun corners are neither
    counted nor named."""
    if not corners_out:
        return
    ran = sum(1 for corner in corners_out if not _is_unrun_batch_corner(corner))
    if ran:
        print(
            f"klt sim: --resume harvested {ran} already-completed "
            f"unit(s) from {len(harvested_indexes)} shard(s) of {path}",
            file=sys.stderr,
        )


def _mark_map_collected(
    path: str, document: dict[str, Any], indexes: list[int]
) -> None:
    """Stamp ``collected`` on the harvested map entries, exactly as the
    fleet's own collect path would (issue #5775), so the map a resumed run
    leaves behind does not list as outstanding a shard whose outputs this
    run already took."""
    wanted = set(indexes)
    for entry in document.get("shards") or ():
        if _entry_index(entry) not in wanted:
            continue
        if entry.get("state") == "collected":
            continue
        entry["state"] = "collected"
        if not entry.get("collected_at"):
            entry["collected_at"] = datetime.now(timezone.utc).isoformat()
    sim_batch._write_batch_receipt(path, document)


def harvest_resume_shards(
    *,
    artifacts_dir: str,
    config: sim_batch.BatchConfig,
    corner_points: list[sim_batch.CornerPoint],
    request: dict[str, Any],
    netlist_path: str,
    timeout_s: float,
    keep_artifacts: bool,
    want_waveforms: bool,
    measurements_spec: list[dict[str, Any]],
    request_dir: str | None,
    fingerprint: str,
    known: frozenset[str] | set[str],
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """``--resume``'s harvest of a retained shard map (issue #5775): collect
    the corners of every shard whose remote job is already terminal, so the
    ordinary resume flow re-dispatches only what is genuinely outstanding.

    One entry at a time -- probe its remote ``status.json`` (an unreadable
    read is retried, #7115), never wait, never launch (a shard the map
    records ``submitted`` stays for the dispatch, which launches it under
    the recorded id when the partition is unchanged). A terminal entry is
    collected through the plain attach
    machinery (:func:`sim_batch._run_batch_job` with ``attach=True``) and
    marked ``collected`` in the map; anything else is left alone. Returns
    ``(corners, engine_version, remote_environment)`` -- the corners
    :func:`_record_batch_checkpoint` filters and records (a terminally
    *failed* shard returns unrun corners, which that filter leaves
    outstanding). The environment carries only shards whose corners ran,
    in map order, with ``None`` for a shard without provenance. Returns
    ``([], None, None)`` when there is no map or nothing new to collect.

    A map written for a different request refuses with
    :data:`sim_batch.BATCH_SHARD_MANIFEST_CODE` -- never a silent attach of
    stale results.
    """
    path = os.path.join(artifacts_dir, sim_batch.BATCH_SHARDS_FILENAME)
    loaded = _load_resume_shard_map(path, fingerprint, request)
    if loaded is None:
        return [], None, None
    document, entries, hosts = loaded
    by_id = {point.corner_id: point for point in corner_points}
    partition = sim_batch._shard_corner_points(corner_points, hosts)
    corners_out: list[dict[str, Any]] = []
    engine_version: str | None = None
    harvested_indexes: list[int] = []
    fleet: list[dict[str, Any] | None] = []
    collected_indexes: list[int] = []
    for entry in sorted(entries, key=_entry_index):
        outcome = _collect_resume_entry(
            entry,
            config,
            by_id,
            partition,
            known,
            netlist_path=netlist_path,
            timeout_s=timeout_s,
            keep_artifacts=keep_artifacts,
            want_waveforms=want_waveforms,
            artifacts_dir=artifacts_dir,
            request=request,
            measurements_spec=measurements_spec,
            request_dir=request_dir,
        )
        if outcome is None:
            continue
        corners, entry_engine_version, environment, index = outcome
        corners_out.extend(corners)
        if any(not _is_unrun_batch_corner(corner) for corner in corners):
            harvested_indexes.append(index)
            fleet.append(
                sim_batch._mark_resumed(
                    (corners, entry_engine_version, environment), True
                )[2]
            )
        if entry_engine_version is not None:
            engine_version = entry_engine_version
        # Only a shard whose outputs were actually collected is marked
        # collected -- exactly the fleet's own `_mark_collected` rule -- so a
        # terminally failed job stays outstanding for the dispatch (its
        # corners come back unrun and are filtered by the caller).
        if environment is None or not environment.get("client_collected_at"):
            continue
        collected_indexes.append(index)
    if collected_indexes:
        _mark_map_collected(path, document, collected_indexes)
    _report_harvest(corners_out, harvested_indexes, path)
    return corners_out, engine_version, {"fleet": fleet} if fleet else None


def harvest_resume_shard_map(
    *,
    artifacts_dir: str,
    corner_points: list[sim_batch.CornerPoint],
    request: dict[str, Any],
    netlist_path: str,
    timeout_s: float,
    keep_artifacts: bool,
    want_waveforms: bool,
    measurements_spec: list[dict[str, Any]],
    request_dir: str | None,
    checkpoint: Any,
    pre_completed: dict[str, dict[str, Any]],
) -> tuple[str | None, dict[str, Any] | None]:
    """Harvest the retained shard map and absorb what it yields: the
    harvested corners are recorded into the live checkpoint (so an
    interrupted resumed run keeps them) and merged into ``pre_completed``
    (so ``dispatch_points`` shrinks and the ``resume: reused N`` summary
    counts them). Returns the harvested engine version and remote
    environment, or ``(None, None)`` when nothing was harvested.

    Without ``--resume`` a sharded run persists no checkpoint, so this -- not
    ``checkpoint.json`` -- is what makes ``environment.remote.resumable:
    true`` satisfiable by ``--resume`` alone.
    """
    config = sim_batch._resolve_batch_config(
        request, corner_count=len(corner_points), timeout_s=timeout_s
    )
    corners, engine_version, environment = harvest_resume_shards(
        artifacts_dir=artifacts_dir,
        config=config,
        corner_points=corner_points,
        request=request,
        netlist_path=netlist_path,
        timeout_s=timeout_s,
        keep_artifacts=keep_artifacts,
        want_waveforms=want_waveforms,
        measurements_spec=measurements_spec,
        request_dir=request_dir,
        fingerprint=checkpoint.fingerprint,
        known=frozenset(pre_completed),
    )
    if not corners:
        return None, None
    _record_batch_checkpoint(checkpoint, "batch", corners, engine_version)
    pre_completed.update(
        {
            corner["corner_id"]: corner
            for corner in corners
            if not _is_unrun_batch_corner(corner)
        }
    )
    return engine_version, environment


def maybe_harvest_resume_shards(
    *,
    resume: bool | None,
    backend: str,
    checkpoint: Any,
    dry_run: bool,
    submit_only: bool,
    attach_job_id: str | None,
    **harvest_kwargs: Any,
) -> tuple[str | None, dict[str, Any] | None]:
    """``_run_sim_corners``'s whole resume-harvest step (issue #5775): with
    ``--resume`` on the batch backend -- never for ``--attach`` (the
    explicit re-attach form), ``--submit-only`` (refused with ``--resume``
    before any S3 write) or ``--dry-run`` (which must not touch S3 at all)
    -- harvest the retained shard map into the checkpoint and return the
    harvested engine version and remote environment; otherwise ``(None, None)``."""
    if not (
        resume
        and backend == "batch"
        and checkpoint is not None
        and not dry_run
        and not submit_only
        and attach_job_id is None
    ):
        return None, None
    return harvest_resume_shard_map(checkpoint=checkpoint, **harvest_kwargs)


def resume_hosts(
    hosts: int, corner_points: Sequence[Any], dispatch_points: Sequence[Any]
) -> int:
    """The fleet size a resumed dispatch may ask for (issue #6685).

    ``--resume`` reuses finished units, so ``dispatch_points`` (what is still
    outstanding) can be shorter than ``corner_points`` (the whole request) and
    fall below the requested ``hosts``. The "hosts exceeds the units to
    dispatch" refusal guards a user asking for more hosts than units; on a
    resume the user asked for nothing new, so refusing would strand a
    half-finished sweep. Cap ``hosts`` at the outstanding unit count -- but
    only when the resume actually reused something: a request whose own
    ``hosts`` exceeds its own units is still refused, resume or not. Nothing
    outstanding (everything reused) dispatches nothing, so there is nothing to
    cap.
    """
    if 0 < len(dispatch_points) < len(corner_points):
        return min(hosts, len(dispatch_points))
    return hosts


def merge_resume_environment(
    harvested: dict[str, Any] | None,
    dispatched: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Combine provenance for harvested and dispatched shards (issue #6423).

    Keep harvested map order, then dispatch order, preserving unknown shard
    entries and dispatch-level signals. A job observed in both phases uses
    the newer dispatch observation once. Single-job dispatch metadata belongs
    inside ``fleet[]``, never at the fleet level. Neither input is mutated.
    """
    if harvested is None:
        return dispatched
    if dispatched is None:
        return harvested
    if isinstance(dispatched.get("fleet"), list):
        result = dict(dispatched)
        dispatched_fleet = dispatched["fleet"]
    else:
        result = {}
        dispatched_fleet = [dispatched]
    dispatched_jobs = {
        entry["job_id"]
        for entry in dispatched_fleet
        if isinstance(entry, dict) and entry.get("job_id") is not None
    }
    result["fleet"] = [
        entry
        for entry in harvested["fleet"]
        if not (isinstance(entry, dict) and entry.get("job_id") in dispatched_jobs)
    ] + dispatched_fleet
    return result
