"""Item-1 input-set binding for ``klt signoff --attest`` (#4764).

Split out of :mod:`klayout_tools.signoff_attest` to keep that module under its
file-size ceiling."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from ._provenance import sha256_file
from .signoff import input_set_digest


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
    """The one-line text summary of an attestation's bound input."""
    binding = result["provenance"]["input"]
    digest = f"({binding['content_hash']})"
    if "paths" in binding:
        # Item 1 binds its whole input set (#4764): name the set, not one file.
        return f"input set: {len(binding['paths'])} files {digest}"
    label = "stream" if result["t1_item"] == stream_item else "inventory"
    return f"{label}: {binding['path']['path']} {digest}"
