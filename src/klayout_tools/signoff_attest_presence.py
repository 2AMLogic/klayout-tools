"""Presence-only inventory entries (issue #5615).

Split out of :mod:`klayout_tools.signoff_attest` (file-size ratchet). A
path-list entry may be a mapping ``{"path": "...", "attest": "presence"}``
alongside the plain-string form. ``"attest": "content"`` is the explicit
default.

A ``present`` check's verdict is already presence-only (``passed`` is "the file
exists"); content immutability is enforced by the *consumers* of the stamped
``content_hash`` -- the ``--check`` envelope diff and the ``--manifest``
grading re-hash. Both are key-driven, so a presence-only entry simply omits
the key (never ``null``: that is the missing-file marker) and carries
``"attest": "presence"`` so the semantics are part of the envelope.

Only item 10's ``hygiene`` list accepts the option. Item 1's ``sources`` are
re-hashed by ``input_binding()`` regardless of the check dict, so the option
would silently not work there; an item 9 bench *is* the attested file.

The reduction mirrors :func:`signoff_attest_item9.split_testbenches` and is
generic (driven by the item's path lists), so a later entry key adds to
:data:`ENTRY_KEYS` instead of bolting on a second parallel entry form.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ._provenance import sha256_file

#: The mapping-entry keys a path-list entry accepts.
ENTRY_KEYS = frozenset({"path", "attest"})

#: Accepted ``attest`` values; ``content`` is the default.
ATTEST_VALUES = ("content", "presence")

#: ``{item: path-list fields that accept "attest": "presence"}``.
PRESENCE_FIELDS: dict[int, frozenset[str]] = {10: frozenset({"hygiene"})}


def _error(message: str) -> Exception:
    """An ``AttestationError`` (imported lazily: ``signoff_attest`` imports us)."""
    from klayout_tools.signoff_attest import AttestationError

    return AttestationError(message)


def split_entries(
    inventory: Mapping[str, Any],
    item: int,
    path_lists: Sequence[tuple[str, str]],
    root: str,
    normalize_path: Callable[[Any, str, str], str],
) -> tuple[dict[str, Any], frozenset[str]]:
    """Reduce mapping-form path-list entries to plain paths.

    Returns ``(inventory_with_plain_paths, presence_only_paths)``. String
    entries pass through untouched, so an old-form inventory is returned as an
    equal mapping and validates exactly as before. A malformed container is
    left for the generic path-list validator to refuse."""
    out = dict(inventory)
    presence: set[str] = set()
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
            if mode not in ATTEST_VALUES:
                raise _error(
                    f"'{where}.attest' must be one of "
                    f"{', '.join(repr(v) for v in ATTEST_VALUES)}, got {mode!r}"
                )
            if mode == "presence":
                if field not in PRESENCE_FIELDS.get(item, frozenset()):
                    raise _error(
                        f"'{where}.attest' = 'presence' is only accepted on an "
                        "item 10 'hygiene' entry -- elsewhere the file's bytes "
                        "are the attested property"
                    )
                presence.add(normalize_path(entry["path"], field, root))
            paths.append(entry["path"])
        out[field] = paths
    return out, frozenset(presence)


def file_check(
    root: str, path: str, role: str, *, presence: bool = False
) -> dict[str, Any]:
    """A ``present`` check for ``path``. A ``presence`` entry omits
    ``content_hash`` (never ``null``) and records ``"attest": "presence"``."""
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
