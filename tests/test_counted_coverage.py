"""Counted coverage across the schema, producers, and report readers."""

import json
from pathlib import Path

import jsonschema
import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.cli.drc_text import _print_coverage_gaps
from klayout_tools.cli.erc_cmd import _print_skipped_coverage
from klayout_tools.coverage import (
    build_check_coverage,
    coverage_rollup,
    coverage_validation_error,
    work_id,
)
from klayout_tools.erc import run_erc
from klayout_tools.signoff import build_signoff, build_tier_report
from test_erc import (
    _antenna_fixture,
    _basic_fixture,
    _full_stack_spec,
    _passive_gds,
    _passive_spec,
    _um,
    _write_spec,
)
from test_signoff import DRC_CLEAN_ENVELOPE, _item, _manifest, _write


@pytest.fixture(scope="module")
def validator():
    schema = Path(__file__).parents[1] / "docs/schemas/coverage.schema.json"
    return jsonschema.Draft202012Validator(json.loads(schema.read_text()))


@pytest.mark.parametrize("field", ["skipped", "inapplicable"])
@pytest.mark.parametrize(
    ("count", "valid"),
    [
        (1, True),
        (696, True),
        (2.0, True),
        (True, False),
        (False, False),
        (1.5, False),
        (0, False),
        (-1, False),
        ("2", False),
        (None, False),
    ],
)
def test_count_validation_matches_schema(validator, field, count, valid):
    block = build_check_coverage(checked=["checked"])
    block[field] = [{"id": "group", "reason": "missing_limit", "count": count}]
    assert validator.is_valid(block) is valid
    assert (coverage_validation_error(block) is None) is valid


def test_count_is_unavailable_for_unknown_or_checked(validator):
    unknown = {"id": "deck", "reason": "unmeasured"}
    block = build_check_coverage(checked=[], unknown=[unknown])
    assert validator.is_valid(block)
    for field in ("checked", "unknown"):
        changed = {**block, field: [{**unknown, "count": 2}]}
        assert not validator.is_valid(changed)
        assert coverage_validation_error(changed)


@pytest.mark.parametrize("field", ["skipped", "inapplicable"])
@pytest.mark.parametrize("other", ["checked", "skipped", "inapplicable", "unknown"])
def test_counted_ids_still_must_be_unique(field, other):
    first = {"id": "same", "reason": "missing_limit", "count": 2}
    second = {"id": "same", "reason": "other"}
    if field == other:
        second = {**first, "count": 3}
    kwargs = {name: [] for name in ("checked", "skipped", "inapplicable", "unknown")}
    kwargs[field].append(first)
    kwargs[other].append("same" if other == "checked" else second)
    with pytest.raises(ValueError, match="identit"):
        build_check_coverage(**kwargs)


@pytest.mark.parametrize(
    ("checked", "has_skip", "expected"),
    [
        (["checked"], True, "partial"),
        ([], True, "zero"),
        (["checked"], False, "full"),
        ([], False, "zero"),
    ],
)
def test_counted_and_unit_rollups_are_equivalent(
    validator, checked, has_skip, expected
):
    counted = {"id": "group", "reason": "missing_limit", "count": 3}
    unit = {"id": "unit", "reason": "missing_limit"}
    reference = {"id": "refs", "reason": "reference", "count": 3, "missing": ["layers"]}
    block = build_check_coverage(
        checked=checked,
        skipped=[counted, unit] if has_skip else [],
        inapplicable=[reference],
    )
    assert validator.is_valid(block)
    legacy = {
        **block,
        **{
            field: [
                {key: value for key, value in record.items() if key != "count"}
                for record in block[field]
            ]
            for field in ("skipped", "inapplicable")
        },
    }
    assert validator.is_valid(legacy)
    assert coverage_rollup({"coverage": block}).result == expected
    assert coverage_rollup({"coverage": legacy}) == coverage_rollup({"coverage": block})


@pytest.mark.parametrize("unit_records", [0, 21])
def test_signoff_weights_full_skipped_list(tmp_path, unit_records):
    group = {"id": "z_group", "reason": "missing_limit", "count": 696}
    units = [
        {"id": f"unit_{index:02}", "reason": "missing_limit" if index == 0 else "other"}
        for index in range(unit_records)
    ]
    block = build_check_coverage(checked=["rule"], skipped=[group, *units])
    path = _write(tmp_path, "drc.json", {**DRC_CLEAN_ENVELOPE, "coverage": block})
    check = build_signoff([path])["checks"][0]
    item = _item(build_tier_report(_manifest(evidence={"3": path})), 3)
    disclosure = check["detail"]["coverage_qualification"]
    assert check["passed"] is True
    assert item["status"] == "met"
    assert item["citation"]["coverage_qualification"] == disclosure
    assert disclosure["skipped_total"] == 696 + unit_records
    assert disclosure["skipped"] == block["skipped"][:20]
    assert disclosure.get("truncated", False) is bool(unit_records)
    expected = {"missing_limit": 696}
    if unit_records:
        expected = {"missing_limit": 697, "other": unit_records - 1}
    assert disclosure["skipped_by_reason"] == expected


def test_text_reports_weight_requested_skips(capsys):
    skipped = [
        {"id": "group", "reason": "missing_limit", "count": 696},
        {"id": "unit", "reason": "missing_limit"},
    ]
    _print_skipped_coverage({"erc_coverage": {"skipped": skipped}})
    assert "erc_skipped: 697 " in capsys.readouterr().out
    _print_coverage_gaps(
        {
            "skipped": [
                *skipped,
                {"id": "subset", "reason": "not_selected", "count": 3},
            ],
            "layers_in_stream_without_rules": [],
        }
    )
    assert "skipped requested rules: 697\n" in capsys.readouterr().out


@pytest.mark.parametrize("gate_count", [1, 3])
def test_graded_antenna_report_counts_and_signoff_agree(tmp_path, capsys, gate_count):
    gds = tmp_path / "full.gds"
    spec = tmp_path / "full.erc.json"
    _full_stack_spec(spec)
    _antenna_fixture(gds, li1_um2=1.0, met1_um2=1.0, met2_um2=1.0)
    if gate_count > 1:
        layout = kdb.Layout()
        layout.read(str(gds))
        gate_cell = layout.top_cell()
        top = layout.create_cell("ARRAY_TOP")
        for index in range(gate_count):
            top.insert(
                kdb.CellInstArray(gate_cell.cell_index(), kdb.Trans(_um(index * 10), 0))
            )
        layout.write(str(gds))

    assert (
        main(["erc", str(gds), str(spec), "--pdk", "sky130", "--format", "json"]) == 0
    )
    report = json.loads(capsys.readouterr().out)
    block = report["coverage"]
    gates = report["gates"]
    assert len(gates) == report["gate_count"] == gate_count
    assert report["status"] == "clean_partial"
    assert all(gate["antenna_verdict"] == "pass_partial" for gate in gates)
    checked = []
    skipped = []
    inapplicable = []
    for gate in gates:
        for index, level in enumerate(gate["levels"]):
            identity = work_id("antenna", gate["gate_id"], level["layer"])
            if index == 0:
                inapplicable.append({"id": identity, "reason": "gate_reference_level"})
            elif level["verdict"] == "pass":
                checked.append(identity)
            else:
                assert level["verdict"] == "unchecked"
                skipped.append({"id": identity, "reason": "missing_antenna_limit"})
    enumerated = build_check_coverage(
        checked=checked, skipped=skipped, inapplicable=inapplicable
    )
    assert coverage_rollup(report) == coverage_rollup({"coverage": enumerated})
    assert block["checked"] == enumerated["checked"]
    assert len(block["skipped"]) == len(block["inapplicable"]) == 1
    assert block["skipped"][0]["count"] == len(skipped) == 3 * gate_count
    assert block["inapplicable"][0]["count"] == len(inapplicable) == gate_count
    assert block["inapplicable"][0]["reason"] == "gate_reference_level"

    counted_path = _write(tmp_path, "counted-erc.json", report)
    legacy_path = _write(
        tmp_path, "enumerated-erc.json", {**report, "coverage": enumerated}
    )
    counted_check, legacy_check = build_signoff([counted_path, legacy_path])["checks"]
    assert counted_check["kind"] == legacy_check["kind"] == "erc"
    assert counted_check["passed"] == legacy_check["passed"]
    disclosure = counted_check["detail"]["coverage_qualification"]
    assert disclosure["skipped_total"] == len(skipped)
    assert disclosure["skipped_by_reason"] == {"missing_antenna_limit": len(skipped)}
    assert not disclosure.get("truncated")


@pytest.mark.parametrize(
    "options",
    [
        {"findings_only": True, "compact": True},
        {"findings_only": True, "omit_antenna_tables": True},
        {"omit_antenna_tables": True},
        {"findings_only": True, "compact": True, "omit_antenna_tables": True},
    ],
)
def test_collapsed_modes_count_the_original_work(tmp_path, options):
    gds = tmp_path / "full.gds"
    spec = tmp_path / "full.erc.json"
    _basic_fixture(gds)
    _full_stack_spec(spec)
    plain = run_erc(
        str(gds), str(spec), findings_only=options.get("findings_only", False)
    )
    collapsed = run_erc(str(gds), str(spec), **options)
    assert collapsed["gates"] == []
    for field in ("skipped", "inapplicable"):
        records = collapsed["coverage"][field]
        assert len(records) == 1
        assert records[0]["count"] == len(plain["coverage"][field])
        assert {records[0]["reason"]} == {r["reason"] for r in plain["coverage"][field]}


@pytest.mark.parametrize(
    "options", [{}, {"compact": True}, {"omit_antenna_tables": True}]
)
def test_no_gate_sentinel_is_a_unit_record(tmp_path, options):
    gds = _passive_gds(tmp_path)
    spec = tmp_path / "passive.erc.json"
    _write_spec(spec, _passive_spec(nets=[{"name": "VDD", "kind": "supply"}]))
    report = run_erc(str(gds), str(spec), findings_only=True, **options)
    assert report["gate_count"] == 0
    block = report["coverage"]
    assert block["skipped"] == []
    assert block["inapplicable"] == [{"id": work_id("antenna"), "reason": "no_gates"}]
