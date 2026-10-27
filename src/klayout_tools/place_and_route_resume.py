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

from ._provenance import _content_hash

COMMIT_SCHEMA = "klt.place_and_route.stage_commit/1"

#: Request keys that do not influence what a stage computes; the per-stage
#: script text and input-file hashes already pin everything that does.
_REQUEST_HASH_EXCLUDE = ("target_stage",)


def commit_path(output_dir: str, hdl_toplevel: str, stage: str) -> str:
    return os.path.join(output_dir, f"{hdl_toplevel}_{stage}.commit.json")


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
        digest.update(((path and _content_hash(path)) or "-").encode())
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
