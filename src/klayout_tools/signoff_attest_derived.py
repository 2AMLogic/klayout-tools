"""Item-1 ``derived[]`` entries for ``klt signoff --attest`` (issues #2844, #5945).

Split out of :mod:`klayout_tools.signoff_attest` for the per-file size ratchet.
Validation of a derived artifact's declared freshness (a ``check`` command or a
``recorded`` hash set) and the ``recorded_hash`` check it yields. It must not
import :mod:`signoff_attest` (which imports it) at module level, so the path
normaliser is passed in and ``AttestationError`` is imported lazily.

In **record format 2** (:mod:`klayout_tools.signoff_records`) the file hashes
live in the records file, not the inventory, so a ``recorded`` entry has no
digests to compare against and its check would degenerate to presence-only:
re-running ``--attest --records`` after a source edit would silently renew a
stale artifact's record line. ``recorded`` is therefore **refused** with
``--records``; a derived artifact must declare the ``check`` command form,
which re-verifies freshness on every attestation exactly as in the classic
format (migrating a classic ``recorded`` inventory is issue #5947).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from typing import Any

from ._provenance import sha256_file

_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")

_NormalizePath = Callable[[Any, str, str], str]


def _error(message: str) -> Exception:
    from klayout_tools.signoff_attest import AttestationError

    return AttestationError(message)


def _hex_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _HEX_DIGEST.fullmatch(value):
        raise _error(f"'{field}' must be a 64-character lowercase hex sha256 digest")
    return value


def _validated_recorded(
    raw: Any, field: str, root: str, sources: set[str], normalize: _NormalizePath
) -> dict[str, Any]:
    """Validate a ``derived[]`` entry's ``recorded`` freshness record: the
    derived file's digest plus the digest of every source it was built from."""
    if not isinstance(raw, Mapping):
        raise _error(f"'{field}' must be an object")
    unknown = sorted(set(raw) - {"derived_sha256", "sources"})
    if unknown:
        raise _error(f"'{field}' has unknown field(s): {', '.join(unknown)}")
    derived_sha256 = _hex_digest(raw.get("derived_sha256"), f"{field}.derived_sha256")
    recorded_sources = raw.get("sources")
    if not isinstance(recorded_sources, Mapping) or not recorded_sources:
        raise _error(
            f"'{field}.sources' must be a non-empty object mapping each source "
            "path to its sha256 -- a hash of the artifact alone proves nothing "
            "about the sources it was built from"
        )
    normalized: dict[str, str] = {}
    for raw_path, digest in recorded_sources.items():
        path = normalize(raw_path, f"{field}.sources", root)
        if path not in sources:
            raise _error(
                f"'{field}.sources' lists '{path}', which is not in the "
                "inventory's top-level 'sources'"
            )
        if path in normalized:
            raise _error(f"'{field}.sources' lists '{path}' more than once")
        normalized[path] = _hex_digest(digest, f"{field}.sources[{path}]")
    return {"derived_sha256": derived_sha256, "sources": normalized}


def validated_derived(
    inventory: Mapping[str, Any],
    root: str,
    seen: dict[str, str],
    sources: set[str],
    normalize: _NormalizePath,
    command_spec: Callable[..., dict[str, Any]],
    records: bool = False,
) -> list[dict[str, Any]]:
    derived = inventory.get("derived")
    if not isinstance(derived, list) or not derived:
        raise _error(
            "item 1 inventory must declare a non-empty 'derived' list -- the "
            "netlist(s) generated from the sources, each with a freshness check"
        )
    entries = []
    for index, raw in enumerate(derived):
        field = f"derived[{index}]"
        if not isinstance(raw, Mapping):
            raise _error(f"'{field}' must be an object")
        unknown = sorted(set(raw) - {"path", "generator", "check", "recorded"})
        if unknown:
            raise _error(f"'{field}' has unknown field(s): {', '.join(unknown)}")
        path = normalize(raw.get("path"), f"{field}.path", root)
        if path in seen:
            raise _error(
                f"path '{path}' is listed more than once (in '{seen[path]}' and "
                f"'{field}') -- each inventoried file must appear exactly once"
            )
        seen[path] = field
        generator = raw.get("generator")
        if not isinstance(generator, str) or not generator:
            raise _error(
                f"'{field}.generator' must name what produced the artifact "
                '(e.g. "klt netlist")'
            )
        if ("check" in raw) == ("recorded" in raw):
            raise _error(
                f"'{field}' must declare exactly one of 'check' (a command that "
                "fails when the artifact is stale, e.g. klt netlist ... --check) "
                "or 'recorded' (the derived file's and its sources' sha256 "
                "digests) -- presence alone does not prove a derived artifact "
                "is current"
            )
        entry: dict[str, Any] = {"path": path, "generator": generator}
        if "check" in raw:
            entry["check"] = command_spec(raw["check"], f"{field}.check", root)
        elif records:
            raise _error(
                f"'{field}.recorded' is not accepted with --records: without "
                "the declared source hashes there is no freshness gate, and "
                "re-running --attest would silently renew a stale artifact's "
                "record line. Declare 'check' instead -- a command that fails "
                "when the artifact is stale (e.g. klt netlist ... --check); "
                "migrating a classic 'recorded' inventory is issue #5947"
            )
        else:
            entry["recorded"] = _validated_recorded(
                raw["recorded"], f"{field}.recorded", root, sources, normalize
            )
        entries.append(entry)
    return sorted(entries, key=lambda entry: entry["path"])


def recorded_hash_check(root: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    """Compare the derived file and each recorded source against the digests
    the inventory recorded; no command is run. ``mismatches`` names every
    differing path with its expected and actual digest (``None`` = missing).

    Classic format only: under ``--records`` a ``recorded`` entry is refused
    at validation (:func:`validated_derived`), so the freshness gate is the
    entry's ``check`` command, as in the classic ``check`` form."""
    recorded = entry["recorded"]
    expected = {entry["path"]: recorded["derived_sha256"], **recorded["sources"]}
    check: dict[str, Any] = {
        "check": "recorded_hash",
        "role": "freshness",
        "target": entry["path"],
        "generator": entry["generator"],
    }
    mismatches = []
    for path in sorted(expected):
        actual = sha256_file(os.path.join(root, path))
        if actual != expected[path]:
            mismatches.append(
                {"path": path, "expected": expected[path], "actual": actual}
            )
    check["passed"] = not mismatches
    if mismatches:
        check["mismatches"] = mismatches
    return check


def derived_freshness_check(
    root: str,
    entry: Mapping[str, Any],
    command: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """The freshness check for one item-1 ``derived`` entry: the recorded
    hash comparison, or the entry's ``check`` command."""
    if "recorded" in entry:
        return recorded_hash_check(root, entry)
    return command(
        entry["check"],
        "freshness",
        target=entry["path"],
        generator=entry["generator"],
    )
