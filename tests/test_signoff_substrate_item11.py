"""Item 11 substrate-path grading (issue #5346).

Split out of ``test_signoff.py`` to respect the per-file size ratchet.
"""

from __future__ import annotations

from klayout_tools.signoff import build_tier_report
from test_signoff import (
    ERC_CLEAN_ENVELOPE,
    _erc_coverage_block,
    _item_11,
    _manifest,
    _power_delivery_evidence,
)


def _substrate_erc(**overrides) -> dict:
    return {**ERC_CLEAN_ENVELOPE, **overrides}


def _grade_item_11(tmp_path, erc):
    return _item_11(
        build_tier_report(
            _manifest(
                kind="digital",
                evidence={
                    "11": _power_delivery_evidence(
                        tmp_path, kind="digital", erc_envelope=erc
                    )
                },
            )
        )
    )


def test_item_11_unmet_for_a_real_substrate_path_supply_short(tmp_path):
    """Issue #5346: a `via: "substrate"` `erc.supply_short` is never `met`."""
    erc = _substrate_erc(
        status="violations",
        erc_findings=[
            {
                "rule": "erc.supply_short",
                "description": "'VPWR' and 'VGND' share a substrate body",
                "net": "VGND",
                "other_net": "VPWR",
                "gate_id": None,
                "layer": None,
                "bbox": None,
                "via": "substrate",
                "body": {"kind": "substrate", "id": "vsubs", "ties": ["a", "b"]},
            }
        ],
        erc_finding_count=1,
    )

    item = _grade_item_11(tmp_path, erc)

    assert item["status"] == "unmet"
    assert item["reason"] == "supply_not_continuous"


def test_item_11_unmet_when_substrate_isolation_is_disclosed_undecidable(tmp_path):
    """Issue #5346: a disclosed-undecidable substrate path is not `met`, and
    the `missing` list reaches `detail.power_delivery`."""
    erc = _substrate_erc(
        erc_coverage=_erc_coverage_block(
            inapplicable=[
                {
                    "id": "erc.supply_short",
                    "reason": "substrate_isolation_undecidable",
                    "missing": ["substrate_isolation layer for sky130"],
                }
            ]
        )
    )

    item = _grade_item_11(tmp_path, erc)

    assert item["status"] == "unmet"
    assert item["reason"] == "substrate_isolation_undecidable"
    assert item["detail"]["power_delivery"]["substrate_isolation_missing"] == [
        "substrate_isolation layer for sky130"
    ]


def test_item_11_met_when_the_substrate_path_was_evaluated_clean(tmp_path):
    """Issue #5346: evaluated-clean stays `met` and states how it was
    evaluated; a run without the substrate pass carries no such key."""
    erc = _substrate_erc(
        erc_coverage=_erc_coverage_block(
            checked=["erc.supply_short", "erc.supply_short.substrate"],
            checked_by_assertion=["erc.supply_short.substrate"],
        )
    )

    item = _grade_item_11(tmp_path, erc)

    assert item["status"] == "met"
    assert item["citation"]["power_delivery"]["substrate_continuity"] == {
        "checked": True,
        "by_assertion": True,
    }
    plain = _grade_item_11(tmp_path, ERC_CLEAN_ENVELOPE)
    assert "substrate_continuity" not in plain["citation"]["power_delivery"]
