"""File-backed ``functional-verification`` ``content_hash`` pins (issue #4893)."""

from __future__ import annotations

import hashlib

from klayout_tools.signoff import build_tier_report
from test_signoff import FUNCTIONAL_VERIFICATION_SDF_ENVELOPE, _manifest, _write


def _fv_item7(tmp_path, pin, *, command=False):
    fv_path = _write(tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_SDF_ENVELOPE)
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


def test_file_backed_functional_verification_pin_matches_evidence_bytes(tmp_path):
    """The grader re-hashes the cited file; a matching pin grades on the normal
    verdict and the citation discloses the hash basis."""
    fv_path = _write(tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_SDF_ENVELOPE)
    with open(fv_path, "rb") as handle:
        pin = "sha256:" + hashlib.sha256(handle.read()).hexdigest()

    _, item_7 = _fv_item7(tmp_path, pin)

    assert item_7["status"] == "met"
    assert item_7["citation"]["content_hash"] == pin
    assert item_7["citation"]["content_hash_basis"] == "evidence_file"


def test_file_backed_functional_verification_pin_goes_stale_on_edit(tmp_path):
    """A pin that no longer matches the evidence bytes is stale."""
    _, item_7 = _fv_item7(tmp_path, "sha256:" + "0" * 64)

    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "stale_evidence"


def test_command_backed_functional_verification_pin_stays_unverifiable(tmp_path):
    """A command's stdout is not a stable file; the pin stays ungradable and
    the note says why."""
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
