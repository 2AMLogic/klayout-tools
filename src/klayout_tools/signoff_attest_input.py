"""Item-1 input-set binding for ``klt signoff --attest`` (#4764).

Split out of :mod:`klayout_tools.signoff_attest` to keep that module under its
file-size ceiling."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from ._provenance import sha256_file
from .signoff import input_set_digest


def normalized_description(source: Mapping[str, Any]) -> str | None:
    """The inventory's ``description`` as the envelope carries it (issue
    #5573): the string verbatim, or ``None`` when ``source`` declares no
    ``description``, a non-string, or one that is empty or whitespace-only
    -- the one rule both producers apply, so they cannot disagree about
    what "no disclosure" means. Never truncated or rewritten."""
    value = source.get("description")
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def attestation_block(
    producer: str,
    hash_algorithm: str,
    checks: Sequence[Mapping[str, Any]],
    passed_count: int,
    *,
    inventory: Mapping[str, Any] | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """The ``attestation`` block every ``klt signoff --attest`` envelope
    carries (issue #5573; assembled here so neither producing module grows
    for a shape they share).

    Key order is contract: an envelope whose inventory declared no
    ``description`` must render byte-identically to pre-#5573 output, so
    the shared keys keep their historical order and the two optional keys
    append at the end. ``inventory`` (the inventory-based producer) records
    ``inventory_schema_version`` and, through :func:`normalized_description`,
    the carried ``description``; ``description`` (the stream producer, whose
    optional inventory is parsed under its own guard) is the pre-normalized
    disclosure. Both normalize through the one rule, so the two producers
    cannot disagree about what "no disclosure" means."""
    block: dict[str, Any] = {"producer": producer}
    if inventory is not None:
        block["inventory_schema_version"] = inventory["schema_version"]
    block["hash_algorithm"] = hash_algorithm
    block["check_count"] = len(checks)
    block["passed_count"] = passed_count
    block["checks"] = checks
    if description is None and inventory is not None:
        description = normalized_description(inventory)
    if description is not None:
        block["description"] = description
    return block


def input_binding(
    item: int,
    root: str,
    normalized: Mapping[str, Any],
    inventory_rel: str,
    inventory_hash: str,
) -> tuple[dict[str, Any], str]:
    """The envelope's ``provenance.input`` and the digest a manifest pins.

    Item 1 binds the whole input set (every source, every derived file and the
    inventory itself) through ``provenance.input.paths`` so editing any member
    makes the manifest row ``stale_evidence``. A member that cannot be read is
    left out: its ``present`` check already fails the attestation. Other items
    bind the inventory alone."""
    if item != 1:
        return (
            {
                "path": {"path": inventory_rel, "scope": "repo"},
                "content_hash": inventory_hash,
            },
            inventory_hash,
        )
    member_paths = {
        *normalized["sources"],
        *(entry["path"] for entry in normalized["derived"]),
        inventory_rel,
    }
    members: list[tuple[str, str]] = []
    for path in sorted(member_paths, key=lambda value: value.encode("utf-8")):
        digest = sha256_file(os.path.join(root, path))
        if digest is not None:
            members.append((path, f"sha256:{digest}"))
    combined = input_set_digest(members)
    return (
        {
            "paths": [
                {"path": {"path": path, "scope": "repo"}, "content_hash": digest}
                for path, digest in members
            ],
            "content_hash": combined,
        },
        combined,
    )


def input_line(result: Mapping[str, Any], stream_item: int) -> str:
    """The text summary of an attestation's bound input, plus -- for a live
    result carrying one (:func:`with_entry`) -- the manifest entry to cite.
    A carried ``attestation.description`` rides along as its own line
    (issue #5573)."""
    binding = result["provenance"]["input"]
    digest = f"({binding['content_hash']})"
    if "paths" in binding:
        # Item 1 binds its whole input set (#4764): name the set, not one file.
        line = f"input set: {len(binding['paths'])} files {digest}"
    else:
        label = "stream" if result["t1_item"] == stream_item else "inventory"
        line = f"{label}: {binding['path']['path']} {digest}"
    attestation = result.get("attestation")
    disclosure = (
        normalized_description(attestation)
        if isinstance(attestation, Mapping)
        else None
    )
    if disclosure is not None:
        # The claimant's scope disclosure (issue #5573), surfaced so a human
        # reader sees it without opening the JSON.
        line += f"\ndescription: {disclosure}"
    entry = result.get("manifest_entry")
    if entry:
        file = entry["file"] or "<envelope path>"
        line += (
            f"\nmanifest entry: {{file: {file}, content_hash: {entry['content_hash']}}}"
        )
    return line


def with_entry(result: dict[str, Any], output: str | None) -> dict[str, Any]:
    """``result`` plus the ``manifest_entry`` to cite for the envelope at
    ``output`` (issue #5124). Call after the envelope is written, so its
    committed bytes are unchanged; live results only."""
    binding = result["provenance"]["input"]
    result["manifest_entry"] = {
        "file": output,
        "content_hash": binding["content_hash"],
    }
    return result
