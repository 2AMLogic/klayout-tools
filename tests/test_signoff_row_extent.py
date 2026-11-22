"""Configuration slices declared by item 5 and 6 spec rows (#7006)."""

from __future__ import annotations

import pytest

from klayout_tools.cli.color import PLAIN
from klayout_tools.cli.signoff_cmd import _print_tier_report_text
from klayout_tools.signoff import build_tier_report
from test_signoff import YIELD_PASS_ENVELOPE
from test_signoff_derived_rows import SIM_FAIL_ENVELOPE, _meas, _tagged_sim, _write


def _sim(tmp_path, file, tag="ratio"):
    return _write(
        tmp_path, file, _tagged_sim([_meas("division", tag, limits={"min": 1})])
    )


def _cite(file, *slices):
    return {"file": file, "rows": ["ratio"], "covers": {"ratio": list(slices)}}


def _report(rows, citations, item=5, **options):
    return build_tier_report(
        {
            "kind": "analog",
            "spec_rows": {str(item): rows},
            "evidence": {str(item): citations},
            **options,
        }
    )


def _item(report, item=5):
    return next(i for i in report["items"] if i["id"] == item)


def test_single_divisor_does_not_cover_full_range(tmp_path, capsys):
    extent = [f"N={i}" for i in range(4, 65)]
    missing = [s for s in extent if s != "N=33"]
    report = _report(
        [{"id": "ratio", "extent": extent}], _cite(_sim(tmp_path, "33.json"), "N=33")
    )
    item = _item(report)

    assert (item["status"], item["reason"]) == ("unmet", "partial_coverage")
    slices = {"covered": ["N=33"], "missing": missing}
    assert item["row_coverage"]["row_slices"] == {"ratio": slices}
    assert item["detail"]["partial_rows"] == [
        {"id": "ratio", "extent": extent, **slices}
    ]
    assert item["detail"]["uncovered_rows"] == ["ratio"]
    assert item["detail"]["row_sources"] == {}
    _print_tier_report_text(report, PLAIN)
    output = capsys.readouterr().out
    assert "PARTIAL" in output and "1/61" in output and "N=64" in output


@pytest.mark.parametrize("item", [5, 6])
def test_overlapping_citations_cover_union_in_declaration_order(tmp_path, item):
    extent = ["low", "typ", "high"]
    if item == 6:
        a = _write(tmp_path, "a.json", YIELD_PASS_ENVELOPE)
        b = _write(tmp_path, "b.json", YIELD_PASS_ENVELOPE)
    else:
        a, b = _sim(tmp_path, "a.json"), _sim(tmp_path, "b.json")
    report = _report(
        [{"id": "ratio", "extent": extent}],
        [_cite(a, "typ", "low", "typ"), _cite(b, "high", "typ")],
        item,
    )
    entry = _item(report, item)
    assert entry["status"] == "met"
    assert entry["row_coverage"]["row_slices"] == {
        "ratio": {"covered": extent, "missing": []}
    }
    assert entry["row_coverage"]["row_sources"]["ratio"] == (
        "derived" if item == 5 else "asserted"
    )


def test_failed_citation_keeps_its_reason_and_contributes_no_slices(tmp_path):
    a = _sim(tmp_path, "a.json")
    b = _write(tmp_path, "b.json", SIM_FAIL_ENVELOPE)
    row = {"id": "ratio", "extent": ["one", "two"]}
    entry = _item(_report([row], [_cite(a, "one"), _cite(b, "two")]))
    assert entry["reason"] == "check_failed"
    assert entry["detail"]["failed_rows"] == ["ratio"]
    assert entry["row_coverage"]["row_slices"]["ratio"] == {
        "covered": ["one"],
        "missing": ["two"],
    }


def test_failed_sim_preserves_derived_row_credit_without_claiming_slices(tmp_path):
    measurements = [
        _meas("voltage", "output", limits={"min": 1}, status="pass"),
        _meas("division", "ratio", limits={"min": 1}, status="pass"),
        _meas("settling", "settling", limits={"min": 1}, status="fail"),
    ]
    envelope = {**SIM_FAIL_ENVELOPE, "measurements": measurements}
    citation = {
        "file": _write(tmp_path, "mixed.json", envelope),
        "rows": ["output", "ratio", "settling"],
        "covers": {"ratio": ["N=4"]},
    }
    rows = [{"id": "output"}, {"id": "ratio", "extent": ["N=4"]}, {"id": "settling"}]

    item = _item(_report(rows, citation))

    assert item["reason"] == "check_failed"
    assert item["detail"]["row_sources"] == {"output": "derived"}
    assert item["detail"]["failed_rows"] == ["ratio", "settling"]
    assert item["row_coverage"]["row_slices"]["ratio"] == {
        "covered": [],
        "missing": ["N=4"],
    }


@pytest.mark.parametrize("tag,covers", [("unrelated", True), ("ratio", False)])
def test_citation_must_name_slices_and_be_credited_with_row(tmp_path, tag, covers):
    cite = _cite(_sim(tmp_path, "a.json", tag), "only")
    if not covers:
        del cite["covers"]
    entry = _item(_report([{"id": "ratio", "extent": ["only"]}], cite))
    assert entry["reason"] == "partial_coverage"
    assert entry["row_coverage"]["row_slices"]["ratio"] == {
        "covered": [],
        "missing": ["only"],
    }


@pytest.mark.parametrize(
    "extent,covers",
    [
        ([], {"ratio": ["a"]}),
        ("a", {"ratio": ["a"]}),
        ([None], {}),
        ([""], {}),
        (["a"], {"ratio": ["b"]}),
        (["a"], {"other": ["a"]}),
        (["a"], {"ratio": [1]}),
        (["a"], None),
        (["a"], {"ratio": "a"}),
        (None, {"ratio": ["a"]}),
    ],
)
def test_invalid_declaration_isolated_to_the_item(tmp_path, extent, covers):
    row = {"id": "ratio", **({"extent": extent} if extent is not None else {})}
    report = _report(
        [row], {"file": _sim(tmp_path, "a.json"), "rows": ["ratio"], "covers": covers}
    )
    assert _item(report)["reason"] == "invalid_evidence"
    assert _item(report, 6)["reason"] == "no_evidence"


def test_single_citation_with_covers_and_no_spec_rows_is_invalid(tmp_path):
    report = build_tier_report(
        {"kind": "analog", "evidence": {"5": _cite(_sim(tmp_path, "a.json"), "a")}}
    )
    assert _item(report)["reason"] == "invalid_evidence"


def test_require_derived_rows_needs_tagged_row_credit_for_every_slice(tmp_path):
    a = _sim(tmp_path, "a.json")
    b = _sim(tmp_path, "b.json", None)
    row = {"id": "ratio", "extent": ["one", "two"]}
    for second_citation, expected in [
        (_cite(a, "two"), "met"),
        (_cite(a, "one"), "unmet"),
    ]:
        citations = [_cite(b, "one", "two"), _cite(a, "one"), second_citation]
        item = _item(_report([row], citations, require_derived_rows=True))
        assert item["status"] == expected
        if expected == "unmet":
            assert item["detail"]["asserted_rows"] == ["ratio"]
            assert item["detail"]["row_sources"] == {"ratio": "asserted"}


def test_gap_and_unratified_rows_keep_their_grading(tmp_path):
    rows = [
        {"id": "ratio", "extent": ["one"], "ratified": False},
        {"id": "future", "extent": ["two"], "gap": "pending"},
    ]
    entry = _item(_report(rows, _cite(_sim(tmp_path, "a.json"), "one")))
    assert entry["reason"] == "partial_coverage"
    assert entry["detail"]["unratified_rows"] == ["ratio"]
    assert entry["detail"]["declared_gaps"] == [{"id": "future", "gap": "pending"}]
    assert "partial_rows" not in entry["detail"]
    assert entry["row_coverage"]["row_slices"]["future"] == {
        "covered": [],
        "missing": ["two"],
    }


def test_legacy_row_coverage_shape_unchanged(tmp_path):
    cite = {"file": _sim(tmp_path, "a.json"), "rows": ["ratio"]}
    entry = _item(_report([{"id": "ratio"}], cite))
    assert entry["status"] == "met"
    assert entry["row_coverage"] == {
        "failed_rows": [],
        "uncovered_rows": [],
        "declared_gaps": [],
        "unratified_rows": [],
        "row_sources": {"ratio": "derived"},
    }


def test_partition_specific_declarations_use_the_selected_evidence(tmp_path):
    a = _sim(tmp_path, "a.json")
    manifest = {
        "kind": "mixed-signal",
        "spec_rows": {
            "5": [{"id": "ratio", "extent": ["analog"]}],
            "5.digital": [{"id": "ratio", "extent": ["first", "second"]}],
        },
        "evidence": {
            "5.analog": _cite(a, "analog"),
            "5.digital": _cite(a, "first"),
        },
    }
    items = {
        i["partition"]: i for i in build_tier_report(manifest)["items"] if i["id"] == 5
    }
    assert items["analog"]["status"] == "met"
    # Digital sim evidence also needs a timing-margin row. Its reason comes first.
    assert items["digital"]["reason"] == "timing_margin_missing"
    assert items["digital"]["row_coverage"]["row_slices"]["ratio"]["missing"] == [
        "second"
    ]


def test_empty_campaign_lists_the_whole_extent_in_declaration_order():
    entry = _item(_report([{"id": "ratio", "extent": ["z", "a", "z"]}], None))
    assert entry["reason"] == "partial_coverage"
    assert entry["detail"]["partial_rows"] == [
        {"id": "ratio", "extent": ["z", "a"], "covered": [], "missing": ["z", "a"]}
    ]
