"""Issue #5786: a multi-citation item-6 grade carries each met member's
`yield_campaign` block on its `citations_graded` entry.

The single-citation form has disclosed the cited campaign's own
sample-size / negative-control state since issue #2467; the multi-citation
aggregator dropped it, so a graded multi-citation report was no longer a
sufficient place to read each citation's campaign-discipline status. These
cover the copied block (and every shape that must keep omitting it); the
single-citation disclosure itself is covered beside the other issue-#2467
cases in ``test_signoff.py``.
"""

from __future__ import annotations

from klayout_tools.signoff import build_tier_report
from test_signoff import (
    SIM_PASS_ENVELOPE,
    YIELD_FAIL_ENVELOPE,
    YIELD_PASS_ENVELOPE,
    _item6,
    _manifest,
    _write,
    _yield_envelope,
    _yield_measurement,
)


def _item6_rows(tmp_path, entry, rows):
    evidence = {
        str(item_id): _write(tmp_path, "filler.json", YIELD_PASS_ENVELOPE)
        for item_id in range(1, 11)
        if item_id != 6
    }
    evidence["6"] = entry
    manifest = _manifest(kind="digital", evidence=evidence)
    manifest["spec_rows"] = {"6": rows}
    return _item6(build_tier_report(manifest))


def test_each_met_yield_citation_carries_its_own_yield_campaign(tmp_path):
    detected = _write(
        tmp_path,
        "mc_detected.json",
        _yield_envelope(_yield_measurement("offset", negative_control="detected")),
    )
    undeclared = _write(
        tmp_path,
        "mc_undeclared.json",
        _yield_envelope(_yield_measurement("gain_err")),
    )

    item = _item6_rows(
        tmp_path,
        [
            {"file": detected, "rows": ["offset"]},
            {"file": undeclared, "rows": ["gain_err"]},
        ],
        [{"id": "offset"}, {"id": "gain_err"}],
    )

    assert item["status"] == "met"
    entries = item["citations_graded"]
    assert entries[0]["yield_campaign"]["negative_control"] == "detected"
    assert entries[1]["yield_campaign"]["negative_control"] == "not_declared"
    assert entries[0]["yield_campaign"]["sample_size"] == "sufficient"
    # Copied from the member citation, never re-derived: the block beside
    # each grade is the same object the member's own citation carries.
    assert entries[0]["yield_campaign"] == item["citations"][0]["yield_campaign"]
    assert entries[1]["yield_campaign"] == item["citations"][1]["yield_campaign"]


def test_the_block_matches_the_single_citation_form(tmp_path):
    envelope = _yield_envelope(
        _yield_measurement("offset", negative_control="detected")
    )
    path = _write(tmp_path, "mc.json", envelope)

    multi = _item6_rows(
        tmp_path, [{"file": path, "rows": ["offset"]}], [{"id": "offset"}]
    )
    single = _item6(build_tier_report(_manifest(kind="digital", evidence={"6": path})))

    assert multi["status"] == single["status"] == "met"
    assert (
        multi["citations_graded"][0]["yield_campaign"]
        == single["citation"]["yield_campaign"]
    )


def test_sim_citations_graded_entries_still_omit_yield_campaign(tmp_path):
    a = _write(tmp_path, "sim_a.json", SIM_PASS_ENVELOPE)
    b = _write(tmp_path, "sim_b.json", SIM_PASS_ENVELOPE)

    manifest = _manifest(
        kind="analog",
        evidence={"5": [{"file": a, "rows": ["gain"]}, {"file": b, "rows": ["psrr"]}]},
    )
    manifest["spec_rows"] = {"5": [{"id": "gain"}, {"id": "psrr"}]}
    item = next(i for i in build_tier_report(manifest)["items"] if i["id"] == 5)

    assert item["status"] == "met"
    assert len(item["citations_graded"]) == 2
    assert all("yield_campaign" not in e for e in item["citations_graded"])


def test_a_report_with_no_measurements_omits_the_key(tmp_path):
    empty = _write(tmp_path, "mc_empty.json", _yield_envelope())

    item = _item6_rows(
        tmp_path, [{"file": empty, "rows": ["offset"]}], [{"id": "offset"}]
    )

    assert item["status"] == "met"
    assert item["citations"][0].keys() >= {"kind"}
    assert "yield_campaign" not in item["citations_graded"][0]


def test_an_unmet_member_omits_the_key(tmp_path):
    good = _write(tmp_path, "mc_good.json", YIELD_PASS_ENVELOPE)
    bad = _write(tmp_path, "mc_bad.json", YIELD_FAIL_ENVELOPE)

    item = _item6_rows(
        tmp_path,
        [
            {"file": good, "rows": ["offset"]},
            {"file": bad, "rows": ["gain_err"]},
        ],
        [{"id": "offset"}, {"id": "gain_err"}],
    )

    assert item["status"] == "unmet"
    entries = item["citations_graded"]
    # The met member keeps its own block; the unmet member never had a
    # retained citation to copy one from (issue #5786 leaves that path
    # alone -- its refusal detail already discloses the campaign state).
    assert entries[0]["yield_campaign"]["negative_control"] == "not_declared"
    assert "yield_campaign" not in entries[1]
