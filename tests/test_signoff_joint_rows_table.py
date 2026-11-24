"""Issue #6969: the ``joint_grid`` row in the per-spec-row table.

Split from ``tests/test_signoff_joint_rows.py``: the table record's
static projection of the joint evidence and its text rendering, over
the shared fixtures in ``tests/helpers/signoff_joint_fixtures.py``.
"""

from __future__ import annotations

import json

from helpers.signoff_joint_fixtures import _grid_envelope, _joint_members, _write
from helpers.subprocess_fakes import fake_completed
from klayout_tools import signoff as signoff_module
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
    # Issue #7701: no readable shortfall, one member the projection cannot
    # read -- "unknown", never "not covered".
    assert record["joint"]["covered"] is None
    assert record["joint"]["members"][1] == {
        "member": missing,
        "missing_points": None,
        "missing_probe": None,
    }


def _joint_report(tmp_path, *members, evidence=None):
    """A one-row item-5 report whose ``joint_grid`` names ``members``."""
    return build_tier_report(
        {
            "block": "demo-block",
            "kind": "analog",
            "evidence": {"5": evidence or [{"file": m} for m in members]},
            "spec_rows": {
                "5": [{"id": "pvt_grid", "joint_grid": _joint_members(*members)}]
            },
        }
    )


def test_a_readable_shortfall_outranks_an_unevaluable_member(tmp_path):
    short = _write(tmp_path, "a.json", _grid_envelope(drop=("ss", 1.62, 125)))
    missing = str(tmp_path / "missing.json")

    (record,) = _joint_report(tmp_path, short, missing)["spec_rows"]

    # One member definitely falls short, so the row is definitely not covered
    # whatever the unreadable member would have shown.
    assert record["joint"]["covered"] is False


def test_all_unevaluable_members_read_unknown(tmp_path):
    first = str(tmp_path / "first.json")
    second = str(tmp_path / "second.json")

    (record,) = _joint_report(tmp_path, first, second)["spec_rows"]

    assert record["joint"]["covered"] is None
    assert [m["missing_points"] for m in record["joint"]["members"]] == [None, None]


def test_a_monte_carlo_member_reads_unknown_not_uncovered(tmp_path):
    a = _write(tmp_path, "a.json", _grid_envelope())
    mc = _write(tmp_path, "mc.json", _grid_envelope(monte_carlo=True))

    (record,) = _joint_report(tmp_path, a, mc)["spec_rows"]

    assert record["joint"]["covered"] is None
    assert record["joint"]["members"][1]["missing_points"] is None


def test_a_command_backed_member_credits_the_item_axis_without_reading_false(
    tmp_path, monkeypatch
):
    """The pairing issue #7701 flagged: a command-backed member ran `klt sim`
    and genuinely proved its half of the grid, so the item axis grades the
    row ``derived`` -- while the table cannot re-read it. The table's
    ``covered`` must read unknown, not the ``false`` that contradicts the
    item axis."""
    a = _write(tmp_path, "a.json", _grid_envelope())
    command = ["klt", "sim", "bench_b.json"]
    monkeypatch.setattr(
        signoff_module.subprocess,
        "run",
        lambda argv, **kwargs: fake_completed(stdout=json.dumps(_grid_envelope())),
    )

    report = build_tier_report(
        {
            "block": "demo-block",
            "kind": "analog",
            "evidence": {"5": [{"file": a}, {"command": command}]},
            "spec_rows": {
                "5": [
                    {
                        "id": "pvt_grid",
                        "joint_grid": _joint_members(a, "klt sim bench_b.json"),
                    }
                ]
            },
        }
    )

    item = next(i for i in report["items"] if i["id"] == 5)
    assert item["status"] == "met"
    assert item["row_coverage"]["row_sources"] == {"pvt_grid": "derived"}
    (record,) = report["spec_rows"]
    assert record["joint"]["covered"] is None
    assert record["joint"]["members"] == [
        {"member": a, "missing_points": [], "missing_probe": False},
        {
            "member": "klt sim bench_b.json",
            "missing_points": None,
            "missing_probe": None,
        },
    ]


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


def test_the_text_table_says_unevaluable_not_shortfall(tmp_path, capsys):
    a = _write(tmp_path, "a.json", _grid_envelope())
    missing = str(tmp_path / "missing.json")

    _print_tier_report_text(_joint_report(tmp_path, a, missing), PLAIN)

    out = capsys.readouterr().out
    assert "[JOINT] pvt_grid: 2 member(s), not evaluable from the report alone" in out
    assert "shortfall" not in out
    assert f"member {missing}: not evaluable from the report alone" in out


def test_the_text_table_keeps_shortfall_beside_an_unevaluable_member(tmp_path, capsys):
    short = _write(tmp_path, "a.json", _grid_envelope(drop=("ff", 1.98, -40)))
    missing = str(tmp_path / "missing.json")

    _print_tier_report_text(_joint_report(tmp_path, short, missing), PLAIN)

    out = capsys.readouterr().out
    assert "[JOINT] pvt_grid: 2 member(s), shortfall" in out
