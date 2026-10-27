"""``klt place-and-route --resume`` stage commit records (issue #4411).

Each flow stage (``floorplan``/``place``/``cts``/``route``) is a separate
OpenROAD process chained by ``<top>_<stage>.odb`` checkpoints. After a stage
finishes -- including everything the Python side derives from its stdout --
the flow writes ``<top>_<stage>.commit.json`` next to the checkpoint. A later
``--resume`` run reuses a stage only when that record exists, its input hash
matches, and every artifact it names is still on disk; the first stage that
fails the check (and every stage after it) runs normally. The hash chains the
previous stage's hash, so a changed input invalidates all later stages.

A record is written *last*, so a stage killed mid-way (partial ``.odb``) has
no record and is never reused. Resuming inside a stage is out of scope.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from ._provenance import sha256_file

COMMIT_SCHEMA = "klt.place_and_route.stage_commit/1"

#: Request keys that do not influence what a stage computes; the per-stage
#: script text and input-file hashes already pin everything that does.
_REQUEST_HASH_EXCLUDE = ("target_stage",)


def commit_path(output_dir: str, hdl_toplevel: str, stage: str) -> str:
    return os.path.join(output_dir, f"{hdl_toplevel}_{stage}.commit.json")


def input_files(
    base: list[str | None], macro_liberty_paths: list[str], macros: list[dict[str, Any]]
) -> list[str | None]:
    """Every file a stage's output depends on, in a stable order."""
    return [*base, *macro_liberty_paths, *(macro["lef"] for macro in macros)]


def stage_input_hash(
    *,
    previous_hash: str | None,
    request: dict[str, Any],
    script_lines: list[str],
    input_files: list[str | None],
) -> str:
    """Digest of everything the stage's output depends on."""
    digest = hashlib.sha256()
    digest.update((previous_hash or "").encode())
    trimmed = {k: v for k, v in request.items() if k not in _REQUEST_HASH_EXCLUDE}
    digest.update(json.dumps(trimmed, sort_keys=True, default=str).encode())
    digest.update("\n".join(script_lines).encode())
    for path in input_files:
        digest.update((sha256_file(path) or "-").encode())
    return digest.hexdigest()


def load_commit(
    output_dir: str, hdl_toplevel: str, stage: str, input_hash: str
) -> dict[str, Any] | None:
    """The committed record for ``stage`` iff it is reusable, else ``None``."""
    try:
        with open(commit_path(output_dir, hdl_toplevel, stage), encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return None
    if (
        not isinstance(record, dict)
        or record.get("schema") != COMMIT_SCHEMA
        or record.get("input_hash") != input_hash
        or not isinstance(record.get("stage_entry"), dict)
        or not isinstance(record.get("engine_logs"), list)
    ):
        return None
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, list) or not all(
        isinstance(name, str) and os.path.isfile(os.path.join(output_dir, name))
        for name in artifacts
    ):
        return None
    return record


def write_commit(
    output_dir: str,
    hdl_toplevel: str,
    stage: str,
    *,
    input_hash: str,
    artifacts: list[str],
    stage_entry: dict[str, Any],
    engine_logs: list[dict[str, Any]],
    placement_routability: dict[str, Any],
) -> None:
    """Atomically write the record; a failure to write is non-fatal (the
    stage simply is not resumable)."""
    record = {
        "schema": COMMIT_SCHEMA,
        "stage": stage,
        "input_hash": input_hash,
        "artifacts": artifacts,
        "stage_entry": stage_entry,
        "engine_logs": engine_logs,
        "placement_routability": placement_routability,
    }
    path = commit_path(output_dir, hdl_toplevel, stage)
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        try:
            os.unlink(tmp)
        except OSError:
            pass


def invalidate(output_dir: str, hdl_toplevel: str, stage: str) -> None:
    """Drop a stale record before its stage re-runs, so a crash mid-stage
    cannot leave an old record next to a half-written ``.odb``."""
    try:
        os.unlink(commit_path(output_dir, hdl_toplevel, stage))
    except OSError:
        pass


def stage_artifacts(
    hdl_toplevel: str, stage: str, checkpoint_path: str, metrics_path: str
) -> list[str]:
    """File names (relative to the output dir) a committed ``stage`` must keep."""
    names = [os.path.basename(checkpoint_path), os.path.basename(metrics_path)]
    if stage == "route":
        names += [f"{hdl_toplevel}.def", f"{hdl_toplevel}.v"]
    return names


def reuse_or_invalidate(
    output_dir: str,
    hdl_toplevel: str,
    stage: str,
    input_hash: str,
    artifacts: list[str],
    *,
    attempt: bool,
) -> dict[str, Any] | None:
    """The record to reuse for ``stage``, or ``None`` after dropping any stale
    record because the stage must (re)run. ``attempt`` is False when resume is
    off or an earlier stage already re-ran."""
    record = (
        load_commit(output_dir, hdl_toplevel, stage, input_hash) if attempt else None
    )
    if record is not None and set(artifacts) <= set(record["artifacts"]):
        record["placement_routability"] = record.get("placement_routability") or {}
        return record
    invalidate(output_dir, hdl_toplevel, stage)
    return None


def annotate_response(
    response: dict[str, Any], resume: bool, resumed_stages: list[str]
) -> None:
    """Additive response field (issue #4411): present only under ``--resume``."""
    if resume:
        response["resume"] = {"resumed_stages": resumed_stages}
