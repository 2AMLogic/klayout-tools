"""Presence-only and canonical-YAML inventory entries (issues #5615, #5862).

Split out of :mod:`klayout_tools.signoff_attest` (file-size ratchet). A
path-list entry may be a mapping ``{"path": "...", "attest": ...}`` alongside
the plain-string form. ``"attest": "content"`` (the explicit default) hashes
raw bytes; ``"attest": "presence"`` binds existence only; ``"attest":
"yaml"`` binds a canonical parse of the file (with an optional ``select``
dotted subtree), so comments, whitespace and key order are not drift while
every key/value change still is (:mod:`.signoff_attest_canonical_yaml`).

A ``present`` check's verdict is already presence-only (``passed`` is "the
file exists"); content immutability is enforced by the *consumers* of the
stamped ``content_hash`` -- the ``--check`` envelope diff and the
``--manifest`` grading re-hash. Both are key-driven, so a presence-only
entry simply omits the key (never ``null``: that is the missing-file
marker) and carries ``"attest": "presence"`` so the semantics are part of
the envelope. A yaml entry stamps the canonical digest as ``content_hash``
(with its own ``hash_algorithm`` and, when given, ``select``) plus the raw
bytes as ``raw_content_hash`` -- informational for ``--check``, excluded
from the drift diff by :mod:`.signoff_attest_drift`.

Only item 10's ``hygiene`` list accepts either option. Item 1's ``sources``
are re-hashed by ``input_binding()`` regardless of the check dict, so the
option would silently not work there; an item 9 bench *is* the attested
file.

The reduction mirrors :func:`signoff_attest_item9.split_testbenches` and is
generic (driven by the item's path lists), so a later entry key adds to
:data:`ENTRY_KEYS` instead of bolting on a second parallel entry form.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from . import signoff_attest_canonical_yaml as canonical_yaml
from ._provenance import sha256_file

#: The mapping-entry keys a path-list entry accepts.
ENTRY_KEYS = frozenset({"path", "attest", "select"})

#: Accepted ``attest`` values; ``content`` is the default.
ATTEST_VALUES = ("content", "presence", "yaml")

#: ``{item: path-list fields that accept "attest": "presence"}``.
PRESENCE_FIELDS: dict[int, frozenset[str]] = {10: frozenset({"hygiene"})}

#: ``{item: path-list fields that accept "attest": "yaml"}`` -- the same
#: restriction as :data:`PRESENCE_FIELDS`: elsewhere the file's bytes are
#: the attested property, and item 1's binding re-hashes raw regardless.
YAML_FIELDS: dict[int, frozenset[str]] = {10: frozenset({"hygiene"})}


def _error(message: str) -> Exception:
    """An ``AttestationError`` (imported lazily: ``signoff_attest`` imports us)."""
    from klayout_tools.signoff_attest import AttestationError

    return AttestationError(message)


def _validated_select(entry: Mapping[str, Any], where: str) -> str | None:
    """The entry's ``select``: a non-empty dotted path without empty
    segments, or ``None`` when absent. Only meaningful with
    ``attest: "yaml"`` (checked by :func:`_check_mode`)."""
    select = entry.get("select")
    if select is None:
        return None
    if not isinstance(select, str) or not select.strip():
        raise _error(f"'{where}.select' must be a non-empty dotted path string")
    if any(not segment.strip() for segment in select.split(".")):
        raise _error(f"'{where}.select' must have no empty segment: got {select!r}")
    return select


def _check_mode(
    mode: Any, select: str | None, where: str, item: int, field: str
) -> None:
    """Refuse an ``attest``/``select`` combination this path list does not
    take: an unknown value; ``select`` outside ``attest: "yaml"``; and the
    item-10-``hygiene``-only restriction both non-default modes share
    (elsewhere the file's bytes are the attested property)."""
    if mode not in ATTEST_VALUES:
        raise _error(
            f"'{where}.attest' must be one of "
            f"{', '.join(repr(v) for v in ATTEST_VALUES)}, got {mode!r}"
        )
    if select is not None and mode != "yaml":
        raise _error(
            f"'{where}.select' is only accepted with 'attest': 'yaml', not {mode!r}"
        )
    if mode == "content":
        return
    fields = (PRESENCE_FIELDS if mode == "presence" else YAML_FIELDS).get(
        item, frozenset()
    )
    if field not in fields:
        raise _error(
            f"'{where}.attest' = {mode!r} is only accepted on an item 10 "
            "'hygiene' entry -- elsewhere the file's bytes are the "
            "attested property"
        )


def split_entries(
    inventory: Mapping[str, Any],
    item: int,
    path_lists: Sequence[tuple[str, str]],
    root: str,
    normalize_path: Callable[[Any, str, str], str],
) -> tuple[dict[str, Any], frozenset[str], dict[str, str | None]]:
    """Reduce mapping-form path-list entries to plain paths.

    Returns ``(inventory_with_plain_paths, presence_only_paths,
    yaml_selects)`` -- ``yaml_selects`` maps each ``attest: "yaml"`` entry's
    normalised path to its ``select`` (``None`` = whole document). String
    entries pass through untouched, so an old-form inventory is returned as
    an equal mapping and validates exactly as before. A malformed container
    is left for the generic path-list validator to refuse."""
    out = dict(inventory)
    presence: set[str] = set()
    yaml_selects: dict[str, str | None] = {}
    for field, _role in path_lists:
        entries = inventory.get(field)
        if not isinstance(entries, list):
            continue
        paths: list[Any] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                paths.append(entry)
                continue
            where = f"{field}[{index}]"
            unknown = sorted(set(entry) - ENTRY_KEYS)
            if unknown:
                raise _error(
                    f"'{where}' has unknown field(s): {', '.join(unknown)} "
                    f"(accepted: {', '.join(sorted(ENTRY_KEYS))})"
                )
            if "path" not in entry:
                raise _error(f"'{where}' must declare 'path'")
            mode = entry.get("attest", "content")
            select = _validated_select(entry, where)
            _check_mode(mode, select, where, item, field)
            if mode == "presence":
                presence.add(normalize_path(entry["path"], field, root))
            elif mode == "yaml":
                yaml_selects[normalize_path(entry["path"], field, root)] = select
            paths.append(entry["path"])
        out[field] = paths
    return out, frozenset(presence), yaml_selects


def _yaml_check(root: str, path: str, role: str, select: str | None) -> dict[str, Any]:
    """A ``present`` check bound by canonical parse (issue #5862):
    ``content_hash`` is the ``sha256-canonical-yaml-v1`` digest of the whole
    document or the ``select`` subtree, and the raw bytes ride along as
    ``raw_content_hash`` (informational for ``--check``, excluded from the
    drift diff). A file that cannot be canonicalised deterministically is
    an ``AttestationError`` naming the path -- never a silent fallback to
    byte-hashing."""
    where = f"hygiene entry '{path}'"
    check: dict[str, Any] = {
        "check": "present",
        "role": role,
        "path": path,
        "attest": "yaml",
    }
    if select is not None:
        check["select"] = select
    check["hash_algorithm"] = canonical_yaml.HASH_ALGORITHM
    try:
        with open(os.path.join(root, path), "rb") as handle:
            data = handle.read()
    except OSError:
        check["passed"] = False
        check["content_hash"] = None
        return check
    try:
        content = canonical_yaml.content_digest(data, select, where)
    except canonical_yaml.CanonicalYamlError as exc:
        raise _error(f"cannot attest {exc}") from exc
    check["passed"] = True
    check["content_hash"] = content
    check["raw_content_hash"] = f"sha256:{hashlib.sha256(data).hexdigest()}"
    return check


def file_check(
    root: str,
    path: str,
    role: str,
    *,
    presence: bool = False,
    yaml: bool = False,
    select: str | None = None,
) -> dict[str, Any]:
    """A ``present`` check for ``path``. A ``presence`` entry omits
    ``content_hash`` (never ``null``) and records ``"attest": "presence"``;
    a ``yaml`` entry records the canonical digest (:func:`_yaml_check`)."""
    if yaml:
        return _yaml_check(root, path, role, select)
    digest = sha256_file(os.path.join(root, path))
    check: dict[str, Any] = {
        "check": "present",
        "role": role,
        "path": path,
        "passed": digest is not None,
    }
    if presence:
        check["attest"] = "presence"
    else:
        check["content_hash"] = f"sha256:{digest}" if digest is not None else None
    return check


def path_checks(
    root: str, normalized: Mapping[str, Any], field: str, role: str
) -> list[dict[str, Any]]:
    """The ``present`` checks for one path list of ``normalized``, sorted by
    path: the per-entry mode dispatch :func:`file_check` performs, hoisted
    so :func:`signoff_attest._run_checks` stays one line per path-list
    field. ``normalized["presence"]`` lists presence-only paths;
    ``normalized["yaml"]`` maps a path to its ``select`` (``None`` = whole
    document)."""
    presence = normalized.get("presence", ())
    selects = normalized.get("yaml") or {}
    return [
        file_check(
            root,
            path,
            role,
            presence=path in presence,
            yaml=path in selects,
            select=selects.get(path),
        )
        for path in sorted(normalized[field])
    ]
