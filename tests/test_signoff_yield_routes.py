"""Issue #6399: the two routes into ``undersized_sample`` disclose
differently, and each carries its own detail convention.

The envelope's own ``status: "insufficient_evidence"`` token (issue #2873)
carries ``cited_evidence`` -- the block a ``"met"`` citation would have
carried -- while the campaign-discipline refusal (issue #2467), a *passing*
report whose own ``sample_size.verdict`` says ``insufficient``, carries
``detail.yield_campaign`` instead, never both.

Lives beside ``tests/test_signoff.py`` rather than in it (that file is at
its size-ratchet ceiling), reusing its envelope fixtures.
"""

from __future__ import annotations

from klayout_tools.signoff import build_tier_report
from test_signoff import (
    YIELD_INSUFFICIENT_SAMPLE_ENVELOPE,
    YIELD_PASS_ENVELOPE,
    _manifest,
    _write,
)


def test_yield_insufficient_evidence_renders_unmet_undersized_sample(tmp_path):
    envelope = {**YIELD_PASS_ENVELOPE, "status": "insufficient_evidence"}
    yield_path = _write(tmp_path, "yield.json", envelope)

    result = build_tier_report(_manifest(evidence={"6": yield_path}))

    item_6 = next(item for item in result["items"] if item["id"] == 6)
    assert item_6["status"] == "unmet"
    assert item_6["reason"] == "undersized_sample"
    # Issue #6399: this status-token route carries `cited_evidence` (the
    # citation a `met` item would have had), unlike the campaign-discipline
    # refusal below, which carries `yield_campaign` instead.
    assert item_6["detail"]["cited_evidence"]["kind"] == "yield"
    assert "yield_campaign" not in item_6["detail"]


def test_yield_insufficient_sample_size_renders_unmet_undersized_sample(tmp_path):
    """Issue #2467: the report's own `sample_size.verdict` says its estimate
    is not sized for the claim; a `"pass"` status must not overrule that."""
    yield_path = _write(tmp_path, "yield.json", YIELD_INSUFFICIENT_SAMPLE_ENVELOPE)

    result = build_tier_report(_manifest(evidence={"6": yield_path}))

    item_6 = next(item for item in result["items"] if item["id"] == 6)
    assert item_6["status"] == "unmet"
    assert item_6["reason"] == "undersized_sample"
    assert item_6["citation"] is None
    # Issue #6399: this route's disclosure convention is `yield_campaign`
    # (#2467); it deliberately does not carry `cited_evidence`.
    assert "cited_evidence" not in item_6["detail"]
    campaign = item_6["detail"]["yield_campaign"]
    assert campaign["sample_size"] == "insufficient"
    assert campaign["undersized_measurements"] == ["vref"]
