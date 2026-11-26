"""Item 1 recorded freshness for derived files and their declared sources."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from typing import Any

from ._provenance import sha256_file
from .signoff_base import AttestationError
from .signoff_git_source import GitPath, git_entry, git_source, hex_digest


def _recorded_sources(
    raw: Any, field: str, root: str, version: int
) -> Iterator[tuple[str | GitPath, str]]:
    from .signoff_attest import _normalize_repo_path

    if isinstance(raw, Mapping):
        entries = [{"path": path, "sha256": digest} for path, digest in raw.items()]
    elif isinstance(raw, list) and version >= 2:
        entries = raw
    else:
        raise AttestationError(
            f"'{field}.sources' must map paths to digests or list schema 2 sources"
        )
    for i, entry in enumerate(entries):
        where = f"{field}.sources[{i}]"
        if git_entry(entry):
            yield git_source(entry, where)
        elif isinstance(entry, Mapping) and set(entry) == {"path", "sha256"}:
            path = _normalize_repo_path(entry["path"], where, root)
            yield path, hex_digest(entry["sha256"], where)
        else:
            raise AttestationError(
                f"'{where}' must declare path and sha256 or a complete Git source"
            )


def validated_recorded(
    raw: Any, field: str, root: str, sources: set[str | GitPath], version: int
) -> dict[str, Any]:
    """Validate a ``derived[]`` entry's ``recorded`` freshness record: the
    derived file's digest plus the digest of every source it was built from."""
    if not isinstance(raw, Mapping):
        raise AttestationError(f"'{field}' must be an object")
    unknown = sorted(set(raw) - {"derived_sha256", "sources"})
    if unknown:
        raise AttestationError(f"'{field}' has unknown field(s): {', '.join(unknown)}")
    derived_sha256 = hex_digest(raw.get("derived_sha256"), f"{field}.derived_sha256")
    recorded_sources = raw.get("sources")
    if not recorded_sources:
        raise AttestationError(f"'{field}.sources' must be non-empty")
    normalized: dict[str | GitPath, str] = {}
    for path, digest in _recorded_sources(recorded_sources, field, root, version):
        if path not in sources:
            raise AttestationError(
                f"'{field}.sources' lists '{path}', which is not in the "
                "inventory's top-level 'sources'"
            )
        if path in normalized:
            raise AttestationError(f"'{field}.sources' lists '{path}' more than once")
        normalized[path] = digest
    return {"derived_sha256": derived_sha256, "sources": normalized}


def recorded_hash_check(
    root: str, entry: Mapping[str, Any], git_hashes: Mapping[GitPath, str | None]
) -> dict[str, Any]:
    """Compare the derived file and each recorded source against the digests
    the inventory recorded; no command is run. ``mismatches`` names every
    differing path with its expected and actual digest (``None`` = missing)."""
    recorded = entry["recorded"]
    expected = {entry["path"]: recorded["derived_sha256"], **recorded["sources"]}
    mismatches = []
    for path in sorted(expected, key=lambda p: (isinstance(p, GitPath), p)):
        if isinstance(path, GitPath):
            actual = git_hashes[path]
            location = path.binding()
        else:
            actual = sha256_file(os.path.join(root, path))
            location = path
        if actual != expected[path]:
            mismatches.append(
                {"path": location, "expected": expected[path], "actual": actual}
            )
    check: dict[str, Any] = {
        "check": "recorded_hash",
        "role": "freshness",
        "target": entry["path"],
        "generator": entry["generator"],
        "passed": not mismatches,
    }
    if mismatches:
        check["mismatches"] = mismatches
    return check
