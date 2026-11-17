"""Request identity and shard scoping for sharded ``--submit-only`` /
``--attach`` (issues #4620, #4878); split out of :mod:`sim_batch_async`."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

from . import sim_batch

#: ``request.batch`` fields that steer submission only (how long to wait for
#: spot capacity, how many jobs to keep in flight) and never change what a
#: shard computes. They are left out of :func:`request_sha256` so a sharded
#: ``--submit-only --wait-for-capacity`` can be collected by an
#: ``--attach <map>`` that (by design) cannot repeat that flag (issue #4878).
_SUBMISSION_ONLY_BATCH_FIELDS = ("capacity_wait_s", "max_concurrent_jobs")


def _canonical_sha256(document: Any) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def legacy_request_sha256(request: dict[str, Any]) -> str:
    """The digest of the whole request document, as maps written before
    issue #4878 recorded it; ``--attach`` still accepts it."""
    return _canonical_sha256(request)


def request_sha256(request: dict[str, Any]) -> str:
    """Digest of the request document (canonical JSON), so a caller can tie a
    later status read back to the request it submitted. Submission-only
    ``batch`` fields (:data:`_SUBMISSION_ONLY_BATCH_FIELDS`) are excluded."""
    return _canonical_sha256(_without_submission_only(request, drop_empty=False))


def receipt_request_sha256(request: dict[str, Any]) -> str:
    """:func:`request_sha256` that also drops a ``batch`` block the exclusion
    emptied, so a request that never had one (``--wait-for-capacity`` folds
    ``batch.capacity_wait_s`` in) hashes the same as the original file
    (issue #5194). Used by the synchronous submit receipt."""
    return _canonical_sha256(_without_submission_only(request, drop_empty=True))


def _without_submission_only(
    request: dict[str, Any], *, drop_empty: bool
) -> dict[str, Any]:
    batch = request.get("batch")
    if not (
        isinstance(batch, dict)
        and any(k in batch for k in _SUBMISSION_ONLY_BATCH_FIELDS)
    ):
        return request
    kept = {k: v for k, v in batch.items() if k not in _SUBMISSION_ONLY_BATCH_FIELDS}
    if drop_empty and not kept:
        return {k: v for k, v in request.items() if k != "batch"}
    return {**request, "batch": kept}


def request_section_digests(request: dict[str, Any]) -> dict[str, str]:
    """Short per-field digests (top-level keys; ``batch`` split per key) that
    the shard map records so a digest mismatch can name what differs."""
    sections: dict[str, str] = {}
    for key, value in request.items():
        if key == "batch" and isinstance(value, dict):
            for sub, sub_value in value.items():
                if sub not in _SUBMISSION_ONLY_BATCH_FIELDS:
                    sections[f"batch.{sub}"] = _canonical_sha256(sub_value)[:12]
        else:
            sections[key] = _canonical_sha256(value)[:12]
    return sections


def differing_request_fields(
    recorded: dict[str, str], request: dict[str, Any]
) -> list[str]:
    """Fields whose digest differs between a map's record and ``request``."""
    current = request_section_digests(request)
    return sorted(
        key
        for key in set(recorded) | set(current)
        if recorded.get(key) != current.get(key)
    )


def shard_map_request_fields(request: dict[str, Any]) -> dict[str, Any]:
    """The request identity a shard map records (issue #4878)."""
    return {
        "request_sha256": request_sha256(request),
        "request_sections": request_section_digests(request),
    }


def shard_map_mismatch(document: dict[str, Any], request: dict[str, Any]) -> str | None:
    """Why ``request`` is not the one ``document`` (a shard map) was submitted
    from: ``None`` when it is (or the map records no digest), else the text to
    show the caller."""
    recorded = document.get("request_sha256")
    current = request_sha256(request)
    if recorded in (None, current, legacy_request_sha256(request)):
        return None
    sections = document.get("request_sections")
    differing = (
        differing_request_fields(sections, request)
        if isinstance(sections, dict)
        else []
    )
    detail = (
        f"differing request field(s): {', '.join(differing)}"
        if differing
        else "re-run with exactly the request file, overrides (--max-units-per-job, "
        "--budget-s, --pdk-root) and environment used at --submit-only"
    )
    return f"request_sha256: map {str(recorded)[:12]}, request {current[:12]}; {detail}"


def shard_points_for_job(
    artifacts_dir: str, job_id: str, corner_points: list[Any]
) -> list[Any] | None:
    """The slice of ``corner_points`` one shard job of a sharded submit ran,
    read from the shard map in ``artifacts_dir`` (issue #4878); ``None`` when
    no map there lists ``job_id`` (a plain single-job attach)."""
    try:
        path = os.path.join(artifacts_dir, sim_batch.BATCH_SHARDS_FILENAME)
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        hosts = int(document["hosts"])
        entry = next(e for e in document["shards"] if e.get("job_id") == job_id)
        wanted = entry["units_sha256"]
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        return None
    for points in sim_batch._shard_corner_points(corner_points, hosts):
        if points and sim_batch._units_digest(points) == wanted:
            return points
    return None


def shard_scoped_points(resolved: Any, attach_job_id: str | None) -> list[Any]:
    """``resolved.corner_points`` narrowed to one shard's slice when
    ``--attach`` names a shard job of a sharded submit: a bare shard job
    returns only its own corners, so the echo is compared against that slice
    (issue #4878). Otherwise unchanged."""
    points = resolved.corner_points
    if (
        resolved.backend != "batch"
        or attach_job_id is None
        or attach_job_id.endswith(".json")
        or resolved.artifacts_dir is None
    ):
        return points
    shard = shard_points_for_job(resolved.artifacts_dir, attach_job_id, points)
    return points if shard is None else shard


def mismatch_error(
    path: str, stored: dict[str, Any], fingerprint: str, submit_only: bool
) -> Exception:
    """Refusal for a shard map written for a different request. Issue #4979:
    a ``--submit-only`` caller passed no ``--resume``, so name what they ran
    and keep the first request's map (and its ``--attach`` path) intact."""
    hosts = stored["hosts"]
    difference = sim_batch._manifest_difference(
        stored["fingerprint"], fingerprint, hosts, hosts
    )
    if submit_only:
        message = (
            f"--submit-only: shard map {path} belongs to a different request "
            f"({difference}); use a distinct -o directory for this request, or "
            "finish/attach the first one (deleting the map would lose its "
            "--attach path)"
        )
    else:
        message = (
            f"--resume: shard map {path} was written for a different request "
            f"({difference}); delete it or drop --resume"
        )
    from .sim import SimError

    return SimError(message, code=sim_batch.BATCH_SHARD_MANIFEST_CODE)


def gate_partial_shard(fleet: Any, index: int, prior: dict[str, Any]) -> None:
    """``--attach <map> --collect-partial``: read one shard's ``status.json``
    once, never waiting. Returns for a shard in a terminal state (collected
    as in a plain attach); raises for any other, which
    :func:`sim_remote._run_sharded` reports as a ``lost_shard`` per unit
    with this reason. Nothing is launched: a shard the map records as
    ``submitted`` (never launched) is reported as unlaunched.
    """
    job_id = str(prior["job_id"])
    if prior.get("state") == "submitted":
        status = None
        state = "unlaunched"
    else:
        try:
            status = sim_batch._read_status(
                fleet.config, job_id, sim_batch._run_subprocess
            )
        except Exception:  # - an unreadable status is "unknown"
            status = None
        raw = status.get("state") if status else None
        state = str(raw) if raw else "unknown"
    if state in sim_batch.BATCH_TERMINAL_STATES:
        return
    fleet.unfinished[index] = {"index": index, "job_id": job_id, "state": state}
    raise sim_batch.BatchError(
        f"shard still unfinished (--collect-partial): remote job {job_id} "
        f"is {state}; re-run --attach (without --collect-partial) or "
        "--resume to finish it",
        code="batch_shard_unfinished",
    )


def with_unfinished(
    environment: dict[str, Any] | None,
    unfinished: dict[int, dict[str, Any]],
    manifest: Any,
) -> dict[str, Any] | None:
    """``environment`` plus ``unfinished_shards[]`` (index order),
    ``resumable`` and ``shard_map`` when ``--collect-partial`` left shards."""
    if not unfinished:
        return environment
    return {
        **(environment or {}),
        "unfinished_shards": [unfinished[i] for i in sorted(unfinished)],
        "resumable": True,
        "shard_map": str(manifest.path),
    }


def attach_batch_shards(
    *,
    manifest_path: str,
    config: sim_batch.BatchConfig,
    corner_points: list[sim_batch.CornerPoint],
    netlist_path: str,
    timeout_s: float,
    keep_artifacts: bool,
    want_waveforms: bool,
    request: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    request_dir: str | None,
    shard_fingerprint: str | None,
    collect_partial: bool = False,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """``--attach <shard map>`` (issue #4620): wait for every shard a
    sharded ``--submit-only`` recorded and merge them exactly as the blocking
    sharded run does. The map must belong to this request (digest and
    fingerprint); a shard whose job is gone is reported lost, never
    resubmitted."""
    from .sim import SimError

    if os.path.basename(manifest_path) != sim_batch.BATCH_SHARDS_FILENAME:
        raise SimError(
            f"--attach {manifest_path}: expected a job id or a "
            f"{sim_batch.BATCH_SHARDS_FILENAME} shard map",
            code=sim_batch.BATCH_SHARD_MANIFEST_CODE,
        )
    try:
        with open(manifest_path, encoding="utf-8") as handle:
            document = json.load(handle)
        hosts = int(document["hosts"])
        fingerprint = str(document["fingerprint"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SimError(
            f"--attach: shard map {manifest_path} is unreadable ({exc})",
            code=sim_batch.BATCH_SHARD_MANIFEST_CODE,
        ) from exc
    mismatch = shard_map_mismatch(document, request)
    if mismatch is not None:
        raise SimError(
            f"--attach: shard map {manifest_path} was submitted from a "
            f"different request ({mismatch}); pass the request the shards "
            "were submitted from",
            code=sim_batch.BATCH_SHARD_MANIFEST_CODE,
        )
    try:
        return sim_batch._run_batch_fleet(
            corner_points=corner_points,
            netlist_path=netlist_path,
            timeout_s=timeout_s,
            keep_artifacts=keep_artifacts,
            want_waveforms=want_waveforms,
            artifacts_dir=os.path.dirname(os.path.abspath(manifest_path)),
            request=request,
            hosts=hosts,
            measurements_spec=measurements_spec,
            request_dir=request_dir,
            config=config,
            shard_fingerprint=shard_fingerprint or fingerprint,
            resume=True,
            attach_only=True,
            collect_partial=collect_partial,
        )
    except sim_batch.BatchError as exc:
        raise SimError(f"batch backend failed: {exc}", code=exc.code) from exc


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
) -> tuple[list[dict[str, Any]], str | None]:
    """``--resume``'s harvest of a retained shard map (issue #5775): collect
    the corners of every shard whose remote job is already terminal, so the
    ordinary resume flow re-dispatches only what is genuinely outstanding.

    One entry at a time -- read its remote ``status.json`` once, never wait,
    never launch (a shard the map records ``submitted`` stays for the
    dispatch, which launches it under the recorded id when the partition is
    unchanged). A terminal entry is collected through the plain attach
    machinery (:func:`sim_batch._run_batch_job` with ``attach=True``) and
    marked ``collected`` in the map; anything else is left alone. Returns
    ``(corners, engine_version)`` -- the corners a
    :func:`sim._record_batch_checkpoint` filters and records (a terminally
    *failed* shard returns unrun corners, which that filter leaves
    outstanding); ``([], None)`` when there is no map or nothing new to
    collect.

    A map written for a different request refuses with
    :data:`sim_batch.BATCH_SHARD_MANIFEST_CODE` -- never a silent attach of
    stale results.
    """
    from .sim import SimError

    path = os.path.join(artifacts_dir, sim_batch.BATCH_SHARDS_FILENAME)
    if not os.path.isfile(path):
        return [], None
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        entries = [dict(entry) for entry in document["shards"]]
        hosts = int(document["hosts"])
        str(document["fingerprint"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SimError(
            f"--resume: shard map {path} is unreadable ({exc}); delete it "
            "or drop --resume",
            code=sim_batch.BATCH_SHARD_MANIFEST_CODE,
        ) from exc
    if not entries:
        # A well-formed map listing no shards has nothing to attach, re-bill
        # or refuse over -- an inert file, not a stale one.
        return [], None
    if document.get("fingerprint") != fingerprint:
        raise mismatch_error(path, document, fingerprint, False)
    mismatch = shard_map_mismatch(document, request)
    if mismatch is not None:
        raise SimError(
            f"--resume: shard map {path} was submitted from a different "
            f"request ({mismatch}); re-run with exactly the request the "
            "shards were submitted from",
            code=sim_batch.BATCH_SHARD_MANIFEST_CODE,
        )
    by_id = {point.corner_id: point for point in corner_points}
    partition = sim_batch._shard_corner_points(corner_points, hosts)
    corners_out: list[dict[str, Any]] = []
    engine_version: str | None = None
    collected_indexes: list[int] = []
    for entry in sorted(entries, key=lambda entry: int(entry.get("index") or 0)):
        unit_ids = _entry_unit_ids(entry, by_id, partition)
        if unit_ids is None or all(unit in known for unit in unit_ids):
            continue
        if entry.get("state") == "submitted":
            # Never launched: no results exist to harvest, and launching is
            # the dispatch's decision (`launch_if_unlaunched`), never a
            # harvest's (a shard the map says was submitted is launched, not
            # re-minted, when the partition is unchanged).
            continue
        job_id = str(entry["job_id"])
        try:
            status = sim_batch._read_status(config, job_id, sim_batch._run_subprocess)
        except Exception:  # an unreadable status is "unknown", never an error
            status = None
        if str((status or {}).get("state") or "unknown") not in (
            sim_batch.BATCH_TERMINAL_STATES
        ):
            continue
        points = [by_id[unit] for unit in unit_ids]
        try:
            corners, entry_engine_version, environment = sim_batch._run_batch_job(
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
        except sim_batch.BatchError as exc:
            print(
                f"klt sim: notice: --resume could not re-attach to shard "
                f"job {job_id} ({exc}); its units stay outstanding",
                file=sys.stderr,
            )
            continue
        corners_out.extend(corners)
        if entry_engine_version is not None:
            engine_version = entry_engine_version
        # Only a shard whose outputs were actually collected is marked
        # collected -- exactly the fleet's own `_mark_collected` rule -- so a
        # terminally failed job stays outstanding for the dispatch (its
        # corners come back unrun and are filtered by the caller).
        if environment is None or not environment.get("client_collected_at"):
            continue
        collected_indexes.append(int(entry.get("index") or 0))
    if collected_indexes:
        _mark_map_collected(path, document, collected_indexes)
    if corners_out:
        from .sim import _is_unrun_batch_corner

        ran = sum(1 for corner in corners_out if not _is_unrun_batch_corner(corner))
        if ran:
            print(
                f"klt sim: --resume harvested {ran} already-completed "
                f"unit(s) from {len(collected_indexes)} shard(s) of {path}",
                file=sys.stderr,
            )
    return corners_out, engine_version


def _mark_map_collected(
    path: str, document: dict[str, Any], indexes: list[int]
) -> None:
    """Stamp ``collected`` on the harvested map entries, exactly as the
    fleet's own collect path would (issue #5775), so the map a resumed run
    leaves behind does not list as outstanding a shard whose outputs this
    run already took."""
    wanted = set(indexes)
    for entry in document.get("shards") or ():
        if int(entry.get("index") or 0) not in wanted:
            continue
        order = ["pending", "submitted", "launching", "launched", "collected"]
        current = str(entry.get("state") or "")
        if current in order and order.index(current) >= order.index("collected"):
            continue
        entry["state"] = "collected"
        if not entry.get("collected_at"):
            entry["collected_at"] = datetime.now(timezone.utc).isoformat()
    sim_batch._write_batch_receipt(path, document)
