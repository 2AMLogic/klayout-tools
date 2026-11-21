"""Issue #6027: a failing ``klt sim`` citation credits the rows its passing,
tagged, limited measurements grade.

Split out of ``test_signoff_derived_rows.py`` to stay under the per-file size
ratchet; the shared fixtures and helpers still live there.
"""

from __future__ import annotations

from klayout_tools import sim
from klayout_tools.coverage import coverage_refusal_reason
from klayout_tools.signoff import build_tier_report
from klayout_tools.signoff_derived_rows import (
    _RECOGNISED_LIMIT_KEYS,
    _sim_no_limits_applied,
)
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


# Issue #6027, review round: a failed envelope's aggregate ``status: pass`` is
# not proof the row's bound was applied. These envelopes are built with
# `klt sim`'s own rollup and checked-work coverage builders (no ngspice), so
# the work ids the credit reads are the producer's, not a hand-written copy.


def _corner(corner_id: str, **results: tuple[float | None, str]) -> dict:
    """One corner whose measurements are ``name=(value, status)``."""
    measurements = [
        {
            "name": name,
            "value": value,
            "status": status,
            "margin": None if value is None else (0.1 if status == "pass" else -0.1),
        }
        for name, (value, status) in results.items()
    ]
    bad = any(m["status"] in {"fail", "error"} for m in measurements)
    return {
        "corner_id": corner_id,
        "status": "fail" if bad else "pass",
        "measurements": measurements,
    }


def _spec(name: str, limits: dict | None) -> dict:
    return {"name": name, "limits": limits, "spec_row": name}


def _producer_failure(specs: list[dict], corners: list[dict]) -> dict:
    failed = sum(1 for c in corners if c["status"] != "pass")
    return {
        **SIM_FAIL_ENVELOPE,
        "corner_count": len(corners),
        "passed": len(corners) - failed,
        "failed": failed,
        "corners": corners,
        "measurements": sim._rollup_measurements(specs, corners),
        "coverage": sim._build_coverage(specs, corners),
    }


def _rows_of(item: dict) -> tuple[list, dict]:
    return item["detail"]["failed_rows"], item["detail"]["row_sources"]


def test_recognised_limit_keys_mirror_the_producer():
    assert _RECOGNISED_LIMIT_KEYS == sim._RECOGNISED_LIMIT_KEYS


def test_item5_failing_sim_credits_only_rows_whose_bound_was_applied(tmp_path):
    # The review's table: A's key is unrecognised, N's bound is null, B fails
    # its own bound, C is the genuine control. All four measure 1.7.
    specs = [
        _spec("A", {"minimum": 20}),
        _spec("N", {"min": None}),
        _spec("B", {"max": 1.5}),
        _spec("C", {"min": 1}),
    ]
    corners = [
        _corner(
            "tt",
            A=(1.7, "pass"),
            N=(1.7, "pass"),
            B=(1.7, "fail"),
            C=(1.7, "pass"),
        )
    ]
    envelope = _producer_failure(specs, corners)
    reasons = {r["id"]: r["reason"] for r in envelope["coverage"]["skipped"]}
    assert sorted(reasons.values()) == ["missing_limit_value", "unrecognized_limit_key"]
    assert [m["status"] for m in envelope["measurements"]] == [
        "pass",
        "pass",
        "fail",
        "pass",
    ]
    bad = _write(tmp_path, "bad.json", envelope)
    rows = ["A", "N", "B", "C"]

    item = _item5({"5": [{"file": bad, "rows": rows}]}, [{"id": r} for r in rows])

    assert item["reason"] == "check_failed"
    failed_rows, sources = _rows_of(item)
    assert failed_rows == ["A", "B", "N"]
    assert sources == {"C": "derived"}


def test_item5_failing_sim_unreached_corner_withholds_the_row(tmp_path):
    # A passes at tt and is `not_reached` at ss: the aggregate still rolls up
    # `pass`, but the bound was applied at one corner only.
    specs = [
        _spec("A", {"min": 1}),
        _spec("B", {"max": 1.5}),
        _spec("C", {"min": 1}),
    ]
    corners = [
        _corner("tt", A=(1.7, "pass"), B=(1.7, "fail"), C=(1.7, "pass")),
        _corner("ss", A=(None, "not_reached"), B=(1.7, "fail"), C=(1.7, "pass")),
    ]
    envelope = _producer_failure(specs, corners)
    assert envelope["measurements"][0]["status"] == "pass"  # the premise
    skipped = envelope["coverage"]["skipped"]
    assert [r["reason"] for r in skipped] == ["not_reached"]
    assert skipped[0]["id"].startswith("limit:") and '"A"' in skipped[0]["id"]
    bad = _write(tmp_path, "bad.json", envelope)
    rows = ["A", "B", "C"]

    item = _item5({"5": [{"file": bad, "rows": rows}]}, [{"id": r} for r in rows])

    failed_rows, sources = _rows_of(item)
    assert failed_rows == ["A", "B"]
    assert sources == {"C": "derived"}


def test_item5_failing_sim_credits_a_row_bound_at_every_corner(tmp_path):
    specs = [_spec("A", {"min": 1}), _spec("B", {"max": 1.5})]
    corners = [
        _corner("tt", A=(1.7, "pass"), B=(1.7, "fail")),
        _corner("ss", A=(1.2, "pass"), B=(1.7, "fail")),
    ]
    bad = _write(tmp_path, "bad.json", _producer_failure(specs, corners))

    item = _item5(
        {"5": [{"file": bad, "rows": ["A", "B"]}]}, [{"id": "A"}, {"id": "B"}]
    )

    failed_rows, sources = _rows_of(item)
    assert failed_rows == ["B"]
    assert sources == {"A": "derived"}


def test_item5_failing_sim_that_applied_no_bound_credits_nothing(tmp_path):
    # An unbounded observation, an unextractable measurement and a null bound:
    # the run fails, `klt sim` reports `measurements_with_limits: 0`, and the
    # generic coverage gate lets it through -- the sim-specific
    # `no_limits_checked` refusal is what must keep the null-bound row out.
    specs = [_spec("obs", None), _spec("miss", None), _spec("N", {"min": None})]
    corners = [_corner("tt", obs=(1.7, "pass"), miss=(None, "error"), N=(1.7, "pass"))]
    envelope = _producer_failure(specs, corners)
    assert envelope["coverage"]["measurements_with_limits"] == 0
    assert coverage_refusal_reason(envelope) is None
    assert _sim_no_limits_applied(envelope)
    bad = _write(tmp_path, "bad.json", envelope)

    item = _item5({"5": [{"file": bad, "rows": ["N"]}]}, [{"id": "N"}])

    assert item["reason"] == "check_failed"
    failed_rows, sources = _rows_of(item)
    assert failed_rows == ["N"]
    assert sources == {}


def test_item5_failing_sim_declaring_no_applied_bound_credits_no_row(tmp_path):
    # The envelope's own counters say no bound was applied anywhere, although
    # a measurement entry still looks gradable: the sim-specific refusal
    # (`no_limits_checked`) the `met` path applies must keep the capture out,
    # whatever the entry says.
    specs = [_spec("B", {"max": 1.5}), _spec("C", {"min": 1})]
    corners = [_corner("tt", B=(1.7, "fail"), C=(1.7, "pass"))]
    envelope = _producer_failure(specs, corners)
    envelope["coverage"] = {**envelope["coverage"], "measurements_with_limits": 0}
    assert coverage_refusal_reason(envelope) is None
    assert _sim_no_limits_applied(envelope)
    bad = _write(tmp_path, "bad.json", envelope)

    item = _item5(
        {"5": [{"file": bad, "rows": ["B", "C"]}]}, [{"id": "B"}, {"id": "C"}]
    )

    assert item["reason"] == "check_failed"
    failed_rows, sources = _rows_of(item)
    assert failed_rows == ["B", "C"]
    assert sources == {}


def test_item5_failing_sim_unrecognised_or_null_bound_credits_nothing_unrecorded(
    tmp_path,
):
    # Without a checked-work record (an envelope predating `coverage`) the
    # measurement's own bound is the only evidence: it must still be a
    # recognised, non-null one.
    bad = _write(
        tmp_path,
        "bad.json",
        _failing_sim(
            [
                _meas("pa", "A", limits={"minimum": 20}, status="pass"),
                _meas("pn", "N", limits={"min": None}, status="pass"),
                _meas("pc", "C", limits={"max": 2.0}, status="pass"),
                _meas("pb", "B", limits=_LIM, status="fail"),
            ]
        ),
    )
    rows = ["A", "N", "C", "B"]

    item = _item5({"5": [{"file": bad, "rows": rows}]}, [{"id": r} for r in rows])

    failed_rows, sources = _rows_of(item)
    assert failed_rows == ["A", "B", "N"]
    assert sources == {"C": "derived"}


def test_item5_failing_sim_row_with_a_failing_sibling_measurement_stays_failed(
    tmp_path,
):
    # Row R is graded by two limited measurements; one passes, one fails. The
    # sibling's pass must not stand in for the row's own failure. An inert tag
    # (no `limits`) beside a passing one changes nothing.
    bad = _write(
        tmp_path,
        "bad.json",
        _failing_sim(
            [
                _meas("m1", "R", limits=_LIM, status="pass"),
                _meas("m2", "R", limits={"max": 1.5}, status="fail"),
                _meas("m3", "S", limits=_LIM, status="pass"),
                _meas("m4", "S", status="pass"),
            ]
        ),
    )

    item = _item5(
        {"5": [{"file": bad, "rows": ["R", "S"]}]}, [{"id": "R"}, {"id": "S"}]
    )

    failed_rows, sources = _rows_of(item)
    assert failed_rows == ["R"]
    assert sources == {"S": "derived"}
