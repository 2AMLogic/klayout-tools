"""The ``citation.artifact_binding`` attestation record (issue #2844), out of
``signoff.py`` for its size-ratchet ceiling like the other ``signoff_attest_*``
siblings. Issue #5571: item 2's stream producer records the declared-checks
inventory's schema version nested under ``attestation.inventory`` while items
1/8/9/10 write the flat ``attestation.inventory_schema_version`` key, so the
disclosure resolves flat-if-present, else nested: an already-committed
envelope in either shape discloses the same fact with no re-attest. A
malformed nested block yields ``null`` rather than raising. Disclosure only
-- never graded."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def attestation_record(attestation: Mapping[str, Any]) -> dict[str, Any]:
    """The ``binding["attestation"]`` block: which producer attested, which
    inventory schema version it declared, and how many checks backed the
    status. A claimant's scope disclosure is copied verbatim when present."""
    record = {
        "producer": attestation["producer"],
        "inventory_schema_version": inventory_schema_version(attestation),
        "check_count": attestation.get("check_count"),
        "passed_count": attestation.get("passed_count"),
    }
    description = scope_description(attestation)
    if description is not None:
        record["description"] = description
    return record


def scope_description(attestation: Mapping[str, Any]) -> str | None:
    """The claimant's ``attestation.description`` when it is a non-empty
    (after trimming) string, else ``None``. The one rule every echo site
    (the artifact-binding record and item 8's disclosure block) applies."""
    description = attestation.get("description")
    if isinstance(description, str) and description.strip():
        return description
    return None


def inventory_schema_version(attestation: Mapping[str, Any]) -> Any:
    """The flat ``inventory_schema_version`` key wins; else item 2's nested
    ``inventory.schema_version``; ``None`` when neither records a version."""
    flat = attestation.get("inventory_schema_version")
    if flat is not None:
        return flat
    inventory = attestation.get("inventory")
    if isinstance(inventory, Mapping):
        return inventory.get("schema_version")
    return None
