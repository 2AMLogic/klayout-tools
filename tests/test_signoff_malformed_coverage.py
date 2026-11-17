"""`klt signoff --manifest`'s `malformed_coverage` gate and disclosure (#5643).

A cited envelope whose versioned `coverage` block does not validate must be
refused, and the refusal must name what was read and why it was refused --
on the single-envelope route (item 3) *and* on item 11's compound citation,
where `_grade_power_delivery` resolves several parts and previously applied
no coverage gate to any of them. Split out of ``test_signoff.py`` to respect
the per-file size ratchet.
"""

from __future__ import annotations

import pytest

from klayout_tools.coverage import build_check_coverage
from klayout_tools.signoff import build_signoff, build_tier_report
from test_signoff import (
    DRC_CLEAN_ENVELOPE,
    ERC_CLEAN_ENVELOPE,
    LVS_MATCH_SUPPLY_CORRESPONDENCE_ENVELOPE,
    LVS_MISMATCH_ENVELOPE,
    _erc_coverage_block,
    _item_11,
    _manifest,
    _port_evidence,
    _power_delivery_evidence,
    _socket_check_envelope,
    _write,
)

_ANTENNA_CHECKED = 'antenna:["gate0","poly"]'


def _malformed_coverage() -> dict:
    """A v1 block whose `checked` list has been replaced by an object --
    the report's own reproduction recipe, and the one message
    ("checked: work must be an array") every route must quote verbatim."""
    block = build_check_coverage(checked=[_ANTENNA_CHECKED])
    block["checked"] = {"a": 1}
    return block


def _grade_item_11(
    tmp_path, *, erc: dict | None = None, lvs: dict | None = None
) -> dict:
    """Grade an analog item 11 citing `erc`/`lvs` envelope overrides."""
    return _item_11(
        build_tier_report(
            _manifest(
                kind="analog",
                evidence={
                    "11": _power_delivery_evidence(
                        tmp_path,
                        kind="analog",
                        erc_envelope=erc or ERC_CLEAN_ENVELOPE,
                        lvs_envelope=lvs,
                    )
                },
            )
        )
    )


def _item_3(result: dict) -> dict:
    return next(item for item in result["items"] if item["id"] == 3)


# --------------------------------------------------------------------------- #
# AC1: item 11 refuses a malformed coverage block, on either part, with or
# without a valid `erc_coverage` scope beside it.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("erc_coverage", [False, True], ids=["no-scope", "with-scope"])
@pytest.mark.parametrize("part", ["erc", "lvs"])
def test_item_11_refuses_a_malformed_coverage_block(tmp_path, part, erc_coverage):
    erc: dict = {**ERC_CLEAN_ENVELOPE, "coverage": _malformed_coverage()}
    if erc_coverage:
        # A valid connectivity scope must not suppress the refusal the way
        # `_coverage_refusal`'s erc widening suppresses a vacuous one -- the
        # widening exists for #2179's table-less PDK, not for a block that
        # claims coverage it cannot validate.
        erc["erc_coverage"] = _erc_coverage_block(
            checked=['erc.net_connectivity:["VPWR"]']
        )
    lvs: dict = {
        **LVS_MATCH_SUPPLY_CORRESPONDENCE_ENVELOPE,
        "coverage": _malformed_coverage(),
    }

    item = _grade_item_11(
        tmp_path,
        erc=erc if part == "erc" else None,
        lvs=lvs if part == "lvs" else None,
    )

    assert item["status"] == "unmet"
    assert item["reason"] == "malformed_coverage"
    assert item["citation"] is None
    # AC3: the row names the artifact that was read and what was invalid in
    # it -- not just that something was.
    assert item["detail"]["cited_evidence"]["kind"] == part
    assert item["detail"]["cited_evidence"]["file"].endswith(f"pd-{part}.json")
    assert item["detail"]["cited_evidence"]["check_status"] in ("clean", "match")
    assert item["detail"]["cited_evidence"]["content_hash"] is not None
    assert item["detail"]["coverage_error"] == "checked: work must be an array"


# --------------------------------------------------------------------------- #
# AC3, single-envelope route: the refusal names the artifact and the message.
# --------------------------------------------------------------------------- #


def test_single_envelope_malformed_coverage_row_names_artifact_and_message(tmp_path):
    path = _write(
        tmp_path, "drc.json", {**DRC_CLEAN_ENVELOPE, "coverage": _malformed_coverage()}
    )

    item = _item_3(build_tier_report(_manifest(evidence={"3": path})))

    assert item["status"] == "unmet"
    assert item["reason"] == "malformed_coverage"
    assert item["citation"] is None
    evidence = item["detail"]["cited_evidence"]
    assert evidence["kind"] == "drc"
    assert evidence["file"].endswith("drc.json")
    assert evidence["check_status"] == "clean"
    assert evidence["content_hash"] is not None
    assert item["detail"]["coverage_error"] == "checked: work must be an array"


# --------------------------------------------------------------------------- #
# AC5: envelope aggregation and --manifest agree that the block is malformed.
# --------------------------------------------------------------------------- #


def test_both_modes_report_the_same_malformed_coverage_block(tmp_path):
    drc_path = _write(
        tmp_path, "drc.json", {**DRC_CLEAN_ENVELOPE, "coverage": _malformed_coverage()}
    )
    aggregated = next(
        c for c in build_signoff([drc_path])["checks"] if c["kind"] == "drc"
    )
    assert aggregated["detail"]["coverage_state"] == "malformed"
    assert aggregated["detail"]["coverage_error"] == "checked: work must be an array"
    item = _item_3(build_tier_report(_manifest(evidence={"3": drc_path})))
    assert (item["reason"], item["detail"]["coverage_error"]) == (
        "malformed_coverage",
        aggregated["detail"]["coverage_error"],
    )

    erc = {**ERC_CLEAN_ENVELOPE, "coverage": _malformed_coverage()}
    erc_aggregated = next(
        c
        for c in build_signoff([_write(tmp_path, "erc.json", erc)])["checks"]
        if c["kind"] == "erc"
    )
    assert erc_aggregated["detail"]["coverage_state"] == "malformed"
    item_11 = _grade_item_11(tmp_path, erc=erc)
    assert (item_11["reason"], item_11["detail"]["coverage_error"]) == (
        "malformed_coverage",
        erc_aggregated["detail"]["coverage_error"],
    )


# --------------------------------------------------------------------------- #
# No-regression controls: valid, partial and absent coverage grade as before.
# --------------------------------------------------------------------------- #


def test_item_11_still_met_with_valid_versioned_coverage(tmp_path):
    erc = {
        **ERC_CLEAN_ENVELOPE,
        "coverage": build_check_coverage(checked=[_ANTENNA_CHECKED]),
    }

    item = _grade_item_11(tmp_path, erc=erc)

    assert item["status"] == "met"
    assert item["reason"] is None
    assert item["citation"]["kind"] == "erc"
    assert [part["kind"] for part in item["citation"]["parts"]] == ["erc", "lvs"]


def test_item_11_coverage_less_fixture_still_met(tmp_path):
    """The stock `_power_delivery_evidence` erc envelope carries no
    `coverage` block at all -- absence is not evidence (docs/cli/signoff.md),
    so it must keep grading exactly as it always has."""

    item = _grade_item_11(tmp_path)

    assert item["status"] == "met"
    assert item["reason"] is None


def test_partial_coverage_still_qualifies_and_still_meets(tmp_path):
    erc = {
        **ERC_CLEAN_ENVELOPE,
        "coverage": build_check_coverage(
            checked=[_ANTENNA_CHECKED],
            skipped=[
                {
                    "id": 'antenna:["gate0","li1"]',
                    "reason": "missing_antenna_pdk",
                }
            ],
        ),
    }

    item = _grade_item_11(tmp_path, erc=erc)

    assert item["status"] == "met"
    assert item["reason"] is None
    qualification = item["citation"]["coverage_qualification"]
    assert qualification["reason"] == "partial_coverage"
    assert qualification["skipped"][0]["reason"] == "missing_antenna_pdk"


# --------------------------------------------------------------------------- #
# Edge cases from the issue's test plan.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "coverage",
    [3, "schema"],
    ids=["int", "string"],
)
def test_item_11_malformed_non_object_block(tmp_path, coverage):
    item = _grade_item_11(tmp_path, erc={**ERC_CLEAN_ENVELOPE, "coverage": coverage})

    assert (item["status"], item["reason"]) == ("unmet", "malformed_coverage")
    assert item["detail"]["coverage_error"] == "coverage must be an object"


def test_item_11_malformed_missing_schema_version(tmp_path):
    block = build_check_coverage(checked=[_ANTENNA_CHECKED])
    del block["schema_version"]
    item = _grade_item_11(tmp_path, erc={**ERC_CLEAN_ENVELOPE, "coverage": block})

    assert (item["status"], item["reason"]) == ("unmet", "malformed_coverage")
    assert (
        item["detail"]["coverage_error"]
        == "unsupported or missing coverage.schema_version"
    )


def test_item_11_malformed_known_unknown_contradiction(tmp_path):
    block = build_check_coverage(
        checked=[_ANTENNA_CHECKED],
        unknown=[{"id": 'antenna:["gate0","mcon"]', "reason": "no_model"}],
    )
    block["known"] = True
    item = _grade_item_11(tmp_path, erc={**ERC_CLEAN_ENVELOPE, "coverage": block})

    assert (item["status"], item["reason"]) == ("unmet", "malformed_coverage")
    assert item["detail"]["coverage_error"] == "known contradicts unknown work"


def test_item_11_refuses_a_malformed_part_it_did_not_need(tmp_path):
    """AC2: the gate runs over *every* resolved part, in part order -- a
    socket-check citation this analog block never needed is still refused
    when its coverage block does not validate, and named in the detail."""
    parts = _port_evidence(
        tmp_path,
        socket={**_socket_check_envelope(), "coverage": _malformed_coverage()},
    )

    item = _item_11(build_tier_report(_manifest(kind="analog", evidence={"11": parts})))

    assert (item["status"], item["reason"]) == ("unmet", "malformed_coverage")
    assert item["detail"]["cited_evidence"]["kind"] == "socket-check"


def test_a_failing_part_outranks_its_malformed_coverage(tmp_path):
    """Guidance step 1: a part that *failed* is reported as the failure it
    is, before any coverage complaint about it -- the LVS mismatch verdict
    must win over the erc part's malformed block."""
    item = _grade_item_11(
        tmp_path,
        erc={**ERC_CLEAN_ENVELOPE, "coverage": _malformed_coverage()},
        lvs=LVS_MISMATCH_ENVELOPE,
    )

    assert item["status"] == "unmet"
    assert item["reason"] == "lvs_did_not_pass"
