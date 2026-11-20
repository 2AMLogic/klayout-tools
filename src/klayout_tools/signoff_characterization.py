"""T1 item 8's ``citation.attestation`` disclosure block (issues #4311, #6111),
out of ``signoff.py`` for its size-ratchet ceiling like the other
``signoff_*`` siblings. Item 8 accepts a bare generic envelope, so a met
citation always discloses whether ``klt signoff --attest 8`` produced it.
Disclosure only -- never graded."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .signoff_attest_binding import scope_description

#: T1 item 8 (Characterization report): the one item that accepts a bare
#: generic envelope, so a met citation always discloses whether
#: ``klt signoff --attest 8`` produced it (issue #4311).
CHARACTERIZATION_ITEM = 8


def attach_characterization_attestation(
    check_kind: str,
    item_id: int | None,
    envelope: Mapping[str, Any],
    citation: dict[str, Any],
) -> None:
    """Add ``citation["attestation"]`` for a met item-8 generic citation; a
    no-op for every other kind/item."""
    if check_kind == "generic" and item_id == CHARACTERIZATION_ITEM:
        citation["attestation"] = characterization_attestation_disclosure(envelope)


def characterization_attestation_disclosure(
    envelope: Mapping[str, Any],
) -> dict[str, Any]:
    """The ``citation.attestation`` block for a met item-8 generic citation:
    ``tool_produced`` is ``True`` (with the producer and check counts) only
    when the envelope carries a ``klt signoff --attest`` attestation block;
    ``False`` for a hand-written one, so the absence is visible, not implied.
    A tool-produced envelope's claimant scope ``description`` (issue #6111) is
    echoed verbatim when non-empty and omitted otherwise, under the same rule
    as ``artifact_binding.attestation.description`` for items 1/2/9/10.
    Disclosure only -- never graded."""
    attestation = envelope.get("attestation")
    if isinstance(attestation, Mapping) and isinstance(
        attestation.get("producer"), str
    ):
        disclosure: dict[str, Any] = {
            "tool_produced": True,
            "producer": attestation["producer"],
            "check_count": attestation.get("check_count"),
            "passed_count": attestation.get("passed_count"),
        }
        description = scope_description(attestation)
        if description is not None:
            disclosure["description"] = description
        return disclosure
    return {"tool_produced": False}
