"""``klt signoff --attest 2 --input STREAM [--inventory FILE]`` (issues #4188, #4767).

Item 2 (Layout) is attested from the committed layout stream itself; see
:mod:`.signoff_attest` for the inventory-based items and the shared command
machinery this module reuses.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from ._provenance import sha256_file
from .build_identity import build_version, klayout_version
from .env_provenance import find_repo_root
from .signoff import (
    _HASH_KIND_BYTES,
    _HASH_KIND_GEOMETRY,
    _REGENERATED_HASH_RE,
    SCHEMA_VERSION,
)
from .signoff_attest import (
    _ITEM_TITLES,
    HASH_ALGORITHM,
    PRODUCER,
    STREAM_ATTESTABLE_ITEM,
    AttestationError,
    _audit_command_checks,
    _command_check,
    _drift_result,
    _load_committed_attestation,
    _read_inventory,
    _self_check,
    _skipped_check,
    _validate_header,
    _validated_command_checks,
)

#: ``--hash-kind`` values a stream attestation's regeneration claim accepts.
REGENERATION_HASH_KINDS: tuple[str, ...] = (_HASH_KIND_BYTES, _HASH_KIND_GEOMETRY)

#: A database unit counts as "on grid" when it is a positive length that
#: divides one micron into a whole number of database units (0.001 -> 1000),
#: within this relative tolerance for float representation.
_DBU_GRID_TOLERANCE = 1e-6


def _stream_checks(stream: str, digest: str) -> list[dict[str, Any]]:
    """The three item-2 checks, in fixed order: stream readable (loads in
    ``pya``), non-empty top cell(s), database unit on grid. A stream that
    cannot be read fails all three -- nothing else can be said about it."""
    layout = None
    try:
        import klayout.db as kdb

        layout = kdb.Layout()
        layout.read(stream)
    except Exception:
        layout = None
    checks: list[dict[str, Any]] = [
        {
            "check": "stream_readable",
            "role": "layout",
            "passed": layout is not None,
            "content_hash": digest,
        }
    ]
    top_names: list[str] = []
    non_empty = False
    dbu = None
    if layout is not None:
        tops = list(layout.top_cells())
        top_names = sorted(cell.name for cell in tops)
        non_empty = bool(tops) and all(not cell.bbox().empty() for cell in tops)
        dbu = float(layout.dbu)
    checks.append(
        {
            "check": "top_cell_non_empty",
            "role": "layout",
            "passed": non_empty,
            "top_cells": top_names,
        }
    )
    on_grid = False
    if dbu is not None and dbu > 0:
        per_micron = 1.0 / dbu
        on_grid = abs(per_micron - round(per_micron)) <= _DBU_GRID_TOLERANCE * max(
            per_micron, 1.0
        )
    checks.append(
        {"check": "dbu_on_grid", "role": "layout", "passed": on_grid, "dbu": dbu}
    )
    return checks


def _validate_regeneration_claim(
    generator: str | None, regenerated_hash: str | None, hash_kind: str
) -> None:
    """Refuse a malformed ``--generator`` / ``--regenerated-hash`` /
    ``--hash-kind`` combination with :class:`AttestationError`."""
    if (generator is None) != (regenerated_hash is None):
        raise AttestationError(
            "--generator and --regenerated-hash must be given together: a "
            "regeneration claim needs both the command and the hash it produced"
        )
    if hash_kind not in REGENERATION_HASH_KINDS:
        raise AttestationError(
            f"--hash-kind must be one of {', '.join(REGENERATION_HASH_KINDS)}; "
            f"got '{hash_kind}'"
        )
    if generator is not None and not generator.strip():
        raise AttestationError("--generator must be a non-empty command string")
    if regenerated_hash is not None and not _REGENERATED_HASH_RE.match(
        regenerated_hash
    ):
        raise AttestationError(
            "--regenerated-hash must be 'sha256:' followed by 64 lowercase hex digits"
        )


def _resolve_stream(stream_path: str) -> tuple[str, str]:
    """``(absolute path, repository-relative path)`` of a committed stream, or
    :class:`AttestationError` for a missing, symlinked, non-file or
    non-repository path."""
    if not os.path.exists(stream_path):
        raise AttestationError(f"stream not found: {stream_path}")
    if os.path.islink(stream_path):
        raise AttestationError(
            f"stream '{stream_path}' is a symlink -- attest the real file so "
            "the pinned path names the bytes that were hashed"
        )
    if not os.path.isfile(stream_path):
        raise AttestationError(f"stream is not a file: {stream_path}")
    absolute = os.path.realpath(os.path.abspath(stream_path))
    root = find_repo_root(os.path.dirname(absolute))
    if root is None:
        raise AttestationError(
            f"stream '{stream_path}' is not inside a git repository -- the "
            "envelope pins it as a repository-relative path"
        )
    return absolute, os.path.relpath(absolute, root).replace(os.sep, "/")


def build_stream_attestation(
    stream_path: str,
    *,
    generator: str | None = None,
    regenerated_hash: str | None = None,
    hash_kind: str = _HASH_KIND_BYTES,
    inventory_path: str | None = None,
    committed_checks: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """``klt signoff --attest 2 --input STREAM``: run the item-2 checks on a
    committed GDS/OASIS stream and return the artifact-anchored generic
    envelope (``status`` ``"pass"`` iff every check passed).

    ``generator`` and ``regenerated_hash`` must be given together; they
    populate ``provenance.regeneration`` exactly as the grader reads it
    (:func:`~.signoff._regeneration_claim`). The generator is echoed, never
    run. Raises :class:`AttestationError` for a missing, symlinked or
    non-repository stream or a malformed regeneration claim.

    ``inventory_path`` (issue #4767) is an optional item-2 inventory declaring
    ``checks`` -- the user's own geometric / net-width checkers, run as item-10
    style commands (``role: "audit"``) after the three built-in checks. The
    stream is not an inventory field: a command names it in its own ``argv``.
    ``committed_checks`` is passed only by :func:`check_stream_attestation`
    (``local_only`` commands skip). Without an inventory the envelope is
    unchanged."""
    _validate_regeneration_claim(generator, regenerated_hash, hash_kind)
    absolute, stream_rel = _resolve_stream(stream_path)
    raw_digest = sha256_file(absolute)
    if raw_digest is None:
        raise AttestationError(f"could not read stream '{stream_path}'")
    stream_hash = f"sha256:{raw_digest}"
    checks = _stream_checks(absolute, stream_hash)
    inventory_block: dict[str, Any] | None = None
    if inventory_path is not None:
        inventory, inventory_hash = _read_inventory(inventory_path)
        inv_absolute = os.path.realpath(os.path.abspath(inventory_path))
        root = find_repo_root(os.path.dirname(absolute))
        inv_root = find_repo_root(os.path.dirname(inv_absolute))
        if root is None or inv_root != root:
            raise AttestationError(
                f"inventory '{inventory_path}' must be in the same git "
                "repository as the stream"
            )
        _validate_header(inventory, STREAM_ATTESTABLE_ITEM, inventory_path)
        specs = _validated_command_checks(inventory, root, STREAM_ATTESTABLE_ITEM)

        def command(
            spec: Mapping[str, Any], role: str, **identity: Any
        ) -> dict[str, Any]:
            if committed_checks is not None and spec.get("local_only"):
                return _skipped_check(spec, role, committed_checks, **identity)
            return _command_check(root, spec, role, **identity)

        checks.extend(_audit_command_checks(specs, command))
        inventory_block = {
            "path": {
                "path": os.path.relpath(inv_absolute, root).replace(os.sep, "/"),
                "scope": "repo",
            },
            "content_hash": inventory_hash,
            "schema_version": inventory["schema_version"],
        }
    passed_count = sum(1 for check in checks if check["passed"])
    status = "pass" if passed_count == len(checks) else "fail"
    provenance: dict[str, Any] = {
        "klt_version": build_version(),
        "klayout_version": klayout_version(),
        "pdk": None,
        "deck": None,
        "input": {
            "path": {"path": stream_rel, "scope": "repo"},
            "content_hash": stream_hash,
        },
    }
    if generator is not None:
        regeneration: dict[str, Any] = {
            "generator": generator,
            "regenerated_hash": regenerated_hash,
        }
        if hash_kind != _HASH_KIND_BYTES:
            regeneration["hash_kind"] = hash_kind
        provenance["regeneration"] = regeneration
    envelope: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "generic",
        "status": status,
        "t1_item": STREAM_ATTESTABLE_ITEM,
        "summary": (
            f"T1 item 2 ({_ITEM_TITLES[2]}): {passed_count}/{len(checks)} "
            f"attestation checks passed ({PRODUCER})"
        ),
        "source": stream_rel,
        "attestation": {
            "producer": PRODUCER,
            "hash_algorithm": HASH_ALGORITHM,
            "check_count": len(checks),
            "passed_count": passed_count,
            "checks": checks,
        },
        "provenance": provenance,
    }
    if inventory_block is not None:
        envelope["attestation"]["inventory"] = inventory_block
    _self_check(envelope, STREAM_ATTESTABLE_ITEM, stream_hash)
    return envelope


def check_stream_attestation(
    stream_path: str,
    committed_path: str,
    *,
    generator: str | None = None,
    regenerated_hash: str | None = None,
    hash_kind: str = _HASH_KIND_BYTES,
    inventory_path: str | None = None,
) -> dict[str, Any]:
    """``klt signoff --attest 2 --input STREAM --check COMMITTED``: re-attest
    and diff against the committed envelope, writing nothing. Same result
    shape as :func:`check_attestation`."""
    committed = _load_committed_attestation(committed_path)
    committed_checks = (committed.get("attestation") or {}).get("checks")
    fresh = build_stream_attestation(
        stream_path,
        generator=generator,
        regenerated_hash=regenerated_hash,
        hash_kind=hash_kind,
        inventory_path=inventory_path,
        committed_checks=committed_checks if isinstance(committed_checks, list) else [],
    )
    return _drift_result(committed_path, committed, fresh)
