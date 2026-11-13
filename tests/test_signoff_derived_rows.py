"""Issue #5462: the manifest's opt-in ``require_derived_rows`` ratchet.

T1 item 5 reports whether a covered spec row was machine-checked against the
cited envelope's ``spec_row`` tags (``row_coverage.row_sources``, issue
#3778), but by default an all-``asserted`` item grades identically to a fully
tagged one. These cover the top-level boolean that lets a manifest require
derived coverage. The ``derived``/``asserted`` split itself is covered beside
the other item-5 cases in ``test_signoff.py``.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.signoff import build_tier_report

_LIM = {"min": 1.0}

SIM_PASS_ENVELOPE = {
    "schema_version": 1,
    "netlist": "design.spice",
    "status": "pass",
    "corner_count": 3,
    "passed": 3,
    "failed": 0,
    "errored": 0,
    "environment": {},
    "provenance": {
        "klt_version": "0.2.0",
        "klayout_version": "0.30.10",
        "pdk": {"name": "sky130A", "source": "volare", "version": "20240101"},
        "deck": {"name": "sky130.lib.spice", "content_hash": "sha256:models"},
        "input": {"content_hash": "sha256:netlistA", "role": "netlist"},
    },
    "measurements": [],
    "corners": [],
}

SIM_FAIL_ENVELOPE = {
    **SIM_PASS_ENVELOPE,
    "status": "fail",
    "passed": 2,
    "failed": 1,
}


def _write(tmp_path, name, payload) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return str(path)


def _tagged_sim(measurements: list[dict]) -> dict:
    return {**SIM_PASS_ENVELOPE, "measurements": measurements}


def _meas(name, spec_row=None, *, limits=None, status="pass") -> dict:
    entry = {"name": name, "status": status, "limits": limits}
    if spec_row is not None:
        entry["spec_row"] = spec_row
    return entry


def _manifest(**overrides) -> dict:
    return {"block": "demo-block", "kind": "analog", "evidence": {}, **overrides}


def _item5(evidence, spec_rows=None, **overrides) -> dict:
    manifest = _manifest(evidence=evidence, **overrides)
    if spec_rows is not None:
        manifest["spec_rows"] = {"5": spec_rows}
    return next(i for i in build_tier_report(manifest)["items"] if i["id"] == 5)


def test_item5_require_derived_rows_fails_an_asserted_row(tmp_path):
    a = _write(tmp_path, "a.json", _tagged_sim([_meas("t", limits=_LIM)]))

    item = _item5(
        {"5": [{"file": a, "rows": ["tdec"]}]},
        [{"id": "tdec"}],
        require_derived_rows=True,
    )

    assert item["status"] == "unmet"
    assert item["reason"] == "partial_coverage"
    assert item["detail"]["asserted_rows"] == ["tdec"]
    # The existing `row_coverage` detail keys are still reported alongside it.
    assert item["detail"]["row_sources"] == {"tdec": "asserted"}
    assert item["detail"]["uncovered_rows"] == []


def test_item5_require_derived_rows_lists_asserted_rows_sorted(tmp_path):
    a = _write(tmp_path, "a.json", _tagged_sim([_meas("t", limits=_LIM)]))

    item = _item5(
        {"5": [{"file": a, "rows": ["zin", "tdec"]}]},
        [{"id": "zin"}, {"id": "tdec"}],
        require_derived_rows=True,
    )

    assert item["detail"]["asserted_rows"] == ["tdec", "zin"]


def test_item5_require_derived_rows_passes_a_fully_tagged_item(tmp_path):
    a = _write(
        tmp_path,
        "a.json",
        _tagged_sim([_meas("t", "tdec", limits=_LIM), _meas("z", "zin", limits=_LIM)]),
    )

    item = _item5(
        {"5": [{"file": a, "rows": ["tdec", "zin"]}]},
        [{"id": "tdec"}, {"id": "zin"}],
        require_derived_rows=True,
    )

    assert item["status"] == "met"
    assert item["row_coverage"]["row_sources"] == {"tdec": "derived", "zin": "derived"}
    assert "asserted_rows" not in item.get("detail", {})


def test_item5_require_derived_rows_lists_only_the_untagged_citations_rows(tmp_path):
    tagged = _write(tmp_path, "a.json", _tagged_sim([_meas("t", "tdec", limits=_LIM)]))
    untagged = _write(tmp_path, "b.json", _tagged_sim([_meas("z", limits=_LIM)]))

    item = _item5(
        {
            "5": [
                {"file": tagged, "rows": ["tdec"]},
                {"file": untagged, "rows": ["zin"]},
            ]
        },
        [{"id": "tdec"}, {"id": "zin"}],
        require_derived_rows=True,
    )

    assert item["status"] == "unmet"
    assert item["reason"] == "partial_coverage"
    assert item["detail"]["asserted_rows"] == ["zin"]


def test_item5_require_derived_rows_credits_a_row_covered_both_ways(tmp_path):
    tagged = _write(tmp_path, "a.json", _tagged_sim([_meas("t", "tdec", limits=_LIM)]))
    untagged = _write(tmp_path, "b.json", _tagged_sim([_meas("t", limits=_LIM)]))

    item = _item5(
        {
            "5": [
                {"file": untagged, "rows": ["tdec"]},
                {"file": tagged, "rows": ["tdec"]},
            ]
        },
        [{"id": "tdec"}],
        require_derived_rows=True,
    )

    assert item["status"] == "met"
    assert item["row_coverage"]["row_sources"] == {"tdec": "derived"}


def test_item5_require_derived_rows_off_keeps_an_asserted_row_met(tmp_path):
    a = _write(tmp_path, "a.json", _tagged_sim([_meas("t", limits=_LIM)]))
    evidence = {"5": [{"file": a, "rows": ["tdec"]}]}
    rows = [{"id": "tdec"}]

    absent = _item5(evidence, rows)
    explicit_false = _item5(evidence, rows, require_derived_rows=False)

    assert absent["status"] == "met"
    assert absent == explicit_false
    assert "asserted_rows" not in absent.get("detail", {})


def test_item5_require_derived_rows_still_reports_a_failing_citation_first(tmp_path):
    bad = _write(tmp_path, "bad.json", SIM_FAIL_ENVELOPE)

    item = _item5(
        {"5": [{"file": bad, "rows": ["tdec"]}]},
        [{"id": "tdec"}],
        require_derived_rows=True,
    )

    assert item["status"] == "unmet"
    assert item["reason"] == "check_failed"
    assert item["detail"]["failed_citations"] == [bad]
    assert "asserted_rows" not in item["detail"]


def test_item5_require_derived_rows_with_no_spec_rows_is_a_no_op(tmp_path):
    a = _write(tmp_path, "a.json", _tagged_sim([_meas("t", limits=_LIM)]))
    evidence = {"5": [a]}

    assert _item5(evidence, require_derived_rows=True) == _item5(evidence)


@pytest.mark.parametrize("value", ["true", 1, 0, {}, []])
def test_item5_non_boolean_require_derived_rows_degrades_only_item_5(tmp_path, value):
    a = _write(tmp_path, "a.json", _tagged_sim([_meas("t", limits=_LIM)]))

    def report(**overrides):
        return build_tier_report(
            _manifest(
                evidence={"5": [{"file": a, "rows": ["tdec"]}], "6": [a]},
                spec_rows={"5": [{"id": "tdec"}], "6": [{"id": "mc"}]},
                **overrides,
            )
        )

    bad = report(require_derived_rows=value)

    item = next(i for i in bad["items"] if i["id"] == 5)
    assert item["status"] == "unmet"
    assert item["reason"] == "invalid_evidence"
    assert "require_derived_rows" in item["detail"]["problem"]
    # Every other item -- including item 6, the other multi-citation item --
    # grades exactly as it would with the key absent.
    baseline = report()
    assert [i for i in bad["items"] if i["id"] != 5] == [
        i for i in baseline["items"] if i["id"] != 5
    ]


def test_item5_null_require_derived_rows_keeps_the_default(tmp_path):
    a = _write(tmp_path, "a.json", _tagged_sim([_meas("t", limits=_LIM)]))
    evidence = {"5": [{"file": a, "rows": ["tdec"]}]}
    rows = [{"id": "tdec"}]

    # `null` keeps the default, as every other manifest-level field does.
    assert _item5(evidence, rows, require_derived_rows=None) == _item5(evidence, rows)


def test_item6_ignores_require_derived_rows(tmp_path):
    a = _write(tmp_path, "a.json", _tagged_sim([_meas("t", limits=_LIM)]))

    def item6(**overrides):
        manifest = _manifest(
            evidence={"6": [{"file": a, "rows": ["mc"]}]},
            spec_rows={"6": [{"id": "mc"}]},
            **overrides,
        )
        return next(i for i in build_tier_report(manifest)["items"] if i["id"] == 6)

    assert item6(require_derived_rows=True) == item6()
