"""Issue #6969: the ``joint_grid`` row in the per-spec-row table.

Split from ``tests/test_signoff_joint_rows.py``: the table record's
static projection of the joint evidence and its text rendering, over
the shared fixtures in ``tests/helpers/signoff_joint_fixtures.py``.
"""

from __future__ import annotations

from helpers.signoff_joint_fixtures import _grid_envelope, _joint_members, _write
from klayout_tools.cli.color import PLAIN
from klayout_tools.cli.signoff_cmd import _print_tier_report_text
from klayout_tools.signoff import build_tier_report


def test_the_table_record_carries_the_static_projection(tmp_path):
    a = _write(tmp_path, "a.json", _grid_envelope())
    b = _write(tmp_path, "b.json", _grid_envelope(drop=("ss", 1.62, 125)))

    report = build_tier_report(
        {
            "block": "demo-block",
            "kind": "analog",
            "evidence": {"5": [{"file": a}, {"file": b}]},
            "spec_rows": {
                "5": [{"id": "pvt_grid", "joint_grid": _joint_members(a, b)}]
            },
        }
    )

    (record,) = report["spec_rows"]
    assert record["state"] == "joint"
    assert record["sources"] == []
    assert record["limits"] is None and record["worst"] is None
    assert record["joint"]["covered"] is False
    assert record["joint"]["members"] == [
        {"member": a, "missing_points": [], "missing_probe": False},
        {"member": b, "missing_points": ["ss/1.620V/125C"], "missing_probe": False},
    ]


def test_the_table_marks_a_covered_projection(tmp_path):
    a = _write(tmp_path, "a.json", _grid_envelope())
    b = _write(tmp_path, "b.json", _grid_envelope())

    report = build_tier_report(
        {
            "block": "demo-block",
            "kind": "analog",
            "evidence": {"5": [{"file": a}, {"file": b}]},
            "spec_rows": {
                "5": [{"id": "pvt_grid", "joint_grid": _joint_members(a, b)}]
            },
        }
    )

    (record,) = report["spec_rows"]
    assert record["joint"]["covered"] is True
    assert all(
        m["missing_points"] == [] and m["missing_probe"] is False
        for m in record["joint"]["members"]
    )


def test_the_table_marks_an_unreadable_member_unevaluable(tmp_path):
    a = _write(tmp_path, "a.json", _grid_envelope())
    missing = str(tmp_path / "missing.json")

    report = build_tier_report(
        {
            "block": "demo-block",
            "kind": "analog",
            "evidence": {"5": [{"file": a}, {"file": missing}]},
            "spec_rows": {
                "5": [{"id": "pvt_grid", "joint_grid": _joint_members(a, missing)}]
            },
        }
    )

    (record,) = report["spec_rows"]
    assert record["joint"]["covered"] is False
    assert record["joint"]["members"][1] == {
        "member": missing,
        "missing_points": None,
        "missing_probe": None,
    }


def test_the_text_table_lists_the_per_member_shortfall(tmp_path, capsys):
    a = _write(tmp_path, "a.json", _grid_envelope())
    b = _write(tmp_path, "b.json", _grid_envelope(drop=("ff", 1.98, -40)))
    report = build_tier_report(
        {
            "block": "demo-block",
            "kind": "analog",
            "evidence": {"5": [{"file": a}, {"file": b}]},
            "spec_rows": {
                "5": [{"id": "pvt_grid", "joint_grid": _joint_members(a, b)}]
            },
        }
    )

    _print_tier_report_text(report, PLAIN)
    out = capsys.readouterr().out
    assert "[JOINT] pvt_grid: 2 member(s), shortfall" in out
    assert f"member {b}: missing ff/1.980V/-40C" in out
    # A clean member has no line of its own.
    assert f"member {a}" not in out
