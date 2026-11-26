"""Parse, bind and verify the members of a generic signoff input set."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ._digest import content_hash_bytes, sha256_file
from .env_provenance import find_repo_root
from .signoff_artifact import resolve_input_artifact_value
from .signoff_base import AttestationError, input_set_digest
from .signoff_git_source import GitPath, git_scoped


def member_identity(path: Any) -> tuple[str, ...] | None:
    if git_scoped(path):
        if set(path) != {"repo", "commit", "path", "scope"}:
            return None
        try:
            git = GitPath.parse(path, "Git member")
        except AttestationError:
            return None
        return ("git", git.repo, git.commit, git.path)
    if isinstance(path, Mapping) and path.get("scope") == "repo":
        path = path.get("path")
    return ("path", path) if isinstance(path, str) and path else None


def member_set_digest(members: Sequence[tuple[Any, str]]) -> str:
    """Historical sha256sum lines for local sets, typed JSON records for mixed sets."""
    records = []
    for path, digest in members:
        identity = member_identity(path)
        if identity is None:
            raise ValueError("input set member has no identity")
        records.append((*identity, digest))
    if any(record[0] == "git" for record in records):
        data = json.dumps(sorted(records), ensure_ascii=True, separators=(",", ":"))
        return content_hash_bytes(b"klt-git-input-set-v1\0" + data.encode("ascii"))
    return input_set_digest((record[1], record[-1]) for record in records)


def generic_input_set(
    envelope: Mapping[str, Any],
) -> tuple[bool, list[tuple[Any, str]] | None]:
    """``(declared, members)`` for a generic envelope's opt-in
    ``provenance.input.paths`` (issue #3044): a non-empty list of ``{path,
    content_hash}`` members, ``path`` being as for ``input.path``.

    ``declared`` is whether the key is present at all; ``members`` is the
    ``(path value, recorded hash)`` pairs, or ``None`` when it is present
    but malformed (not a list, empty, a member of the wrong shape, a
    non-string or empty ``content_hash``, or two members naming the same
    path). A malformed set never falls back to ``input.path``."""
    provenance = envelope.get("provenance")
    input_block = provenance.get("input") if isinstance(provenance, Mapping) else None
    if not isinstance(input_block, Mapping) or "paths" not in input_block:
        return False, None
    raw = input_block["paths"]
    if not isinstance(raw, list) or not raw:
        return True, None
    members: list[tuple[Any, str]] = []
    names: set[tuple[str, ...]] = set()
    for entry in raw:
        if not isinstance(entry, Mapping):
            return True, None
        path = entry.get("path")
        if git_scoped(path) and envelope.get("t1_item") != 1:
            return True, None
        name = member_identity(path)
        recorded = entry.get("content_hash")
        if name is None or not isinstance(recorded, str) or not recorded:
            return True, None
        if name in names:
            return True, None
        names.add(name)
        members.append((entry["path"], recorded))
    return True, members


def _local_status(
    path: Any,
    recorded: str,
    spec: Mapping[str, Any],
    evidence_dir: str | None,
    repo_root: str | Path | None,
) -> str:
    status = "unverifiable"
    for candidate in resolve_input_artifact_value(path, spec, evidence_dir, repo_root):
        digest = sha256_file(candidate)
        if digest is not None:
            if f"sha256:{digest}" == recorded:
                return "verified"
            status = "stale"
    return status


def verify_generic_input_set(
    members: list[tuple[Any, str]],
    spec: Mapping[str, Any],
    evidence_file: str | None,
    repo_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Re-hash local candidates and pinned Git blobs for the member report."""
    evidence_dir = (
        os.path.dirname(os.path.abspath(evidence_file)) if evidence_file else None
    )
    results: list[dict[str, Any]] = []
    for path_value, recorded in members:
        if git_scoped(path_value):
            root = repo_root or find_repo_root(
                evidence_dir or spec.get("cwd") or os.getcwd()
            )
            location = GitPath.parse(path_value, "Git member")
            digest = location.sha256(root)
            status = (
                "unverifiable"
                if digest is None
                else ("verified" if recorded == f"sha256:{digest}" else "stale")
            )
        else:
            status = _local_status(path_value, recorded, spec, evidence_dir, repo_root)
        results.append({"path": path_value, "content_hash": recorded, "status": status})
    return results
