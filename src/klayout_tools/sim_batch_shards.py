"""Request identity and shard scoping for sharded ``--submit-only`` /
``--attach`` (issues #4620, #4878); split out of :mod:`sim_batch_async`."""

from __future__ import annotations

import hashlib
import json
import os
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
    batch = request.get("batch")
    if isinstance(batch, dict) and any(
        k in batch for k in _SUBMISSION_ONLY_BATCH_FIELDS
    ):
        kept = {
            k: v for k, v in batch.items() if k not in _SUBMISSION_ONLY_BATCH_FIELDS
        }
        request = {**request, "batch": kept}
    return _canonical_sha256(request)


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


def manifest_mismatch_error(
    path: str,
    stored_fingerprint: Any,
    fingerprint: Any,
    stored_hosts: Any,
    submit_only: bool,
) -> sim_batch.SimError:
    """Refusal for a shard map written for a different request. Issue #4979:
    a ``--submit-only`` caller passed no ``--resume``, so name what they ran
    and keep the first request's map (and its ``--attach`` path) intact."""
    difference = sim_batch._manifest_difference(
        stored_fingerprint, fingerprint, stored_hosts, stored_hosts
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
    return sim_batch.SimError(message, code=sim_batch.BATCH_SHARD_MANIFEST_CODE)
