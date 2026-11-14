"""File-backed ``functional-verification`` evidence pins for :mod:`.signoff`:
the grader's own re-hash of the cited evidence file and the unverifiable-pin
note (issues #3063, #3747, #4893; extracted for the file-size ratchet)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._jsonnum import is_int
from ._provenance import sha256_file

#: ``citation.content_hash_basis`` when the live hash is the grader's own
#: re-hash of the cited evidence file (``functional-verification``).
CONTENT_HASH_BASIS_EVIDENCE_FILE = "evidence_file"


PIN_VERIFIED = "verified"
PIN_STALE = "stale"
PIN_UNVERIFIABLE = "unverifiable"


def content_hash_pin_state(
    expected_hash: str | None, live_hash: str | None
) -> str | None:
    """Compare a manifest-pinned ``content_hash`` to the resolved live one.

    ``None`` when the entry pins nothing; otherwise ``"verified"`` (equal),
    ``"unverifiable"`` (no live hash to compare) or ``"stale"`` (differs).
    Shared by the staleness gate and the issue #3063 disclosure on the
    ``yield`` refusals so the two cannot drift.
    """
    if expected_hash is None:
        return None
    if live_hash is None:
        return PIN_UNVERIFIABLE
    return PIN_VERIFIED if live_hash == expected_hash else PIN_STALE


def unverifiable_pin_note(check_kind: str, resolution: Mapping[str, Any]) -> str:
    """Why a pinned ``content_hash`` cannot be compared (issues #3747, #4893)."""
    if check_kind == "functional-verification":
        if resolution.get("command") is not None:
            return (
                "functional-verification envelopes carry no "
                "provenance.input.content_hash, and a command-backed citation's "
                "stdout is not a stable file the grader can re-hash; cite the "
                "envelope as a file to pin its bytes"
            )
        return (
            "functional-verification envelopes carry no "
            "provenance.input.content_hash, and the cited evidence file could "
            "not be re-hashed; the pin cannot be compared"
        )
    return (
        f"{check_kind} envelopes carry no "
        "provenance.input.content_hash; the pin cannot be compared"
    )


def rehash_evidence_file(
    check_kind: str,
    spec: Mapping[str, Any],
    file_label: str | None,
    actual_hash: str | None,
    input_verified: bool | None,
) -> tuple[str | None, str | None, bool | None]:
    """Issue #4893: a ``functional-verification`` envelope records no input
    hash, so a file-backed citation that the manifest pins is re-hashed and the
    pin binds the evidence file's own bytes. Hashed only when pinned, so an
    unpinned entry's output is unchanged. Returns ``(actual_hash, basis,
    input_verified)``."""
    if (
        actual_hash is None
        and check_kind == "functional-verification"
        and file_label is not None
        and spec.get("content_hash") is not None
    ):
        evidence_digest = sha256_file(file_label)
        if evidence_digest is not None:
            return (
                f"sha256:{evidence_digest}",
                CONTENT_HASH_BASIS_EVIDENCE_FILE,
                True,
            )
    return actual_hash, None, input_verified


#: ``detail.missing`` vocabulary for an ``unanchored_evidence`` binding
#: refusal (issue #5124).
MISSING_T1_ITEM = "t1_item"
MISSING_INPUT_PATH = "provenance.input.path"
MISSING_CONTENT_HASH = "manifest content_hash"


def generic_binding_check(
    item_id: int | None,
    envelope: Mapping[str, Any],
    spec: Mapping[str, Any],
    anchored: bool,
    binding_present: bool,
) -> tuple[str | None, list[str]]:
    """``(refusal, missing)`` for a generic citation bound to T1 ``item_id``
    (issues #2718, #5124). ``refusal`` is ``"wrong_item"`` for a declared
    ``t1_item`` naming another item, ``"unanchored_evidence"`` when a field
    is missing, else ``None``. ``missing`` names the absent fields: a
    ``t1_item`` absent (anchored item only) or not an integer,
    ``provenance.input.path`` unusable, and no manifest ``content_hash``."""
    declared = envelope.get("t1_item")
    if is_int(declared) and declared != item_id:
        return "wrong_item", []
    missing: list[str] = []
    if (declared is None and anchored) or (
        declared is not None and not is_int(declared)
    ):
        missing.append(MISSING_T1_ITEM)
    if anchored and not binding_present:
        missing.append(MISSING_INPUT_PATH)
    if anchored and not isinstance(spec.get("content_hash"), str):
        missing.append(MISSING_CONTENT_HASH)
    return ("unanchored_evidence" if missing else None), missing


def unanchored_detail(
    missing: list[str], spec: Mapping[str, Any], resolution: Mapping[str, Any]
) -> dict[str, Any]:
    """``detail`` for a binding refusal: ``{}`` when nothing is missing, else
    ``missing`` plus, when the pin is what is missing, the manifest entry
    shape to cite."""
    if not missing:
        return {}
    detail: dict[str, Any] = {"missing": list(missing)}
    live_hash = resolution["content_hash"]
    if MISSING_CONTENT_HASH in missing and live_hash is not None:
        detail["manifest_entry"] = {
            "file": spec.get("file"),
            "content_hash": live_hash,
        }
    return detail
