"""Cross-build replay of inventory descriptions first echoed by #5573."""

from copy import deepcopy

import pytest

from klayout_tools.signoff_attest_drift import drift_result
from klayout_tools.signoff_attest_stream import build_stream_attestation
from test_signoff_attest_description import _attest, _repo, _stream, _stream_inventory


@pytest.fixture(params=[1, 2, 9, 10], ids=["set", "stream", "reproduction", "audit"])
def envelopes(tmp_path, request):
    repo = _repo(tmp_path)
    if request.param == 2:
        inv = _stream_inventory(repo, "Declared scope")
        fresh = build_stream_attestation(_stream(repo), inventory_path=inv)
    else:
        fresh = _attest(repo, request.param, "Declared scope")
    fresh["provenance"]["klt_version"] = "0.9.3"
    committed = deepcopy(fresh)
    committed["provenance"]["klt_version"] = "0.8.0"
    del committed["attestation"]["description"]
    return committed, fresh


def test_legacy_missing_description_matches_pinned_inventory(envelopes):
    committed, fresh = envelopes
    result = drift_result("evidence.json", committed, fresh)
    assert result["status"] == "match"
    assert result["drift"] == []
    assert result["fresh"]["attestation"]["description"] == "Declared scope"
    assert "description" not in committed["attestation"]


def test_legacy_shape_does_not_hide_a_changed_check(envelopes):
    committed, fresh = envelopes
    committed["attestation"]["checks"][0]["passed"] = False
    result = drift_result("evidence.json", committed, fresh)
    assert result["status"] == "drifted"
    fields = {entry["field"] for entry in result["drift"]}
    assert any(name.startswith("attestation.checks") for name in fields)
    assert "attestation.description" not in fields


@pytest.mark.parametrize(
    "change",
    [
        "missing-pin",
        "different-pin",
        "same-build",
        "unknown-build",
        "edited-description",
    ],
)
def test_unverified_omission_or_edited_scope_remains_drift(envelopes, change):
    committed, fresh = envelopes
    if change in {"missing-pin", "different-pin"}:
        # A stream's pin can agree even when its inventory pin does not.
        binding = (
            committed["attestation"]["inventory"]
            if fresh["t1_item"] == 2
            else committed["provenance"]["input"]
        )
        if change == "missing-pin":
            del binding["content_hash"]
        else:
            binding["content_hash"] = "sha256:" + "0" * 64
    elif change == "same-build":
        committed["provenance"]["klt_version"] = fresh["provenance"]["klt_version"]
    elif change == "unknown-build":
        committed["provenance"]["klt_version"] = "0.9.3.dev0+unknown"
    else:
        committed["attestation"]["description"] = "Changed disclosure"

    result = drift_result("evidence.json", committed, fresh)
    assert result["status"] == "drifted"
    assert "attestation.description" in {entry["field"] for entry in result["drift"]}
