"""Issue #5392: a stale ``content_hash`` pin on a ``klt yield`` citation names
the samples document that was hashed (a yield report has no
``provenance.input``)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from klayout_tools.signoff import build_tier_report
from test_signoff import YIELD_PASS_ENVELOPE, _manifest, _write


def _grade(tmp_path, samples: str, pin: str | None = None, own_hash: bool = False):
    envelope = {**YIELD_PASS_ENVELOPE, "samples": samples}
    yield_path = _write(tmp_path, "yield.json", envelope)
    if own_hash:
        pin = "sha256:" + hashlib.sha256(Path(yield_path).read_bytes()).hexdigest()
    result = build_tier_report(
        _manifest(evidence={"6": {"file": yield_path, "content_hash": pin}})
    )
    return next(i for i in result["items"] if i["id"] == 6)


def _samples(tmp_path) -> Path:
    path = tmp_path / "mc-samples.json"
    path.write_text(json.dumps({"measurements": []}))
    return path


def test_stale_yield_pin_names_samples_document(tmp_path):
    samples = _samples(tmp_path)
    item = _grade(tmp_path, str(samples), pin="sha256:stale-revision")
    assert item["reason"] == "stale_evidence"
    detail = item["detail"]
    assert detail["compared_against"] == str(samples)
    assert detail["hash_check"]["field"].startswith("samples")
    assert detail["hash_check"]["match"] is False
    assert "samples document" in detail["hint"]
    assert "pin_is_envelope_file_hash" not in detail


def test_yield_pin_of_report_own_hash_is_flagged(tmp_path):
    samples = _samples(tmp_path)
    detail = _grade(tmp_path, str(samples), own_hash=True)["detail"]
    assert detail["pin_is_envelope_file_hash"] is True
    assert detail["compared_against"] == str(samples)
    assert "itself" in detail["hint"]


def test_unresolved_samples_has_no_compared_against(tmp_path):
    item = _grade(tmp_path, str(tmp_path / "missing.json"), pin="sha256:abc")
    assert "compared_against" not in (item.get("detail") or {})
