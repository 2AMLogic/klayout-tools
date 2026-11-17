"""Disclosure blocks for artifact-anchored generic citations (issues #2718, #2844,
#4311, #5945).

Split out of :mod:`klayout_tools.signoff` for the per-file size ratchet; pure
functions over a parsed envelope, so this module imports nothing from it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: T1 item 8 (Characterization report): the one item that accepts a bare
#: generic envelope, so a met citation always discloses whether
#: ``klt signoff --attest 8`` produced it (issue #4311).
_CHARACTERIZATION_ITEM = 8


def _attach_characterization_attestation(
    check_kind: str,
    item_id: int | None,
    envelope: Mapping[str, Any],
    citation: dict[str, Any],
) -> None:
    """Add ``citation["attestation"]`` for a met item-8 generic citation; a
    no-op for every other kind/item."""
    if check_kind == "generic" and item_id == _CHARACTERIZATION_ITEM:
        citation["attestation"] = _characterization_attestation_disclosure(envelope)


def _characterization_attestation_disclosure(
    envelope: Mapping[str, Any],
) -> dict[str, Any]:
    """The ``citation.attestation`` block for a met item-8 generic citation:
    ``tool_produced`` is ``True`` (with the producer and check counts) only
    when the envelope carries a ``klt signoff --attest`` attestation block;
    ``False`` for a hand-written one, so the absence is visible, not implied.
    Disclosure only -- never graded."""
    attestation = envelope.get("attestation")
    if isinstance(attestation, Mapping) and isinstance(
        attestation.get("producer"), str
    ):
        return {
            "tool_produced": True,
            "producer": attestation["producer"],
            "check_count": attestation.get("check_count"),
            "passed_count": attestation.get("passed_count"),
        }
    return {"tool_produced": False}


def _generic_artifact_binding(
    envelope: Mapping[str, Any], resolution: Mapping[str, Any]
) -> dict[str, Any]:
    """The ``citation.artifact_binding`` disclosure for a ``"met"``
    artifact-anchored generic citation (issue #2718): which item the
    attestation declared, which artifact it is bound to (``path`` exactly as
    the envelope names it -- a string or a ``{path, scope}`` object), and the
    hash that artifact re-hashed to. ``summary``/``source`` are echoed when
    the envelope carries them, so a reader sees what was audited without
    opening the envelope; they are never graded. A graded, matching
    regeneration claim (items 1/2, issue #2887) is added by
    :func:`_bind_anchored_citation`, not here.
    """
    provenance = envelope.get("provenance") or {}
    input_block = provenance.get("input") or {}
    binding: dict[str, Any] = {
        "t1_item": envelope.get("t1_item"),
        "path": input_block.get("path"),
        "content_hash": resolution["content_hash"],
        "input_verified": resolution.get("input_verified"),
    }
    if resolution.get("input_members"):
        binding["members"] = resolution["input_members"]
    for field in ("summary", "source"):
        value = envelope.get(field)
        if isinstance(value, str):
            binding[field] = value
    # Issue #2844: an envelope produced by `klt signoff --attest` names the
    # producer and how many item-specific checks backed its status, so a
    # reader (or a fleet roll-up) can tell a derived pass from a hand-written
    # one. Disclosure only -- never graded, and absent for any envelope that
    # carries no `attestation` block.
    attestation = envelope.get("attestation")
    if isinstance(attestation, Mapping) and isinstance(
        attestation.get("producer"), str
    ):
        binding["attestation"] = {
            "producer": attestation["producer"],
            "inventory_schema_version": attestation.get("inventory_schema_version"),
            "check_count": attestation.get("check_count"),
            "passed_count": attestation.get("passed_count"),
        }
        if "record_format" in attestation:
            # Issue #5945: counts are not committed in record format 2 (null).
            binding["attestation"]["record_format"] = attestation["record_format"]
    return binding
