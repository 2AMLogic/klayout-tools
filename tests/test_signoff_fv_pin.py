"""File-backed ``functional-verification`` ``content_hash`` pins (issues
#4893, #5550): the legacy evidence-file basis for an envelope from an older
``klt``, and the RTL-digest basis for a provenance-bearing one."""

from __future__ import annotations

import hashlib

from klayout_tools.signoff import build_tier_report
from test_signoff import (
    FUNCTIONAL_VERIFICATION_SDF_ENVELOPE,
    FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE,
    _manifest,
    _write,
)

#: The RTL digest the provenance-bearing fixture records -- what a manifest
#: `content_hash` pin binds for this kind since issue #5550.
RTL_DIGEST = FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE["provenance"]["input"][
    "content_hash"
]


def _fv_item7(
    tmp_path, pin, *, command=False, envelope=FUNCTIONAL_VERIFICATION_SDF_ENVELOPE
):
    fv_path = _write(tmp_path, "fv.json", envelope)
    spec = {"file": fv_path}
    if command:
        spec = {
            "command": [
                "python3",
                "-c",
                f"print(open({fv_path!r}).read())",
            ]
        }
    if pin is not None:
        spec["content_hash"] = pin
    result = build_tier_report(_manifest(kind="digital", evidence={"7": spec}))
    return fv_path, next(item for item in result["items"] if item["id"] == 7)


# --------------------------------------------------------------------------- #
# Legacy envelopes (no `provenance` key): the #4893 evidence-file basis
# --------------------------------------------------------------------------- #


def test_legacy_file_backed_pin_matches_evidence_bytes(tmp_path):
    """A report from an older `klt` is graded by the grader re-hashing the
    cited file; a matching pin grades on the normal verdict and the citation
    discloses the hash basis."""
    fv_path = _write(tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_SDF_ENVELOPE)
    with open(fv_path, "rb") as handle:
        pin = "sha256:" + hashlib.sha256(handle.read()).hexdigest()

    _, item_7 = _fv_item7(tmp_path, pin)

    assert item_7["status"] == "met"
    assert item_7["citation"]["content_hash"] == pin
    assert item_7["citation"]["content_hash_basis"] == "evidence_file"


def test_legacy_file_backed_pin_goes_stale_on_edit(tmp_path):
    """A pin that no longer matches the evidence bytes is stale."""
    _, item_7 = _fv_item7(tmp_path, "sha256:" + "0" * 64)

    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "stale_evidence"


def test_legacy_command_backed_pin_stays_unverifiable(tmp_path):
    """A legacy command citation's stdout is not a stable file; the pin stays
    ungradable and the note says why."""
    _, item_7 = _fv_item7(tmp_path, "sha256:expected", command=True)

    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "unverifiable_provenance"
    assert item_7["citation"] is None
    assert item_7["detail"]["content_hash_pin"] == "unverifiable"
    note = item_7["detail"]["content_hash_pin_note"]
    assert "functional-verification" in note
    assert "provenance.input.content_hash" in note
    assert "command-backed" in note


def test_unpinned_functional_verification_citation_has_no_basis_key(tmp_path):
    """No pin -> no new keys."""
    _, item_7 = _fv_item7(tmp_path, None)

    assert "content_hash_basis" not in (item_7.get("citation") or {})


# --------------------------------------------------------------------------- #
# Provenance-bearing envelopes (issue #5550): the pin binds the RTL digest
# --------------------------------------------------------------------------- #


def test_rtl_digest_pin_grades_met_without_evidence_file_basis(tmp_path):
    _, item_7 = _fv_item7(
        tmp_path, RTL_DIGEST, envelope=FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE
    )

    assert item_7["status"] == "met"
    assert item_7["citation"]["content_hash"] == RTL_DIGEST
    # Not the report's own bytes: the #4893 basis key is absent, so a reader
    # can tell this pin binds a design input.
    assert "content_hash_basis" not in item_7["citation"]


def test_rtl_digest_pin_goes_stale_when_digest_differs(tmp_path):
    """A pin that no longer matches the recorded RTL digest -- the manifest
    was written against a different RTL revision -- is stale evidence."""
    _, item_7 = _fv_item7(
        tmp_path,
        "sha256:rtl-b",
        envelope=FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE,
    )

    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "stale_evidence"


def test_command_backed_rtl_digest_pin_is_gradable(tmp_path):
    """A command-backed citation no longer grades `unverifiable_provenance`
    when the envelope carries the digest: the parsed stdout *is* the envelope,
    so the pin compares against it like a file-backed one."""
    _, item_7 = _fv_item7(
        tmp_path,
        RTL_DIGEST,
        command=True,
        envelope=FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE,
    )

    assert item_7["status"] == "met"
    assert item_7["citation"]["content_hash"] == RTL_DIGEST


def test_legacy_byte_pin_flips_to_stale_against_provenance_bearing_envelope(
    tmp_path,
):
    """The compat flip (issue #5550): a manifest that pinned the report's own
    bytes under #4893 -- and graded `verified` through the evidence-file
    re-hash -- now grades `stale_evidence` against an unchanged committed
    report, because the pin is compared against the RTL digest instead."""
    fv_path = _write(
        tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE
    )
    with open(fv_path, "rb") as handle:
        byte_pin = "sha256:" + hashlib.sha256(handle.read()).hexdigest()

    _, item_7 = _fv_item7(
        tmp_path, byte_pin, envelope=FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE
    )

    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "stale_evidence"


def test_null_rtl_digest_pin_is_unverifiable_with_null_note(tmp_path):
    """A source that could not be hashed records `content_hash: null`; a pin
    against that envelope is unverifiable with a note naming the null digest
    -- not the legacy wording, and never the evidence-file basis."""
    envelope = {
        **FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE,
        "provenance": {
            **FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE["provenance"],
            "input": {"content_hash": None, "role": "source"},
        },
    }
    _, item_7 = _fv_item7(tmp_path, "sha256:expected", envelope=envelope)

    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "unverifiable_provenance"
    note = item_7["detail"]["content_hash_pin_note"]
    assert "null" in note
    assert "could not be hashed" in note
    assert "legacy" not in note
