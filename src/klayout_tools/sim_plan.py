"""``klt sim-plan``: split one ``klt sim`` request into process shards (issue #3423).

The planner is the producer side of :mod:`klayout_tools.sim_merge`: given one
``klt sim`` request it writes one request file per ``corners.process[]`` entry
plus the shard manifest ``klt sim-merge`` consumes. Each manifest shard's
``corners`` list comes from running :func:`klayout_tools.sim._expand_corners`
on that shard's own narrowed request -- the very expansion ``klt sim`` runs --
so the ids the merge matches reports against cannot drift from what the shard
run will emit.

Shard files are written next to the source request: ``klt sim`` resolves a
request's relative paths (netlist, model libraries) against the request's own
directory, so a copy elsewhere would silently point at nothing.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from typing import Any

from . import sim, sim_merge

PLAN_SCHEMA_VERSION = 1
SHARD_AXES = ("process",)

_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


class PlanError(Exception):
    """A request that cannot be planned into mergeable shards."""


def _dump(payload: dict[str, Any]) -> bytes:
    """The canonical on-disk form: sorted keys, 2-space indent, trailing newline."""
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _shard_id(entry: Any) -> str:
    name, _, _ = sim._parse_process_entry(entry)
    return str(name)


def _file_token(shard_id: str) -> str:
    return _UNSAFE_ID_CHARS.sub("_", shard_id)


def _check_plannable(request: dict[str, Any], axis: str) -> list[Any]:
    if axis not in SHARD_AXES:
        raise PlanError(
            f"unsupported shard axis {axis!r} (supported: {', '.join(SHARD_AXES)})"
        )
    if "decks" in request:
        raise PlanError(
            "deck-list requests cannot be planned into shards: klt sim-merge "
            "refuses deck-list reports"
        )
    if request.get("monte_carlo") is not None:
        raise PlanError(
            "Monte Carlo requests cannot be planned into shards: klt sim-merge "
            "does not support monte_carlo reports"
        )
    corners = request.get("corners") or {}
    if "units" in corners:
        raise PlanError(
            "corners.units requests have no process axis to shard; use corners.process"
        )
    entries = corners.get("process")
    if not isinstance(entries, list) or len(entries) < 2:
        raise PlanError(
            "sharding by process needs corners.process with at least two "
            "entries; a single-value or missing axis is already one shard"
        )
    return entries


def plan_shards(request: dict[str, Any], axis: str = "process") -> list[dict[str, Any]]:
    """Plan the shards of ``request``: ``[{"id", "request", "corners"}]``.

    Each shard's ``request`` is a deep copy of ``request`` with
    ``corners.process`` narrowed to that one entry. Shards are in the
    request's axis order, so concatenating their ``corners`` reproduces the
    corner order of one request over the whole grid. Raises :class:`PlanError`
    for a request merge cannot take, and for a shard that exclusions empty
    (a manifest shard needs at least one corner).
    """
    entries = _check_plannable(request, axis)
    shards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        try:
            shard_id = _shard_id(entry)
        except sim.SimError as exc:
            raise PlanError(str(exc)) from None
        if shard_id in seen:
            raise PlanError(f"corners.process names {shard_id!r} twice")
        seen.add(shard_id)
        shard_request = copy.deepcopy(request)
        shard_request["corners"]["process"] = [copy.deepcopy(entry)]
        try:
            points = sim._expand_corners(
                shard_request["corners"], shard_request.get("exclude") or []
            )
        except sim.SimError as exc:
            raise PlanError(str(exc)) from None
        if not points:
            raise PlanError(
                f"shard {shard_id!r} has no corners left after exclude; drop "
                "that process or relax the exclusion"
            )
        shards.append(
            {
                "id": shard_id,
                "request": shard_request,
                "corners": [point.corner_id for point in points],
            }
        )
    flat = [cid for shard in shards for cid in shard["corners"]]
    if len(flat) != len(set(flat)):
        raise PlanError("two shards plan the same corner id; the plan is ambiguous")
    return shards


def write_plan(request_path: str, axis: str = "process") -> dict[str, Any]:
    """Plan ``request_path`` and write shard requests + manifest beside it.

    Returns the report payload (``schema_version``, ``axis``, ``manifest``,
    ``shards[]``). All paths in the manifest are relative to the manifest's
    directory (the request's directory), the form ``klt sim-merge`` resolves.
    """
    try:
        request = sim.load_request(request_path)
    except sim.SimError as exc:
        raise PlanError(str(exc)) from None
    shards = plan_shards(request, axis)

    directory = os.path.dirname(os.path.abspath(request_path))
    stem = os.path.splitext(os.path.basename(request_path))[0]
    tokens = [_file_token(shard["id"]) for shard in shards]
    if len(set(tokens)) != len(tokens):
        raise PlanError(
            "shard ids collide once made filename-safe; rename the processes"
        )

    manifest_shards = []
    report_shards = []
    for shard, token in zip(shards, tokens, strict=True):
        name = f"{stem}.shard-{token}.json"
        data = _dump(shard["request"])
        with open(os.path.join(directory, name), "wb") as handle:
            handle.write(data)
        digest = hashlib.sha256(data).hexdigest()
        manifest_shards.append(
            {
                "id": shard["id"],
                "request": name,
                "request_sha256": digest,
                "corners": shard["corners"],
            }
        )
        report_shards.append(
            {
                "id": shard["id"],
                "request": name,
                "request_sha256": digest,
                "corner_count": len(shard["corners"]),
            }
        )
    manifest_name = f"{stem}.manifest.json"
    with open(os.path.join(directory, manifest_name), "wb") as handle:
        handle.write(
            _dump(
                {
                    "schema_version": sim_merge.MANIFEST_SCHEMA_VERSION,
                    "shards": manifest_shards,
                }
            )
        )
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "axis": axis,
        "manifest": manifest_name,
        "shard_count": len(report_shards),
        "shards": report_shards,
    }
