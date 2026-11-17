"""Issue #5837: a stale ``content_hash`` pin on a path-less
``provenance.input`` -- the ``{content_hash, role}`` shape ``klt drc`` emits
-- still names the remedy: the generic hint, and
``pin_is_envelope_file_hash`` when the pin is the cited envelope file's own
sha256 (hashing the record, not what the record is about)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from klayout_tools.signoff import build_tier_report
from test_signoff import DRC_CLEAN_ENVELOPE, _manifest, _write


def _grade(tmp_path, pin: str | None = None, own_hash: bool = False):
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    if own_hash:
        pin = "sha256:" + hashlib.sha256(Path(drc_path).read_bytes()).hexdigest()
    result = build_tier_report(
        _manifest(evidence={"3": {"file": drc_path, "content_hash": pin}})
    )
    return next(i for i in result["items"] if i["id"] == 3)


def test_stale_drc_pin_states_the_input_hash_rule(tmp_path):
    item = _grade(tmp_path, pin="sha256:stale-revision")
    assert item["reason"] == "stale_evidence"
    detail = item["detail"]
    assert "compared_against" not in detail
    assert detail["hash_check"]["field"] == "provenance.input.content_hash"
    assert detail["hash_check"]["expected"] == "sha256:stale-revision"
    assert detail["hash_check"]["actual"] == "sha256:layoutA"
    assert detail["hash_check"]["match"] is False
    assert "pin_is_envelope_file_hash" not in detail
    assert detail["hint"] == (
        "content_hash must be the envelope's provenance.input.content_hash "
        "(the recorded input hash), not the hash of the evidence envelope file"
    )


def test_drc_pin_of_envelope_own_hash_is_flagged(tmp_path):
    item = _grade(tmp_path, own_hash=True)
    assert item["reason"] == "stale_evidence"
    detail = item["detail"]
    assert detail["pin_is_envelope_file_hash"] is True
    assert "itself" in detail["hint"]
    assert "provenance.input.content_hash" in detail["hint"]
    assert "compared_against" not in detail
    # expected is the pin (the envelope file's own sha256, via own_hash=True);
    # actual is the recorded input hash -- they differ, that's the point.
    assert detail["hash_check"]["actual"] == "sha256:layoutA"
    assert detail["hash_check"]["match"] is False
    own = (
        "sha256:" + hashlib.sha256(Path(tmp_path / "drc.json").read_bytes()).hexdigest()
    )
    assert detail["hash_check"]["expected"] == own
