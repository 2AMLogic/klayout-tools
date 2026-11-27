"""Item 7 grades the declared PEX corner box across independent citations."""

import json
import subprocess
from pathlib import Path

import pytest

from klayout_tools.cli import main
from klayout_tools.pex import _build_coverage, _status_from_coverage
from klayout_tools.signoff import build_tier_report
from test_signoff import (
    FUNCTIONAL_VERIFICATION_SDF_ENVELOPE,
    PEX_PASS_ENVELOPE,
    SIM_PASS_ENVELOPE,
    _manifest,
    _mixed_signal_manifest,
    _write,
)

CORNERS = ["tt/1.800V/27C", "ss/1.620V/125C"]


def row(corner, status="pass", name="gain", bench="amp"):
    return {
        "testbench": bench,
        "measurement": name,
        "spec_row": name,
        "corner_id": corner,
        "schematic_value": 2.0,
        "extracted_value": None if status in {"error", "skipped"} else 2.1,
        "extracted_status": status,
        "status": status,
    }


def report(tmp_path, name, rows, **changes):
    failed = sum(r["status"] == "fail" for r in rows)
    errored = sum(r["status"] == "error" for r in rows)
    corners = len({r["corner_id"] for r in rows})
    coverage = _build_coverage(testbenches_summary=[], delta=rows, corner_count=corners)
    return _write(
        tmp_path,
        name,
        {
            **PEX_PASS_ENVELOPE,
            "corner_count": corners,
            "passed": sum(r["status"] == "pass" for r in rows),
            "failed": failed,
            "errored": errored,
            "delta": rows,
            "coverage": coverage,
            "status": _status_from_coverage(
                coverage, failed=bool(failed), errored=bool(errored)
            ),
            **changes,
        },
    )


def item(entry, **changes):
    manifest = _manifest(evidence={"7": entry}, corner_box={"7": CORNERS})
    return next(
        i for i in build_tier_report({**manifest, **changes})["items"] if i["id"] == 7
    )


def test_partial_and_supplement_cover_box_through_cli(tmp_path, capsys):
    full = report(tmp_path, "full.json", [row(CORNERS[0]), row(CORNERS[1], "error")])
    supplement = report(tmp_path, "supplement.json", [row(CORNERS[1])])
    for citations, covered in (([full, supplement], 2), ([full], 1), (supplement, 1)):
        manifest = _write(
            tmp_path,
            "manifest.json",
            _manifest(evidence={"7": citations}, corner_box={"7": CORNERS}),
        )
        main(["signoff", "--manifest", manifest, "--format", "json"])
        result = next(
            i for i in json.loads(capsys.readouterr().out)["items"] if i["id"] == 7
        )
        assert result["detail"]["corner_box"]["covered"] == covered
        assert result["status"] == ("met" if covered == 2 else "unmet")
        if citations == [full, supplement]:
            assert [(g["status"], g["reason"]) for g in result["citations_graded"]] == [
                ("unmet", "check_failed"),
                ("met", None),
            ]
            assert len(result["citations"]) == 2
        else:
            assert result["reason"] == "partial_coverage"
            missing = CORNERS[1] if citations == [full] else CORNERS[0]
            assert result["detail"]["corner_box"]["missing"] == [
                {
                    "corner_id": missing,
                    "keys": [{"testbench": "amp", "measurement": "gain"}],
                }
            ]
        main(["signoff", "--manifest", manifest, "--format", "text"])
        out = capsys.readouterr().out
        assert f"corner box: {covered}/2 covered" in out
        if covered == 1:
            assert f"missing corner: {missing}" in out


@pytest.mark.parametrize("aggregate", ["error", "pass", "fail"])
def test_failure_is_never_superseded(tmp_path, aggregate):
    bad = report(
        tmp_path, "bad.json", [row(CORNERS[0], "fail")], status=aggregate, failed=0
    )
    good = report(tmp_path, "good.json", [row(c) for c in CORNERS])
    result = item([bad, good])
    assert result["reason"] == "check_failed"
    assert result["detail"]["failed_citations"] == [bad]


def test_every_key_needs_coverage_skipped_rows_are_inapplicable(tmp_path):
    data = [row(c) for c in [*CORNERS, "supplement-extra"]]
    data += [
        row(CORNERS[1], name="slew", bench=None),
        row(CORNERS[1], "skipped", name="probe"),
    ]
    path = report(tmp_path, "multi.json", data)
    result = item(path)
    assert result["detail"]["corner_box"] == {
        "declared": 2,
        "covered": 1,
        "missing": [
            {
                "corner_id": CORNERS[0],
                "keys": [{"testbench": None, "measurement": "slew"}],
            }
        ],
    }


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (
            {
                "coverage": {
                    "nothing_checked": True,
                    "nothing_checked_reasons": ["no_delta_rows"],
                }
            },
            "nothing_checked",
        ),
        (
            {"extraction": {"engine": "klayout-runset", "parasitics_carried": False}},
            "no_parasitics",
        ),
        ({"metrics": {"drc__error__count": 1}}, "check_failed"),
        ({"errored": 2}, "check_failed"),
        ({"error": {"message": "whole request aborted"}}, "check_errored"),
    ],
)
def test_independent_refusals_survive_row_credit(tmp_path, changes, reason):
    bad = report(
        tmp_path, "bad.json", [row(CORNERS[0]), row(CORNERS[1], "error")], **changes
    )
    good = report(tmp_path, "good.json", [row(c) for c in CORNERS])
    assert item([bad, good])["reason"] == reason


def test_stale_input_and_wrong_kind_remain_hard_failures(tmp_path):
    old = report(tmp_path, "old.json", [row(CORNERS[0]), row(CORNERS[1], "error")])
    assert item({"file": old, "content_hash": "0" * 64})["reason"] == "stale_evidence"
    sim = _write(tmp_path, "sim.json", SIM_PASS_ENVELOPE)
    assert item(sim)["reason"] == "wrong_kind"
    fv = _write(tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_SDF_ENVELOPE)
    assert item(fv, kind="digital")["reason"] == "wrong_kind"


@pytest.mark.parametrize(
    "box",
    [
        None,
        [],
        {},
        {"7": []},
        {"7": "tt"},
        {"7": [""]},
        {"7": [1]},
        {"7": ["tt", "tt"]},
        {"5": ["tt"]},
        {"7.adc": ["tt"]},
    ],
)
def test_invalid_declaration_is_local_to_item_7(box):
    baseline = build_tier_report(_manifest())["items"]
    actual = build_tier_report(_manifest(corner_box=box))["items"]
    for old, new in zip(baseline, actual, strict=True):
        if old["id"] == 7:
            assert new["reason"] == "invalid_evidence" and new["detail"]["problem"]
        else:
            assert new == old


def test_partition_override_and_undeclared_partition(tmp_path):
    one = report(tmp_path, "one.json", [row(CORNERS[0])])
    fv = _write(tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_SDF_ENVELOPE)
    manifest = _mixed_signal_manifest(evidence={"7.analog": one, "7.digital": fv})

    def partitions(**extras):
        return {
            i["partition"]: i
            for i in build_tier_report({**manifest, **extras})["items"]
            if i["id"] == 7
        }

    old = partitions()
    qualified = partitions(corner_box={"7.analog": CORNERS})
    assert qualified["analog"]["reason"] == "partial_coverage"
    assert qualified["digital"] == old["digital"]
    shared = partitions(corner_box={"7": CORNERS, "7.analog": CORNERS[:1]})
    assert shared["analog"]["status"] == "met"
    assert shared["digital"]["reason"] == "wrong_kind"


def test_no_identity_cannot_establish_a_corner_verdict(tmp_path):
    old = _write(tmp_path, "legacy.json", PEX_PASS_ENVELOPE)
    assert item(old)["reason"] == "invalid_evidence"
    assert item(old, corner_box={"7.digital": CORNERS})["status"] == "met"
    assert item(None)["detail"]["corner_box"]["covered"] == 0
    empty = report(tmp_path, "empty.json", [], status="pass", coverage={})
    assert item(empty)["detail"]["corner_box"]["covered"] == 0


def test_command_member_is_resolved_once(tmp_path, monkeypatch):
    full = report(tmp_path, "full.json", [row(CORNERS[0]), row(CORNERS[1], "error")])
    supplement = report(tmp_path, "supplement.json", [row(CORNERS[1])])
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, Path(full).read_text(), "")

    monkeypatch.setattr(subprocess, "run", run)
    command = ["klt", "pex", "example.gds"]
    result = item([{"command": command}, supplement])
    assert result["status"] == "met"
    assert calls == [command]


@pytest.mark.parametrize(
    "malformed",
    [
        {"status": "mystery"},
        {"corner_id": None},
        {"testbench": []},
        {"measurement": ""},
    ],
)
def test_malformed_delta_identity_is_an_item_refusal(tmp_path, malformed):
    source = report(tmp_path, "initial.json", [row(CORNERS[0])])
    payload = json.loads(Path(source).read_text())
    payload["delta"][0].update(malformed)
    broken = _write(tmp_path, "broken.json", payload)
    result = item(broken)
    assert result["reason"] == "invalid_evidence"
    assert result["detail"]["problem"]


def mixed_items(tmp_path, evidence, **extras):
    manifest = _mixed_signal_manifest(evidence=evidence, **extras)
    return {
        i["partition"]: i for i in build_tier_report(manifest)["items"] if i["id"] == 7
    }


def test_bare_box_names_itself_when_it_refuses_the_digital_partition(tmp_path):
    fv = _write(tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_SDF_ENVELOPE)
    evidence = {"7.digital": fv}

    bare = mixed_items(tmp_path, evidence, corner_box={"7": CORNERS})
    assert (bare["digital"]["status"], bare["digital"]["reason"]) == (
        "unmet",
        "wrong_kind",
    )
    problem = bare["digital"]["detail"]["problem"]
    assert "corner_box" in problem and '"7.analog"' in problem
    # `detail.corner_box` keeps its declared/covered/missing shape for graded items.
    assert "corner_box" not in bare["digital"]["detail"]

    scoped = mixed_items(tmp_path, evidence, corner_box={"7.analog": CORNERS})
    assert (scoped["digital"]["status"], scoped["digital"]["reason"]) == ("met", None)
    assert "detail" not in scoped["digital"]


@pytest.mark.parametrize(
    ("kind", "box"),
    [("mixed-signal", {"7.digital": CORNERS}), ("digital", {"7": CORNERS})],
)
def test_scoping_remedy_only_where_it_would_work(tmp_path, kind, box):
    # An explicit "7.digital" box, or a pure digital block, is not helped by
    # re-scoping the box to the analog partition.
    fv = _write(tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_SDF_ENVELOPE)
    manifest = (
        _mixed_signal_manifest(evidence={"7.digital": fv}, corner_box=box)
        if kind == "mixed-signal"
        else _manifest(kind=kind, evidence={"7": fv}, corner_box=box)
    )
    digital = next(
        i
        for i in build_tier_report(manifest)["items"]
        if i["id"] == 7 and i["partition"] in (None, "digital")
    )
    assert digital["reason"] == "wrong_kind"
    problem = digital["detail"]["problem"]
    assert "corner_box" in problem
    assert '"7.analog"' not in problem


@pytest.mark.parametrize("cited", ["absent", "null"])
def test_declared_box_with_nothing_cited_is_no_evidence(cited):
    evidence = {} if cited == "absent" else {"7": None}
    manifest = _manifest(evidence=evidence, corner_box={"7": CORNERS})
    result = next(i for i in build_tier_report(manifest)["items"] if i["id"] == 7)
    assert (result["status"], result["reason"]) == ("unmet", "no_evidence")
    assert result["citation"] is None
    assert result["detail"] == {
        "corner_box": {
            "declared": 2,
            "covered": 0,
            "missing": [{"corner_id": c, "keys": []} for c in CORNERS],
        }
    }
    baseline = next(
        i
        for i in build_tier_report(_manifest(evidence=evidence))["items"]
        if i["id"] == 7
    )
    # Apart from the summary, it is the row a manifest without a box produces.
    assert (baseline["status"], baseline["reason"]) == ("unmet", "no_evidence")
    assert {k: v for k, v in result.items() if k != "detail"} == baseline


def test_nothing_cited_text_output_names_the_missing_corners(tmp_path, capsys):
    manifest = _write(
        tmp_path, "manifest.json", _manifest(evidence={}, corner_box={"7": CORNERS})
    )
    main(["signoff", "--manifest", manifest, "--format", "text"])
    out = capsys.readouterr().out
    assert "corner box: 0/2 covered" in out
    assert f"missing corner: {CORNERS[0]}" in out


def test_present_entry_still_grades_through_the_box(tmp_path):
    one = report(tmp_path, "one.json", [row(CORNERS[0])])
    result = item(one)
    assert result["reason"] == "partial_coverage"
    assert result["detail"]["corner_box"]["covered"] == 1


def test_malformed_box_with_nothing_cited_still_reports_its_problem():
    manifest = _manifest(evidence={}, corner_box={"7": []})
    result = next(i for i in build_tier_report(manifest)["items"] if i["id"] == 7)
    assert result["reason"] == "invalid_evidence"
    assert result["detail"]["problem"]


def test_box_on_a_block_with_other_partition_scope_and_nothing_cited():
    # A "7.analog" box does not reach the digital partition: no box there, so
    # the digital row is the plain uncited result with no corner_box detail.
    result = mixed_items(None, {}, corner_box={"7.analog": CORNERS})
    assert result["analog"]["reason"] == "no_evidence"
    assert "corner_box" in result["analog"]["detail"]
    assert result["digital"]["reason"] == "no_evidence"
    assert "detail" not in result["digital"]
