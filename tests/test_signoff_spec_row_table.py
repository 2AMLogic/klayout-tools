"""Issue #5691: the per-spec-row table on ``klt signoff --manifest`` reports.

When a manifest declares a top-level ``spec_rows`` map, the tier report
gains one additive top-level ``spec_rows[]`` block: one record per declared
row, quoting each row's limit, worst value, binding corner, verdict and
yield estimate off the cited envelopes, with the cited file behind each
number. A manifest that declares nothing omits the key entirely, so
``--check`` reports no drift on an upgrade alone. The grading rules these
records quote are covered in ``test_signoff.py`` /
``test_signoff_derived_rows.py``; nothing here changes an item verdict.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools.cli.color import PLAIN
from klayout_tools.cli.signoff_cmd import _print_tier_report_text
from klayout_tools.signoff import build_tier_report

_LIM = {"min": 1.0}

SIM_PASS_ENVELOPE = {
    "schema_version": 1,
    "status": "pass",
    "corner_count": 3,
    "measurements": [],
}

YIELD_PASS_ENVELOPE = {
    "schema_version": 1,
    "status": "pass",
    "measurement_count": 1,
    "source": {"spec": "spec-limits.json"},
    "measurements": [],
}


def _write(tmp_path, name, payload) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return str(path)


def _sim(measurements: list[dict]) -> dict:
    return {**SIM_PASS_ENVELOPE, "measurements": measurements}


def _yield(measurements: list[dict]) -> dict:
    return {**YIELD_PASS_ENVELOPE, "measurements": measurements}


def _meas(
    name,
    spec_row=None,
    *,
    limits=None,
    status="pass",
    unit=None,
    worst_case=None,
    empirical=None,
) -> dict:
    entry: dict = {"name": name, "status": status}
    if limits is not None:
        entry["limits"] = limits
    if unit is not None:
        entry["unit"] = unit
    if spec_row is not None:
        entry["spec_row"] = spec_row
    if worst_case is not None:
        entry["worst_case"] = worst_case
    if empirical is not None:
        entry["yield"] = {"empirical": empirical}
    return entry


def _manifest(evidence, spec_rows=None, **overrides) -> dict:
    manifest = {"block": "demo-block", "kind": "analog", "evidence": evidence}
    if spec_rows is not None:
        manifest["spec_rows"] = spec_rows
    manifest.update(overrides)
    return manifest


def _records(manifest) -> list[dict]:
    return build_tier_report(manifest)["spec_rows"]


def _cite(path, rows) -> dict:
    return {"file": path, "rows": rows}


def _tagged_row_row(tmp_path, name, **kwargs) -> str:
    """A one-measurement `klt sim` envelope tagged for row ``"row"``."""
    return _write(tmp_path, name, _sim([_meas("m", spec_row="row", **kwargs)]))


# ---------------------------------------------------------------------------
# Presence / absence and declaration order
# ---------------------------------------------------------------------------


def test_key_absent_when_manifest_declares_no_spec_rows(tmp_path):
    a = _write(tmp_path, "a.json", _sim([_meas("t", limits=_LIM)]))
    assert "spec_rows" not in build_tier_report(_manifest({"5": a}))


def test_empty_or_malformed_spec_rows_map_emits_no_block(tmp_path):
    a = _write(tmp_path, "a.json", _sim([]))
    assert "spec_rows" not in build_tier_report(_manifest({"5": a}, spec_rows={}))
    assert "spec_rows" not in build_tier_report(
        _manifest({"5": a}, spec_rows={"5": "nope"})
    )


def test_one_record_per_declared_row_in_declaration_order(tmp_path):
    a = _write(tmp_path, "a.json", _sim([_meas("t", spec_row="t_dec", limits=_LIM)]))
    records = _records(
        _manifest(
            {"5": _cite(a, ["t_dec", "z_in", "noise_mc"])},
            spec_rows={
                "5": [
                    {"id": "t_dec"},
                    {"id": "z_in"},
                    {"id": "noise_mc", "gap": "statistical"},
                ]
            },
        )
    )
    assert [r["id"] for r in records] == ["t_dec", "z_in", "noise_mc"]
    assert [r["state"] for r in records] == ["graded", "no_evidence", "gap"]
    assert [r["item"] for r in records] == [5, 5, 5]


def test_partition_qualified_slot_carries_partition_bare_does_not(tmp_path):
    a = _write(tmp_path, "a.json", _sim([_meas("t", spec_row="t_dec", limits=_LIM)]))
    qualified = _records(
        _manifest(
            {"5.analog": _cite(a, ["t_dec"])},
            spec_rows={"5.analog": [{"id": "t_dec"}]},
            kind="mixed-signal",
            partition_boundary={"analog": "core", "digital": "trim"},
        )
    )
    assert len(qualified) == 1
    assert qualified[0]["partition"] == "analog"
    (bare,) = _records(
        _manifest({"5": _cite(a, ["t_dec"])}, spec_rows={"5": [{"id": "t_dec"}]})
    )
    assert "partition" not in bare


# ---------------------------------------------------------------------------
# The reduce: minimum relative margin, precedence, fallbacks
# ---------------------------------------------------------------------------


def test_reduce_picks_minimum_relative_margin_not_absolute(tmp_path):
    # Supply-scaled instances: the 1V-rail measurement has the smaller
    # absolute margin but the larger relative margin -- it must bind.
    high = _meas(
        "t_dec@1v8",
        spec_row="t_dec",
        limits={"max": 1.2e-6},
        unit="s",
        worst_case={"corner_id": "ss/1.620V/-40C", "value": 1.18e-6, "margin": 2.0e-8},
    )
    low = _meas(
        "t_dec@1v0",
        spec_row="t_dec",
        limits={"max": 5.0e-7},
        unit="s",
        worst_case={"corner_id": "ss/1.0V/-40C", "value": 4.99e-7, "margin": 1.0e-9},
    )
    a = _write(tmp_path, "a.json", _sim([high, low]))
    b = _write(tmp_path, "b.json", _sim([dict(low, name="t_dec@1v0-b")]))
    (record,) = _records(
        _manifest(
            {"5": [_cite(a, ["t_dec"]), _cite(b, ["t_dec"])]},
            spec_rows={"5": [{"id": "t_dec"}]},
        )
    )
    assert record["state"] == "graded"
    # 1.0e-9 / 5.0e-7 = 0.002 beats 2.0e-8 / 1.2e-6 ~= 0.0167.
    assert record["worst"]["corner_id"] == "ss/1.0V/-40C"
    assert record["worst"]["relative_margin"] == pytest.approx(0.002)
    assert {s["file"] for s in record["sources"]} == {a, b}


def test_relative_margin_null_without_computable_bound(tmp_path):
    # Active bound 0 (min 0.0 reproduces the margin): undefined quotient.
    zero = _meas(
        "vlim",
        spec_row="vlim",
        limits={"min": 0.0},
        worst_case={"corner_id": "tt", "value": 0.001, "margin": 0.001},
    )
    a = _write(tmp_path, "zero.json", _sim([zero]))
    (record,) = _records(
        _manifest({"5": _cite(a, ["vlim"])}, spec_rows={"5": [{"id": "vlim"}]})
    )
    assert record["worst"]["relative_margin"] is None
    assert record["worst"]["margin"] == 0.001

    # No limits at all: no margin anywhere, declaration order breaks the
    # tie, and the row carries no fabricated verdict or unit.
    wide = _meas("a", spec_row="row", worst_case={"corner_id": "tt", "value": 1.0})
    tight = _meas("b", spec_row="row", worst_case={"corner_id": "ss", "value": 2.0})
    b = _write(tmp_path, "wide.json", _sim([wide, tight]))
    (record,) = _records(
        _manifest({"5": _cite(b, ["row"])}, spec_rows={"5": [{"id": "row"}]})
    )
    assert record["verdict"] is None
    assert record["limits"] is None
    assert record["unit"] is None
    assert record["worst"]["relative_margin"] is None
    assert record["worst"]["corner_id"] == "tt"


def test_non_pass_outranks_pass_regardless_of_margin(tmp_path):
    failing = _meas(
        "f",
        spec_row="row",
        limits={"max": 1.0},
        status="fail",
        worst_case={"corner_id": "ff", "value": 1.9, "margin": -0.9},
    )
    passing = _meas(
        "p",
        spec_row="row",
        limits={"max": 1.0},
        status="pass",
        worst_case={"corner_id": "ss", "value": 0.999, "margin": 0.001},
    )
    inconclusive = _meas("i", spec_row="row2", limits=_LIM, status="inconclusive")
    plain_pass = _meas("p2", spec_row="row2", limits=_LIM, status="pass")
    a = _write(tmp_path, "a.json", _sim([passing, failing, plain_pass, inconclusive]))
    records = _records(
        _manifest(
            {"5": _cite(a, ["row", "row2"])},
            spec_rows={"5": [{"id": "row"}, {"id": "row2"}]},
        )
    )
    assert records[0]["verdict"] == "fail"
    assert records[0]["worst"]["corner_id"] == "ff"
    assert records[1]["verdict"] == "inconclusive"


# ---------------------------------------------------------------------------
# Quoted, never re-derived
# ---------------------------------------------------------------------------


def test_worst_and_yield_quoted_byte_equal_from_fixtures(tmp_path):
    worst_case = {"corner_id": "ss/1.620V/-40C", "value": 1.18e-6, "margin": 2.0e-8}
    empirical = {
        "method": "clopper-pearson",
        "estimate": 0.9993,
        "confidence": 0.9,
        "confidence_interval": {"low": 0.9961, "high": 0.9999},
    }
    sim_meas = _meas(
        "t_dec",
        spec_row="t_dec",
        limits={"max": 1.2e-6},
        unit="s",
        worst_case=worst_case,
    )
    yield_meas = _meas(
        "t_dec_yield", spec_row="t_dec", limits={"max": 1.2e-6}, empirical=empirical
    )
    a = _write(tmp_path, "sim.json", _sim([sim_meas]))
    b = _write(tmp_path, "yield.json", _yield([yield_meas]))
    records = _records(
        _manifest(
            {"5": _cite(a, ["t_dec"]), "6": _cite(b, ["t_dec"])},
            spec_rows={"5": [{"id": "t_dec"}], "6": [{"id": "t_dec"}]},
        )
    )
    # Two slots declared the same row id: both render, in declaration order.
    assert [r["item"] for r in records] == [5, 6]
    assert records[0]["sources"] == [{"file": a, "measurement": "t_dec"}]
    assert records[1]["sources"] == [{"file": b, "measurement": "t_dec_yield"}]
    # The item-5 record quotes the sim envelope's worst_case (plus the
    # relative-margin quotient that picked it); the item-6 record quotes
    # the yield envelope's empirical block verbatim.
    assert records[0]["worst"] == {**worst_case, "relative_margin": 2.0e-8 / 1.2e-6}
    assert records[0]["yield"] is None
    assert records[1]["worst"] is None
    assert records[1]["yield"] == empirical


def test_row_cited_by_sim_and_yield_in_one_slot(tmp_path):
    worst_case = {"corner_id": "ss/1.620V/-40C", "value": 1.18e-6, "margin": 2.0e-8}
    empirical = {"method": "clopper-pearson", "estimate": 0.999}
    sim_meas = _meas(
        "t_dec",
        spec_row="t_dec",
        limits={"max": 1.2e-6},
        unit="s",
        worst_case=worst_case,
    )
    yield_meas = _meas(
        "t_dec_yield", spec_row="t_dec", limits={"max": 1.2e-6}, empirical=empirical
    )
    a = _write(tmp_path, "sim.json", _sim([sim_meas]))
    b = _write(tmp_path, "yield.json", _yield([yield_meas]))
    (record,) = _records(
        _manifest(
            {"5": [_cite(a, ["t_dec"]), _cite(b, ["t_dec"])]},
            spec_rows={"5": [{"id": "t_dec"}]},
        )
    )
    # The sim measurement binds (worst/corner); the yield estimate still
    # comes along from its own envelope.
    assert record["worst"] == {**worst_case, "relative_margin": 2.0e-8 / 1.2e-6}
    assert record["unit"] == "s"
    assert record["yield"] == empirical


def test_yield_measurement_without_yield_block(tmp_path):
    a = _write(tmp_path, "a.json", _yield([_meas("m", spec_row="row", limits=_LIM)]))
    (record,) = _records(
        _manifest({"6": _cite(a, ["row"])}, spec_rows={"6": [{"id": "row"}]})
    )
    assert record["state"] == "graded"
    assert record["yield"] is None
    assert record["worst"] is None


# ---------------------------------------------------------------------------
# limit_disagreement
# ---------------------------------------------------------------------------


def test_limit_disagreement_listed_not_reconciled(tmp_path):
    # m1's relative margin (2e-7/1.2e-6) loses to m2's (5e-8/1.3e-6): m2
    # binds, m1's different limits are disclosed, never reconciled.
    first = _meas(
        "m1",
        spec_row="row",
        limits={"max": 1.2e-6},
        worst_case={"corner_id": "ss", "value": 1.0e-6, "margin": 2.0e-7},
    )
    second = _meas(
        "m2",
        spec_row="row",
        limits={"max": 1.3e-6},
        worst_case={"corner_id": "tt", "value": 1.25e-6, "margin": 5.0e-8},
    )
    agreeing = _meas(
        "m3",
        spec_row="row",
        limits={"max": 1.3e-6},
        worst_case={"corner_id": "ff", "value": 1.29e-6, "margin": 1.0e-8},
    )
    a = _write(tmp_path, "a.json", _sim([first]))
    b = _write(tmp_path, "b.json", _sim([second, agreeing]))
    (record,) = _records(
        _manifest(
            {"5": [_cite(a, ["row"]), _cite(b, ["row"])]},
            spec_rows={"5": [{"id": "row"}]},
        )
    )
    assert record["state"] == "graded"
    assert record["worst"]["corner_id"] == "ff"  # 1e-8/1.3e-6: m3 binds
    assert record["limits"] == {"max": 1.3e-6}
    assert record["limit_disagreement"] == [
        {"file": a, "measurement": "m1", "limits": {"max": 1.2e-6}}
    ]


# ---------------------------------------------------------------------------
# States and attribution edges
# ---------------------------------------------------------------------------


def test_gap_row_renders_even_when_cited(tmp_path):
    a = _tagged_row_row(tmp_path, "a.json", limits=_LIM)
    (record,) = _records(
        _manifest(
            {"5": _cite(a, ["row"])},
            spec_rows={"5": [{"id": "row", "gap": "not expressible"}]},
        )
    )
    assert record["state"] == "gap"
    assert record["gap"] == "not expressible"
    assert record["limits"] is None
    assert record["sources"] == []


def test_untagged_or_empty_envelope_is_unattributable(tmp_path):
    untagged = _write(tmp_path, "u.json", _sim([_meas("m", limits=_LIM)]))
    empty = _write(tmp_path, "e.json", _sim([]))
    first = _records(
        _manifest({"5": _cite(untagged, ["row"])}, spec_rows={"5": [{"id": "row"}]})
    )
    second = _records(
        _manifest({"5": _cite(empty, ["row"])}, spec_rows={"5": [{"id": "row"}]})
    )
    assert first[0]["state"] == second[0]["state"] == "unattributable"
    assert first[0]["sources"] == []


def test_tag_for_undeclared_row_is_ignored(tmp_path):
    tagged = _meas("m", spec_row="other_row", limits=_LIM)
    a = _write(tmp_path, "a.json", _sim([tagged]))
    (record,) = _records(
        _manifest({"5": _cite(a, ["row"])}, spec_rows={"5": [{"id": "row"}]})
    )
    # The envelope is tagged, but never for *this* row: nothing
    # attributable remains, so the state is no_evidence (not
    # unattributable).
    assert record["state"] == "no_evidence"


def test_status_absent_with_null_value_renders_verdict_null(tmp_path):
    absent = _meas(
        "m",
        spec_row="row",
        limits={"max": 1.0},
        status="absent",
        worst_case={"corner_id": "tt", "value": None, "margin": None},
    )
    a = _write(tmp_path, "a.json", _sim([absent]))
    (record,) = _records(
        _manifest({"5": _cite(a, ["row"])}, spec_rows={"5": [{"id": "row"}]})
    )
    assert record["state"] == "graded"
    assert record["verdict"] == "absent"
    assert record["worst"]["value"] is None
    assert record["worst"]["relative_margin"] is None


def test_command_backed_citation_contributes_nothing(tmp_path):
    manifest = _manifest(
        {"5": {"command": ["false"], "rows": ["row"]}},
        spec_rows={"5": [{"id": "row"}]},
    )
    (record,) = _records(manifest)
    assert record["state"] == "no_evidence"


# ---------------------------------------------------------------------------
# Back-compat and rendering
# ---------------------------------------------------------------------------


def test_example_manifest_declares_no_spec_rows_and_renders_no_block():
    manifest_path = (
        Path(__file__).resolve().parents[1] / "examples" / "signoff" / "manifest.json"
    )
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert "spec_rows" not in manifest
    # Evidence dropped so the report builds hermetically; the block gate is
    # the declaration, not the citations.
    manifest.pop("evidence", None)
    assert "spec_rows" not in build_tier_report(manifest)


def test_text_renderer_prints_table_and_stays_silent_without_block(tmp_path, capsys):
    meas = _meas(
        "t_dec",
        spec_row="t_dec",
        limits={"max": 1.2e-6},
        unit="s",
        worst_case={"corner_id": "ss/1.620V/-40C", "value": 1.18e-6, "margin": 2.0e-8},
    )
    a = _write(tmp_path, "a.json", _sim([meas]))
    result = build_tier_report(
        _manifest({"5": _cite(a, ["t_dec"])}, spec_rows={"5": [{"id": "t_dec"}]})
    )
    _print_tier_report_text(result, PLAIN)
    out = capsys.readouterr().out
    assert "spec rows (1):" in out
    assert "t_dec" in out
    assert "from: " in out

    plain = build_tier_report(_manifest({"5": a}))
    _print_tier_report_text(plain, PLAIN)
    assert "spec rows (" not in capsys.readouterr().out
