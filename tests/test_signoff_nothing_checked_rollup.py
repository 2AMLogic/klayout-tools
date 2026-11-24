"""Issue #5479: a tier item whose cited run checked nothing says so.

`klt signoff`'s `nothing_checked` reason (issue #1996) was only reachable for
an envelope reporting its kind's own *success* token on an empty run. Every
producer that has adopted the common coverage rollup instead reports the
zero-coverage token `"not_checked"`, which fails `_check_passed` and so used to
be given `check_failed` -- "a check ran and failed" for a run that ran nothing,
byte-indistinguishable from a `klt pex` delta that really ran and missed its
tolerance. `klt pex --extract-only` (issue #3739) is the first workflow that
commits such an envelope on purpose.

Lives beside `tests/test_signoff.py` rather than in it (that file is at its
size-ratchet ceiling), reusing its envelope fixtures.
"""

from __future__ import annotations

from klayout_tools.signoff import build_tier_report
from test_signoff import (
    DRC_CLEAN_NO_COVERAGE_ENVELOPE,
    DRC_CLEAN_NOTHING_CHECKED_ENVELOPE,
    DRC_CLEAN_SOMETHING_CHECKED_ENVELOPE,
    PEX_FAIL_ENVELOPE,
    PEX_NOTHING_CHECKED_ENVELOPE,
    PEX_PASS_BODY_UNBIASED_ENVELOPE,
    _manifest,
    _write,
)

#: The *same* vacuous DRC run as reported by a producer that has adopted the
#: common rollup rule -- `status` is the zero-coverage token, not the deck's
#: success word. Byte-identical coverage block; only the status token differs,
#: which is exactly why it must grade to the same reason.
DRC_NOTHING_CHECKED_ROLLED_UP_ENVELOPE = {
    **DRC_CLEAN_NOTHING_CHECKED_ENVELOPE,
    "status": "not_checked",
}

#: `klt pex --extract-only` (issue #3739) -- the extraction leg ran and the
#: measurement leg was deliberately not attempted, so `delta` is empty and the
#: rollup reports `"not_checked"`. The extraction's own `body_bias` statement
#: is real (something *was* produced), which is what the record needs named.
PEX_EXTRACT_ONLY_ENVELOPE = {
    **PEX_NOTHING_CHECKED_ENVELOPE,
    "status": "not_checked",
    "measurement": {
        "mode": "extract-only",
        "command": None,
        "timeout_s": None,
        "sides": None,
    },
    "body_bias": {
        "status": "biased",
        "unbiased_device_count": 0,
        "unbiased_nets": [],
        "unbiased_pmos_body_nets": [],
    },
}


def _item(result, item_id):
    return next(item for item in result["items"] if item["id"] == item_id)


def test_the_success_token_nothing_checked_route_names_the_cited_evidence(tmp_path):
    """`citation` stays `null` on an unmet item (the documented contract), but
    the record must still say *which* artifact was read. Issue #1996's route
    carries that in `detail.cited_evidence`, in exactly the shape a `met`
    citation would have."""
    path = _write(tmp_path, "pex.json", PEX_NOTHING_CHECKED_ENVELOPE)

    item_7 = _item(build_tier_report(_manifest(evidence={"7": path})), 7)

    assert item_7["reason"] == "nothing_checked"
    assert item_7["citation"] is None
    assert item_7["detail"]["nothing_checked_reasons"] == ["no_delta_rows"]
    cited = item_7["detail"]["cited_evidence"]
    assert cited["file"] == str(path)
    assert cited["kind"] == "pex"
    assert cited["check_status"] == "pass"
    assert cited["content_hash"] == "sha256:extractedpex"


def test_an_extract_only_pex_citation_renders_nothing_checked(tmp_path):
    """The reported bug: `klt pex --extract-only` reports the rollup's
    zero-coverage token, so before this the item rendered `check_failed` --
    indistinguishable from a delta run that ran and missed its tolerance. It
    measured nothing; the reason must say so, and name the extraction read."""
    path = _write(tmp_path, "pex.json", PEX_EXTRACT_ONLY_ENVELOPE)

    item_7 = _item(build_tier_report(_manifest(evidence={"7": path})), 7)

    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "nothing_checked"
    assert item_7["citation"] is None
    assert item_7["detail"]["nothing_checked_reasons"] == ["no_delta_rows"]
    cited = item_7["detail"]["cited_evidence"]
    assert cited["file"] == str(path)
    assert cited["kind"] == "pex"
    assert cited["check_status"] == "not_checked"
    assert cited["content_hash"] == "sha256:extractedpex"
    # Issue #1983's passthrough: the extraction these (absent) numbers would
    # have come from did state whether every device body had a bias path.
    assert cited["body_bias"]["status"] == "biased"


def test_a_failing_pex_delta_is_still_check_failed(tmp_path):
    """The control the bug collapsed: a comparison that *ran* and found a row
    outside tolerance keeps `check_failed`, so the two stay distinguishable
    from the report alone.

    Issue #5692 reverses #5479's original last sentence here ("No artifact is
    named -- a failed check's remedy is the defect, not the citation"): the
    failing row now names the artifact it read in `detail.cited_evidence`,
    while `citation` stays `null` (met-only contract) and grading is
    unchanged."""
    path = _write(tmp_path, "pex.json", PEX_FAIL_ENVELOPE)

    item_7 = _item(build_tier_report(_manifest(evidence={"7": path})), 7)

    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "check_failed"
    assert item_7["citation"] is None
    cited = item_7["detail"]["cited_evidence"]
    assert cited["file"] == str(path)
    assert cited["kind"] == "pex"
    assert cited["check_status"] == "fail"
    assert cited["content_hash"] == "sha256:extractedpex"
    assert "nothing_checked_reasons" not in item_7["detail"]


def test_a_failing_pex_citation_carries_the_body_bias_disclosure(tmp_path):
    """Issue #5692 AC2: item 7's claimant-enforced `body_bias` statement has an
    in-report source on the failing row, in the shape a `met` citation has."""
    envelope = {
        **PEX_FAIL_ENVELOPE,
        "body_bias": PEX_PASS_BODY_UNBIASED_ENVELOPE["body_bias"],
    }
    path = _write(tmp_path, "pex.json", envelope)

    item_7 = _item(build_tier_report(_manifest(evidence={"7": path})), 7)

    assert item_7["reason"] == "check_failed"
    bias = envelope["body_bias"]
    assert item_7["detail"]["cited_evidence"]["body_bias"] == {
        key: bias[key] for key in ("status", "unbiased_device_count", "unbiased_nets")
    }


def test_a_failing_pex_citation_from_before_body_bias_omits_the_key(tmp_path):
    """An envelope committed before `body_bias` existed: the key is absent from
    `cited_evidence`, never `null`."""
    path = _write(tmp_path, "pex.json", PEX_FAIL_ENVELOPE)

    item_7 = _item(build_tier_report(_manifest(evidence={"7": path})), 7)

    assert "body_bias" not in item_7["detail"]["cited_evidence"]


def test_check_failed_cited_evidence_matches_the_nothing_checked_shape(tmp_path):
    """`check_failed` and `nothing_checked` disclose one artifact through one
    `_citation()` call, so they list the same fields -- asserted by comparing
    two reports, not by a second hand-written field list."""
    body_bias = PEX_EXTRACT_ONLY_ENVELOPE["body_bias"]
    failed = _write(
        tmp_path, "failed.json", {**PEX_FAIL_ENVELOPE, "body_bias": body_bias}
    )
    vacuous = _write(tmp_path, "vacuous.json", PEX_EXTRACT_ONLY_ENVELOPE)

    failed_item = _item(build_tier_report(_manifest(evidence={"7": failed})), 7)
    vacuous_item = _item(build_tier_report(_manifest(evidence={"7": vacuous})), 7)

    assert failed_item["reason"] == "check_failed"
    assert vacuous_item["reason"] == "nothing_checked"
    assert set(failed_item["detail"]["cited_evidence"]) == set(
        vacuous_item["detail"]["cited_evidence"]
    )


def test_a_partial_coverage_row_also_carries_cited_evidence(tmp_path):
    """The attach is not gated on the `check_failed` token: `partial_coverage`
    leaves `_grade_check_outcome` through the same return."""
    path = _write(tmp_path, "pex.json", {**PEX_FAIL_ENVELOPE, "status": "pass_partial"})

    item_7 = _item(build_tier_report(_manifest(evidence={"7": path})), 7)

    assert item_7["reason"] == "partial_coverage"
    assert item_7["citation"] is None
    assert item_7["detail"]["cited_evidence"]["check_status"] == "pass_partial"


def test_the_nothing_checked_row_is_unchanged_by_the_check_failed_disclosure(tmp_path):
    """The `nothing_checked` route still carries exactly one `cited_evidence`
    next to its `nothing_checked_reasons`, and no other `detail` key."""
    path = _write(tmp_path, "pex.json", PEX_EXTRACT_ONLY_ENVELOPE)

    item_7 = _item(build_tier_report(_manifest(evidence={"7": path})), 7)

    assert set(item_7["detail"]) == {"nothing_checked_reasons", "cited_evidence"}


def test_a_rolled_up_nothing_checked_drc_citation_is_not_check_failed(tmp_path):
    """The defect is not pex-specific: the same conflation applied to every
    kind whose producer adopted the rollup. An empty DRC deck cited for item 3
    grades identically whether its producer called the run `"clean"` (issue
    #1996's fixture) or `"not_checked"` (this one)."""
    path = _write(tmp_path, "drc.json", DRC_NOTHING_CHECKED_ROLLED_UP_ENVELOPE)

    item_3 = _item(build_tier_report(_manifest(evidence={"3": path})), 3)

    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "nothing_checked"
    assert item_3["citation"] is None
    assert item_3["detail"]["nothing_checked_reasons"] == ["deck_reported_no_rules"]
    assert item_3["detail"]["cited_evidence"]["kind"] == "drc"


def test_wrong_kind_outranks_a_rolled_up_nothing_checked_envelope(tmp_path):
    """Issue #826's invariant, applied to the new route exactly as
    `test_wrong_kind_still_wins_over_nothing_checked` applies it to the old
    one: an empty DRC report cited for item 7 must not be relabelled as a thin
    *pex*. The actionable problem is the artifact, not its coverage.

    Issue #5692: the misfiled report grades `check_failed` and, like every
    non-passing single citation, now names the artifact it read -- which is
    what lets a reader tell a misfiled DRC report from a failing extraction
    (`cited_evidence.kind == "drc"`). The `reason != "nothing_checked"`
    assertion, the point of this test, is unaffected."""
    path = _write(tmp_path, "drc.json", DRC_NOTHING_CHECKED_ROLLED_UP_ENVELOPE)

    item_7 = _item(build_tier_report(_manifest(evidence={"7": path})), 7)

    assert item_7["status"] == "unmet"
    assert item_7["reason"] != "nothing_checked"
    assert "nothing_checked_reasons" not in item_7["detail"]
    assert item_7["detail"]["cited_evidence"]["kind"] == "drc"
    assert "body_bias" not in item_7["detail"]["cited_evidence"]


def test_a_critical_metric_blocker_outranks_a_rolled_up_nothing_checked(tmp_path):
    """A registered `critical: true` metric that failed its declared polarity
    is a real, mechanically-detected defect, so it keeps precedence over the
    envelope's own coverage statement whatever its status token says -- the
    same signoff-side ordering issue #2152 fixed for the partial token.

    Issue #5692: the `check_failed` row carries the blocker *and* the cited
    artifact -- the merge drops neither key."""
    path = _write(
        tmp_path,
        "drc.json",
        {**DRC_NOTHING_CHECKED_ROLLED_UP_ENVELOPE, "metrics": {"drc__error__count": 5}},
    )

    item_3 = _item(build_tier_report(_manifest(evidence={"3": path})), 3)

    assert item_3["reason"] == "check_failed"
    assert item_3["detail"]["critical_metric_blockers"] == [
        {"metric": "drc__error__count", "value": 5, "higher_is_better": False}
    ]
    assert item_3["detail"]["cited_evidence"]["kind"] == "drc"
    assert "nothing_checked_reasons" not in item_3["detail"]


def test_a_not_checked_envelope_with_no_coverage_block_stays_check_failed(tmp_path):
    """Absence is not evidence (issue #1996), on the status-token route too. An
    envelope whose `status` is the zero-coverage token but which makes no
    `coverage` statement at all proves nothing about what it measured, so it
    keeps the reason it had before."""
    path = _write(
        tmp_path,
        "drc.json",
        {**DRC_CLEAN_NO_COVERAGE_ENVELOPE, "status": "not_checked"},
    )

    item_3 = _item(build_tier_report(_manifest(evidence={"3": path})), 3)

    assert item_3["reason"] == "check_failed"


def test_a_not_checked_envelope_claiming_coverage_stays_check_failed(tmp_path):
    """The other half of the same edge: a `coverage` block that positively says
    `nothing_checked: false` contradicts the status token rather than
    explaining it, so the refusal does not fire -- it keys off the envelope's
    own explicit `true`, never off the token alone."""
    path = _write(
        tmp_path,
        "drc.json",
        {**DRC_CLEAN_SOMETHING_CHECKED_ENVELOPE, "status": "not_checked"},
    )

    item_3 = _item(build_tier_report(_manifest(evidence={"3": path})), 3)

    assert item_3["reason"] == "check_failed"
