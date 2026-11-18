"""Item 2's ``inputs``: the generator's input files, hash-pinned (issue #5724).

An item-2 inventory may declare ``inputs`` -- repo-relative files the layout
generator consumed (script, cell library, sub-cell streams, configuration).
Each becomes a ``role: "input"`` ``present`` check carrying its sha256, and is
folded -- role-tagged -- into the ``provenance.input.paths`` set the grader
re-hashes, so editing an input grades the citing manifest row stale while the
stream stays byte-identical.

This lives beside :mod:`.signoff_attest_stream` rather than in it so the stream
module stays small; ``signoff_attest.py`` is size-ratcheted and cannot grow.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .signoff import input_set_digest
from .signoff_attest import (
    AttestationError,
    _file_check,
    _path_list,
    _validated_command_checks,
)

#: ``role`` of a stream member of ``provenance.input.paths`` / of an input's check.
STREAM_ROLE = "stream"
INPUT_ROLE = "input"


def validated_inputs(
    inventory: Mapping[str, Any], root: str, stream_paths: Sequence[str]
) -> list[str]:
    """The inventory's ``inputs`` as normalised repo-relative paths, in
    declaration order; ``[]`` when none are declared. Refuses an empty list, a
    path listed twice, and a path that is also an ``--input`` stream (a stream
    is the attested artifact, not an input to it)."""
    if "inputs" not in inventory:
        return []
    seen = {path: "--input" for path in stream_paths}
    return _path_list(inventory, "inputs", root, seen)


def validated_checks(
    inventory: Mapping[str, Any], root: str, inputs: Sequence[str]
) -> list[dict[str, Any]]:
    """The declared command specs. ``checks`` is optional once ``inputs`` are
    declared (a hash pin needs no command), but an inventory declaring neither
    attests nothing and is refused."""
    if inputs and "checks" not in inventory:
        return []
    if not inputs and "checks" not in inventory:
        raise AttestationError(
            "item 2 inventory must declare 'checks' (commands whose result this "
            "attestation reports) and/or 'inputs' (files the generator consumed)"
        )
    return _validated_command_checks(inventory, root, 2)


def input_checks(root: str, inputs: Sequence[str]) -> list[dict[str, Any]]:
    """One hash-pinned ``role: "input"`` ``present`` check per declared input,
    ordered by path (independent of declaration order)."""
    ordered = sorted(inputs, key=lambda path: path.encode("utf-8"))
    return [_file_check(root, path, INPUT_ROLE) for path in ordered]


def bound_input(
    streams: Sequence[tuple[str, str]], checks: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, Any], str]:
    """``(provenance.input, set digest)`` for a stream attestation that declares
    ``inputs``: one role-tagged member per stream (``streams`` is ``(repo path,
    sha256)``) and per input (from ``checks``), sorted by path. An input that
    could not be read has no hash to bind; its failed check is what reports it."""
    members = [(path, digest, STREAM_ROLE) for path, digest in streams]
    members += [
        (check["path"], check["content_hash"], INPUT_ROLE)
        for check in checks
        if check["content_hash"] is not None
    ]
    members.sort(key=lambda member: member[0].encode("utf-8"))
    digest = input_set_digest((path, h) for path, h, _role in members)
    paths = [
        {"path": {"path": path, "scope": "repo"}, "content_hash": h, "role": role}
        for path, h, role in members
    ]
    return {"paths": paths, "content_hash": digest}, digest
