"""Broker-free local half of ``klt artifact verify --local`` (issue #5591),
split out of :mod:`artifact_store` (per-file size ratchet, issue #5464) so
the ``--local`` mode could land without growing :mod:`artifact_store` past
its ratchet ceiling. The broker-backed ``verify_manifest`` and the manifest
plumbing (:func:`load_manifest`, :func:`select_entry`, :func:`hash_file`)
stay in :mod:`artifact_store`.

Re-hashes manifest entries against files under a root -- no broker call, no
network, no credential; the semantics and exit codes are documented in
``docs/cli/artifact.md`` (§ Local verification).
"""

from __future__ import annotations

import os
from typing import Any

from .artifact_store import (
    ArtifactError,
    _git,
    hash_file,
    load_manifest,
    select_entry,
)


def _local_root_for(path: str) -> str:
    """The ``--root`` default for a local verify of the manifest at
    ``path``: its git checkout root, else the process cwd."""
    return (
        _git(os.path.dirname(os.path.abspath(path)), "rev-parse", "--show-toplevel")
        or os.getcwd()
    )


def _verify_entry_local(
    entry: dict[str, Any], root: str, root_real: str
) -> dict[str, Any]:
    """Re-hash one entry in place under ``root``. An entry with no ``name``
    cannot resolve to a file and is reported as ``no_name``; a ``name`` that
    would resolve outside the root (an absolute path, a ``..`` climb, a
    symlink escape) refuses the run before anything is read."""
    result: dict[str, Any] = {
        "sha256": entry["sha256"],
        "name": entry.get("name"),
        "size": entry["size"],
    }
    name = entry.get("name")
    if not name:
        return {**result, "status": "no_name"}
    candidate = os.path.normpath(os.path.join(root, name))
    resolved = os.path.realpath(candidate)
    if os.path.isabs(name) or (
        resolved != root_real and not resolved.startswith(root_real + os.sep)
    ):
        raise ArtifactError(
            f"manifest entry name {name!r} resolves outside the verify root",
            "name_escapes_root",
            {"name": name, "root": root_real, "resolved": resolved},
        )
    result["path"] = candidate
    if not os.path.isfile(candidate):
        return {**result, "status": "absent"}
    observed_sha, observed_size = hash_file(candidate)
    if observed_size < entry["size"]:
        # Missing bytes: the size label carries more information than the
        # hash label here, so a truncated file is reported as a size failure
        # even though its hash deviates too.
        return {**result, "status": "size_mismatch", "observed_size": observed_size}
    if observed_sha != entry["sha256"]:
        return {
            **result,
            "status": "hash_mismatch",
            "observed_sha256": observed_sha,
            "observed_size": observed_size,
        }
    if observed_size != entry["size"]:
        return {**result, "status": "size_mismatch", "observed_size": observed_size}
    return {**result, "status": "ok"}


def verify_manifest_local(
    path: str, selector: str | None = None, root: str | None = None
) -> dict[str, Any]:
    """Verify manifest entries against files under ``root`` instead of the
    store: no broker call, no network, no credential. Each selected entry
    whose ``name`` resolves to a file under ``root`` (default: the
    manifest's git checkout root, else the process cwd) is re-hashed and its
    sha256 and size compared to the manifest entry.

    With a ``selector`` exactly one entry is checked and an ``absent`` one is
    a failure: the caller named an entry it needs. Without a selector every
    entry is checked, ``absent`` ones are reported but expected (the large
    objects live in the store, not in git), and a run in which **no** entry
    is present is refused as ``nothing_verified`` so an empty checkout can
    never look like a pass. An entry with no ``name`` is reported as
    ``no_name`` and is not locally checkable.
    """
    manifest = load_manifest(path)
    if root is None:
        root = _local_root_for(path)
    root_real = os.path.realpath(root)
    entries: list[dict[str, Any]] = manifest["entries"]
    if selector is not None:
        entries = [select_entry(manifest, selector, path)]
    results = [_verify_entry_local(entry, root, root_real) for entry in entries]
    checked = [r for r in results if r["status"] not in ("absent", "no_name")]
    if selector is None and not checked:
        raise ArtifactError(
            f"no entry of {path} resolves to a file under {root_real}",
            "nothing_verified",
            {"entries": results, "root": root_real},
        )
    # Without a selector an absent or unnamed entry is reported, not a
    # failure; with one, the caller named an entry it needs, so it is.
    not_ok = set() if selector is not None else {"absent", "no_name"}
    failed = [r for r in results if r["status"] != "ok" and r["status"] not in not_ok]
    return {
        "status": "ok" if not failed else "failed",
        "mode": "local",
        "root": root_real,
        "entry_count": len(results),
        "failed_count": len(failed),
        "entries": results,
    }
