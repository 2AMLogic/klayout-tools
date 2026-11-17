"""`klt signoff` reading a `klt drc --engine both` (cross-check) envelope
(issue #5815): the projection at the ingestion boundary, its per-engine
disclosure, and the `citation.pointer` record. All fixtures are hand-built
envelope dicts shaped exactly like `drc_crosscheck.run_drc_crosscheck`
output -- no klayout binary, no PDK."""

from __future__ import annotations

import json

import pytest

from klayout_tools.signoff import SignoffError, build_signoff, build_tier_report
from klayout_tools.signoff_drc_crosscheck import (
    crosscheck_disclosure,
    crosscheck_project,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

HASH = "sha256:" + "a" * 64
ZERO_COVERAGE = {
    "nothing_checked": True,
    "nothing_checked_reasons": ["deck_has_no_rules"],
}


def _coverage(*, gaps=None, skipped=None, scope=None):
    return {
        "layers_in_stream_without_rules": list(gaps or []),
        "rules_skipped": list(skipped or []),
        "deck_scope": list(scope or ["5.x"]),
    }


def _report(
    engine,
    *,
    status="clean",
    violations=(),
    coverage=None,
    rule_selection=None,
    nothing_checked=False,
):
    """One embedded per-engine report: a complete `klt drc` envelope."""
    block = dict(coverage or _coverage())
    if nothing_checked:
        block = {**ZERO_COVERAGE, "rules_skipped": []}
    report = {
        "schema_version": 2,
        "file": "x.gds",
        "deck": "sky130",
        "status": status,
        "violation_count": len(violations),
        "violations": list(violations),
        "coverage": block,
        "provenance": {
            "klt_version": "0.9.3",
            "klayout_version": "0.30",
            "pdk": {"name": "sky130A", "source": "s", "version": "v"},
            "deck": {"name": engine, "content_hash": HASH},
            "input": {"content_hash": HASH, "role": "layout"},
        },
    }
    if rule_selection is not None:
        report["rule_selection"] = rule_selection
    return report


def _violation(rule="nwell.6"):
    return {
        "rule": rule,
        "bbox": {"left": 0, "bottom": 0, "right": 10, "top": 10},
    }


def _identity(present=True):
    if not present:
        return None
    return {
        "layout_content_hash": HASH,
        "pdk": {"name": "sky130A"},
        "klt_version": "0.9.3",
        "curated_deck": {"name": "curated"},
        "native_deck": {"name": "native"},
    }


def _envelope(
    curated,
    native,
    *,
    status,
    comparison_state="agree_clean",
    identity=True,
):
    return {
        "schema_version": 2,
        "mode": "crosscheck",
        "file": "x.gds",
        "status": status,
        "identity": _identity(identity),
        "comparison": {
            "state": comparison_state,
            "granularity": "region",
            "groups": [],
            "unmapped": {"curated": [], "native": []},
        },
        "reports": {"curated": curated, "native": native},
    }


def _both_clean(gaps=("70/20",)):
    return _envelope(
        _report("curated", coverage=_coverage(gaps=gaps)),
        _report("native"),
        status="clean",
    )


def _native_dirty():
    return _envelope(
        _report("curated"),
        _report("native", status="violations", violations=[_violation()]),
        status="violations",
        comparison_state="agree_violations",
    )


def _manifest(evidence):
    return {"block": "demo-block", "kind": "analog", "evidence": evidence}


def _write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return str(path)


def _item3(result):
    return next(item for item in result["items"] if item["id"] == 3)


def test_whole_crosscheck_citation_grades_met_with_both_engine_disclosure(
    tmp_path,
):
    """The filed gap: a whole `--engine both` envelope cited for item 3
    grades `met` on the envelope's own top-level `status` (was
    `unmet`/`unrecognized_envelope`), and the citation discloses what was
    actually graded -- each engine's own status, the comparison state and
    each side's three item-3 coverage fields."""
    path = _write(tmp_path, "crosscheck.json", _both_clean())

    item = _item3(build_tier_report(_manifest(evidence={"3": path})))

    assert item["status"] == "met"
    assert item["reason"] is None
    citation = item["citation"]
    assert citation is not None
    assert citation["kind"] == "drc"
    assert citation["check_status"] == "clean"
    # The single-engine `coverage` disclosure quotes the projected (curated)
    # side's three fields, exactly as a single-engine citation would.
    assert citation["coverage"]["layers_in_stream_without_rules"] == ["70/20"]
    disclosure = citation["drc_crosscheck"]
    assert disclosure["mode"] == "crosscheck"
    assert disclosure["comparison_state"] == "agree_clean"
    assert disclosure["engines"]["curated"]["status"] == "clean"
    assert disclosure["engines"]["native"]["status"] == "clean"
    assert disclosure["engines"]["curated"]["coverage"] == _coverage(gaps=("70/20",))
    assert disclosure["engines"]["native"]["coverage"] == _coverage()
    # A whole-envelope citation carries no pointer.
    assert "pointer" not in citation


def test_single_engine_envelope_still_grades_met(tmp_path):
    """The positive control, in the same run: an ordinary single-engine
    `klt drc` envelope is untouched by the projection."""
    single = _write(
        tmp_path, "single.json", _report("curated", coverage=_coverage(gaps=("70/20",)))
    )
    cross = _write(tmp_path, "crosscheck.json", _both_clean())

    single_item = _item3(build_tier_report(_manifest(evidence={"3": single})))
    cross_item = _item3(build_tier_report(_manifest(evidence={"3": cross})))

    assert single_item["status"] == cross_item["status"] == "met"
    assert single_item["citation"]["coverage"] == cross_item["citation"]["coverage"]
    assert "drc_crosscheck" not in single_item["citation"]


def test_native_violations_fail_the_whole_citation(tmp_path):
    """The regression test for the second verified correction: a curated-clean
    envelope whose native side found violations must not be graded `met` when
    cited whole -- the false `met` the `pointer: /reports/curated`
    workaround used to render."""
    path = _write(tmp_path, "cross-native-dirty.json", _native_dirty())

    item = _item3(build_tier_report(_manifest(evidence={"3": path})))

    assert item["status"] == "unmet"
    assert item["reason"] == "check_failed"
    assert item["citation"] is None


def test_pointer_still_selects_one_side_and_is_now_recorded(tmp_path):
    """The pointer contract (#2342) is unchanged: `pointer: /reports/curated`
    still selects one side as a standalone `drc` envelope. What changes is
    that the citation now records the pointer, so a side-pointed cross-check
    citation is distinguishable in the JSON from a whole-envelope one."""
    composed = _write(tmp_path, "cross-native-dirty.json", _native_dirty())

    item = _item3(
        build_tier_report(
            _manifest(evidence={"3": {"file": composed, "pointer": "/reports/curated"}})
        )
    )

    assert item["status"] == "met"
    assert item["reason"] is None
    assert item["citation"]["check_status"] == "clean"
    assert item["citation"]["pointer"] == "/reports/curated"
    # Only one side was graded: the projection is a no-op on it.
    assert "drc_crosscheck" not in item["citation"]


def test_pointer_is_recorded_for_any_carrying_entry(tmp_path):
    """`citation.pointer` is recorded for any evidence entry that carried
    one -- including a plain composite document, not just a cross-check."""
    composed = _write(
        tmp_path, "composed.json", {"drc": _report("curated"), "notes": []}
    )

    item = _item3(
        build_tier_report(
            _manifest(evidence={"3": {"file": composed, "pointer": "/drc"}})
        )
    )

    assert item["status"] == "met"
    assert item["citation"]["pointer"] == "/drc"


def test_either_side_rule_subset_denies_full_deck_credit(tmp_path):
    """A cross-check run must not get credit a single-engine run would be
    refused (#2768): a `--rules` subset declared by *either* embedded side
    withholds full-deck credit even when the top-level status is `clean`."""
    subset = {"subset": True, "omitted": ["met1.1"]}
    curated_subset = _envelope(
        _report("curated", rule_selection=subset),
        _report("native"),
        status="clean",
    )
    native_subset = _envelope(
        _report("curated"),
        _report("native", rule_selection=subset),
        status="clean",
    )

    for name, envelope in (
        ("curated-subset.json", curated_subset),
        ("native-subset.json", native_subset),
    ):
        path = _write(tmp_path, name, envelope)
        item = _item3(build_tier_report(_manifest(evidence={"3": path})))
        assert item["status"] == "unmet", name
        # The same reason a single-engine `--rules` subset renders (#2768:
        # the shortfall is the rules it deliberately omitted, a coverage gap
        # rather than a defect) -- the projection must not improve on it.
        assert item["reason"] == "partial_coverage", name


def test_either_side_nothing_checked_still_refused(tmp_path):
    """The #1996 vacuous-verdict refusal, read per engine: either side
    reporting `coverage.nothing_checked: true` refuses the citation, never
    `met`."""
    curated_zero = _envelope(
        _report("curated", status="not_checked", nothing_checked=True),
        _report("native"),
        status="not_checked",
    )
    native_zero = _envelope(
        _report("curated"),
        _report("native", status="not_checked", nothing_checked=True),
        status="not_checked",
    )

    for name, envelope in (
        ("curated-zero.json", curated_zero),
        ("native-zero.json", native_zero),
    ):
        path = _write(tmp_path, name, envelope)
        item = _item3(build_tier_report(_manifest(evidence={"3": path})))
        assert item["status"] == "unmet", name
        assert item["reason"] == "nothing_checked", name
        assert item["citation"] is None, name


def test_native_absent_envelope_is_readable_and_discloses_absence(tmp_path):
    """A native-side-absent envelope (`reports.native: null`, the engine was
    unavailable or failed) is still readable, grades `unmet` on its
    `status: "not_checked"`, and its disclosure states the native side is
    absent rather than implying a two-engine claim."""
    envelope = _envelope(
        _report("curated"),
        None,
        status="not_checked",
        comparison_state="native_unavailable",
        identity=False,
    )
    path = _write(tmp_path, "cross-native-absent.json", envelope)

    item = _item3(build_tier_report(_manifest(evidence={"3": path})))

    assert item["status"] == "unmet"
    assert item["reason"] == "check_failed"

    projected = crosscheck_project(envelope)
    assert projected is not None
    disclosure = crosscheck_disclosure(projected)
    assert disclosure is not None
    engines = disclosure["drc_crosscheck"]["engines"]
    assert engines["native"] is None
    assert engines["curated"]["status"] == "clean"
    assert disclosure["drc_crosscheck"]["comparison_state"] == ("native_unavailable")


def test_aggregation_mode_classifies_crosscheck_as_drc(tmp_path):
    """`klt signoff crosscheck.json` (envelope-aggregation mode) classifies
    the envelope as one `drc` check instead of exiting 1 with "unrecognized
    shape"."""
    path = _write(tmp_path, "crosscheck.json", _both_clean())

    result = build_signoff([path])

    assert result["status"] == "pass"
    assert result["check_count"] == 1
    check = result["checks"][0]
    assert check["kind"] == "drc"
    assert check["passed"] is True
    assert check["detail"]["violation_count"] == 0


def test_malformed_curated_side_is_still_refused(tmp_path):
    """`project()` must not launder a broken artifact past
    `_validate_envelope`: a cross-check whose `reports.curated` is missing
    `status` is refused as `unrecognized_envelope`, exactly as before."""
    broken = _envelope({"violations": []}, _report("native"), status="clean")
    path = _write(tmp_path, "broken.json", broken)

    item = _item3(build_tier_report(_manifest(evidence={"3": path})))
    assert item["status"] == "unmet"
    assert item["reason"] == "unrecognized_envelope"

    with pytest.raises(SignoffError):
        build_signoff([path])


def test_malformed_top_level_status_is_not_projected():
    """A cross-check envelope with no readable top-level `status` (the field
    the verdict is graded on) is not projected -- refusing rather than
    silently grading the curated side's own status."""
    envelope = _both_clean()
    del envelope["status"]
    assert crosscheck_project(envelope) is None


def test_project_is_a_no_op_on_existing_envelopes():
    """Every existing single-engine envelope grades byte-identically: the
    projection returns `None` for anything without `mode == "crosscheck"`."""
    assert crosscheck_project(_report("curated")) is None
    # The value a `pointer: /reports/curated` citation resolves to is a
    # standalone drc envelope -- no `mode` key, so it is never projected.
    assert crosscheck_project(_both_clean()["reports"]["curated"]) is None
    assert crosscheck_project({"schema_version": 1, "violations": []}) is None
    assert crosscheck_project("not an envelope") is None


def test_projection_reuses_every_curated_field():
    """The projection is the curated report with the cross-check's own
    verdict: `violations`/`provenance` are the curated side's verbatim, and
    the source envelope stays reachable for the disclosure."""
    envelope = _native_dirty()
    projected = crosscheck_project(envelope)

    assert projected["status"] == "violations"
    assert projected["violations"] == envelope["reports"]["curated"]["violations"]
    assert projected["provenance"] is envelope["reports"]["curated"]["provenance"]
    assert crosscheck_disclosure({"status": "clean"}) is None
