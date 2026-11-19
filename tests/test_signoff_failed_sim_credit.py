"""Issue #6027: a failing ``klt sim`` citation credits the rows its passing,
tagged, limited measurements grade.

Split out of ``test_signoff_derived_rows.py`` to stay under the per-file size
ratchet; the shared fixtures and helpers still live there.
"""

from __future__ import annotations

from klayout_tools.signoff import build_tier_report
from test_signoff import (
    FUNCTIONAL_VERIFICATION_PASS_ENVELOPE,
    STA_MULTI_CORNER_SETUP_VIOLATION_ENVELOPE,
)
from test_signoff_derived_rows import (
    _LIM,
    SIM_FAIL_ENVELOPE,
    _item5,
    _manifest,
    _meas,
    _tagged_sim,
    _write,
)

# Issue #6027: a failing `klt sim` envelope credits its passing rows -- the
# citation still grades `unmet`/`check_failed`, but rows its tagged, limited,
# passing measurements grade leave `failed_rows` and read `derived` in
# `row_sources`.


def _failing_sim(measurements: list[dict]) -> dict:
    return {**SIM_FAIL_ENVELOPE, "measurements": measurements}


#: The issue's reproduction: row `tdec` tagged+limited+pass, `tdyn`
#: tagged+limited+fail, one envelope, overall `status: "fail"`.
TWO_ROW_FAIL_SIM = _failing_sim(
    [
        _meas("pa", "tdec", limits=_LIM, status="pass"),
        _meas("pb", "tdyn", limits=_LIM, status="fail"),
    ]
)


def test_item5_failing_sim_credits_only_its_passing_row(tmp_path):
    bad = _write(tmp_path, "bad.json", TWO_ROW_FAIL_SIM)

    item = _item5({"5": [{"file": bad, "rows": ["tdec"]}]}, [{"id": "tdec"}])

    assert item["status"] == "unmet"
    assert item["reason"] == "check_failed"
    assert item["detail"]["failed_citations"] == [bad]
    assert item["detail"]["failed_rows"] == []
    assert item["detail"]["uncovered_rows"] == []
    assert item["detail"]["row_sources"] == {"tdec": "derived"}

    both = _item5(
        {"5": [{"file": bad, "rows": ["tdec", "tdyn"]}]},
        [{"id": "tdec"}, {"id": "tdyn"}],
    )
    assert both["detail"]["failed_rows"] == ["tdyn"]
    assert both["detail"]["row_sources"] == {"tdec": "derived"}


def test_item5_failing_sim_cited_alone_keeps_the_failing_row_failed(tmp_path):
    bad = _write(tmp_path, "bad.json", TWO_ROW_FAIL_SIM)

    item = _item5(
        {"5": [{"file": bad, "rows": ["tdyn"]}]}, [{"id": "tdec"}, {"id": "tdyn"}]
    )

    assert item["reason"] == "check_failed"
    assert item["detail"]["failed_rows"] == ["tdyn"]
    assert item["detail"]["row_sources"] == {}
    assert item["detail"]["uncovered_rows"] == ["tdec"]


def test_item5_failing_untagged_sim_credits_nothing(tmp_path):
    bad = _write(tmp_path, "bad.json", SIM_FAIL_ENVELOPE)

    item = _item5({"5": [{"file": bad, "rows": ["tdec"]}]}, [{"id": "tdec"}])

    assert item["reason"] == "check_failed"
    assert item["detail"]["failed_rows"] == ["tdec"]
    assert item["detail"]["row_sources"] == {}


def test_item5_failing_sim_tag_without_limits_grades_nothing(tmp_path):
    bad = _write(
        tmp_path, "bad.json", _failing_sim([_meas("pa", "tdec", status="pass")])
    )

    item = _item5({"5": [{"file": bad, "rows": ["tdec"]}]}, [{"id": "tdec"}])

    assert item["reason"] == "check_failed"
    assert item["detail"]["failed_rows"] == ["tdec"]
    assert item["detail"]["row_sources"] == {}


def test_item5_failing_sim_untagged_measurement_credits_nothing_extra(tmp_path):
    bad = _write(
        tmp_path,
        "bad.json",
        _failing_sim(
            [
                _meas("pa", "tdec", limits=_LIM, status="pass"),
                _meas("pz", limits=_LIM),
            ]
        ),
    )

    item = _item5({"5": [{"file": bad, "rows": ["tdec"]}]}, [{"id": "tdec"}])

    assert item["detail"]["failed_rows"] == []
    assert item["detail"]["row_sources"] == {"tdec": "derived"}


def test_item5_row_cited_by_failed_and_met_members_reads_derived(tmp_path):
    bad = _write(
        tmp_path,
        "bad.json",
        _failing_sim([_meas("pb", "tdec", limits=_LIM, status="fail")]),
    )
    good = _write(
        tmp_path, "good.json", _tagged_sim([_meas("pa", "tdec", limits=_LIM)])
    )

    item = _item5(
        {"5": [{"file": bad, "rows": ["tdec"]}, {"file": good, "rows": ["tdec"]}]},
        [{"id": "tdec"}],
    )

    assert item["status"] == "unmet"
    assert item["reason"] == "check_failed"
    assert item["detail"]["failed_rows"] == []
    assert item["detail"]["row_sources"] == {"tdec": "derived"}


def test_digital_item5_failing_sta_credits_nothing(tmp_path):
    bad_sta = _write(tmp_path, "sta.json", STA_MULTI_CORNER_SETUP_VIOLATION_ENVELOPE)
    fv = _write(tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_PASS_ENVELOPE)
    manifest = _manifest(
        kind="digital",
        evidence={"5.digital": [{"file": bad_sta, "rows": ["tclk"]}, {"file": fv}]},
        spec_rows={"5": [{"id": "tclk"}]},
    )

    item = next(i for i in build_tier_report(manifest)["items"] if i["id"] == 5)

    assert item["status"] == "unmet"
    assert item["reason"] == "check_failed"
    assert item["detail"]["failed_rows"] == ["tclk"]
    assert item["detail"]["row_sources"] == {}


def test_item5_stale_pinned_sim_credits_nothing(tmp_path):
    good = _write(tmp_path, "good.json", _tagged_sim([_meas("t", "tdec", limits=_LIM)]))

    item = _item5(
        {
            "5": [
                {
                    "file": good,
                    "rows": ["tdec"],
                    "content_hash": "sha256:stale-revision",
                }
            ]
        },
        [{"id": "tdec"}],
    )

    assert item["status"] == "unmet"
    assert item["reason"] == "stale_evidence"
    assert item["detail"]["failed_rows"] == ["tdec"]
    assert item["detail"]["row_sources"] == {}
