"""Tests for `klt trajectory` and the `trajectory` library module (issue #388).

The renderer is exercised entirely on hand-written JSONL fixtures -- the whole
point of the feature is that a trajectory log renders without a live
optimizer, so every fixture here is a plain string written to a temp file,
mirroring `tests/test_drc.py`'s "no external corpus" convention.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.trajectory import (
    SCHEMA_VERSION,
    TrajectoryError,
    append_record,
    build_trajectory,
    detect_milestones,
    read_log,
    record_from_eval,
    render_markdown_table,
    render_plot_svg,
)


def _record(
    turn: int,
    value: float,
    *,
    polarity: str = "minimize",
    name: str = "gates",
    ref: str | None = None,
    description: str = "",
    gate_results: list | None = None,
    wall_clock_s: float | None = None,
) -> dict:
    rec: dict = {
        "schema_version": SCHEMA_VERSION,
        "turn": turn,
        "candidate_ref": ref if ref is not None else f"cand/turn-{turn}.gds",
        "objective": {"name": name, "value": value, "polarity": polarity},
    }
    if description:
        rec["description"] = description
    if gate_results is not None:
        rec["gate_results"] = gate_results
    if wall_clock_s is not None:
        rec["wall_clock_s"] = wall_clock_s
    return rec


def _write_log(tmp_path, records: list[dict], name: str = "traj.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# read_log: round-trip + validation
# ---------------------------------------------------------------------------


def test_read_log_round_trip(tmp_path):
    records = [
        _record(
            0,
            8298,
            gate_results=[{"check": "drc", "status": "clean"}],
            wall_clock_s=12.5,
        ),
        _record(22, 2010, description="iterative shift-subtract"),
        _record(48, 1304),
    ]
    path = _write_log(tmp_path, records)

    got = read_log(str(path))

    assert [r["turn"] for r in got] == [0, 22, 48]
    assert got[0]["gate_results"] == [{"check": "drc", "status": "clean"}]
    assert got[0]["wall_clock_s"] == 12.5
    assert got[1]["objective"]["value"] == 2010


def test_read_log_sorts_out_of_order_appends(tmp_path):
    # A parallel loop can append turns as they finish, out of order.
    path = _write_log(
        tmp_path, [_record(48, 1304), _record(0, 8298), _record(22, 2010)]
    )
    got = read_log(str(path))
    assert [r["turn"] for r in got] == [0, 22, 48]


def test_read_log_ignores_blank_lines(tmp_path):
    path = tmp_path / "traj.jsonl"
    path.write_text(
        json.dumps(_record(0, 100)) + "\n\n" + json.dumps(_record(1, 90)) + "\n",
        encoding="utf-8",
    )
    got = read_log(str(path))
    assert [r["turn"] for r in got] == [0, 1]


def test_read_log_missing_file():
    with pytest.raises(TrajectoryError, match="file not found"):
        read_log("/no/such/trajectory.jsonl")


def test_read_log_empty_file(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    with pytest.raises(TrajectoryError, match="no records"):
        read_log(str(path))


def test_read_log_malformed_json_line(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(_record(0, 100)) + "\n{not json\n", encoding="utf-8")
    with pytest.raises(TrajectoryError, match="not valid JSON"):
        read_log(str(path))


def test_read_log_record_not_object(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text("[1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(TrajectoryError, match="must be a JSON object"):
        read_log(str(path))


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda r: r.pop("turn"), "'turn' is required"),
        (lambda r: r.update(turn="x"), "'turn' is required"),
        (lambda r: r.pop("candidate_ref"), "candidate_ref"),
        (lambda r: r.pop("objective"), "objective"),
        (lambda r: r["objective"].pop("value"), "objective.value"),
        (lambda r: r["objective"].update(polarity="sideways"), "polarity"),
    ],
)
def test_read_log_rejects_invalid_record(tmp_path, mutate, match):
    rec = _record(0, 100)
    mutate(rec)
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    with pytest.raises(TrajectoryError, match=match):
        read_log(str(path))


def test_read_log_rejects_bad_gate_results(tmp_path):
    rec = _record(0, 100)
    rec["gate_results"] = [{"check": "drc"}]  # missing status
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    with pytest.raises(TrajectoryError, match="check.*status"):
        read_log(str(path))


# ---------------------------------------------------------------------------
# record_from_eval / append_record (issue #437 -- wiring a `klt eval`
# envelope, #387, into this module's own record schema)
# ---------------------------------------------------------------------------


def _eval_report(**overrides) -> dict:
    report = {
        "schema_version": 1,
        "valid": True,
        "gates": [
            {
                "check": "drc",
                "name": "drc",
                "status": "pass",
                "exit_code": 0,
                "count": 0,
            },
            {
                "check": "functional-verification",
                "name": "functional-verification",
                "status": "pass",
                "exit_code": 0,
                "count": 0,
            },
        ],
        "objective": {"name": "area_um2", "value": 2951.5808, "polarity": "minimize"},
        "metrics": {},
    }
    report.update(overrides)
    return report


def test_record_from_eval_builds_a_valid_record():
    report = _eval_report()

    record = record_from_eval(report, turn=22, candidate_ref="cand/turn-22.gds")

    assert record == {
        "schema_version": SCHEMA_VERSION,
        "turn": 22,
        "candidate_ref": "cand/turn-22.gds",
        "objective": {"name": "area_um2", "value": 2951.5808, "polarity": "minimize"},
        "gate_results": [
            {"check": "drc", "status": "pass"},
            {"check": "functional-verification", "status": "pass"},
        ],
    }


def test_record_from_eval_drops_gate_exit_code_and_count():
    """`gate_results` is a summary -- `exit_code`/`count`/`name` from the
    `klt eval` envelope's own `gates[]` entries are not carried over."""
    report = _eval_report(
        valid=False,
        gates=[
            {
                "check": "drc",
                "name": "drc",
                "status": "fail",
                "exit_code": 3,
                "count": 5,
            }
        ],
    )

    record = record_from_eval(report, turn=1, candidate_ref="cand/turn-1.gds")

    assert record["gate_results"] == [{"check": "drc", "status": "fail"}]
    assert "exit_code" not in record["gate_results"][0]
    assert "count" not in record["gate_results"][0]
    assert "name" not in record["gate_results"][0]


def test_record_from_eval_optional_fields():
    report = _eval_report()

    record = record_from_eval(
        report,
        turn=48,
        candidate_ref="cand/turn-48.gds",
        description="pruned registers",
        wall_clock_s=9.8,
    )

    assert record["description"] == "pruned registers"
    assert record["wall_clock_s"] == 9.8


def test_record_from_eval_omits_optional_fields_when_not_given():
    report = _eval_report()

    record = record_from_eval(report, turn=0, candidate_ref="cand/turn-0.gds")

    assert "description" not in record
    assert "wall_clock_s" not in record


def test_record_from_eval_rejects_non_eval_report():
    with pytest.raises(TrajectoryError, match="objective"):
        record_from_eval({"not": "an eval envelope"}, turn=0, candidate_ref="x")


def test_record_from_eval_validates_turn_and_candidate_ref():
    report = _eval_report()
    with pytest.raises(TrajectoryError, match="turn"):
        record_from_eval(report, turn="not-an-int", candidate_ref="x")  # type: ignore[arg-type]
    with pytest.raises(TrajectoryError, match="candidate_ref"):
        record_from_eval(report, turn=0, candidate_ref="")


def test_append_record_creates_file_and_parent_dirs(tmp_path):
    report = _eval_report()
    record = record_from_eval(report, turn=0, candidate_ref="cand/turn-0.gds")
    log_path = tmp_path / "nested" / "run.jsonl"

    append_record(str(log_path), record)

    assert log_path.is_file()
    got = read_log(str(log_path))
    assert got == [record]


def test_append_record_appends_multiple_records_readable_by_read_log(tmp_path):
    log_path = tmp_path / "run.jsonl"

    for turn, value in ((0, 8298), (22, 2010), (113, 907)):
        report = _eval_report(
            objective={"name": "gates", "value": value, "polarity": "minimize"}
        )
        record = record_from_eval(
            report, turn=turn, candidate_ref=f"cand/turn-{turn}.gds"
        )
        append_record(str(log_path), record)

    got = read_log(str(log_path))
    assert [r["turn"] for r in got] == [0, 22, 113]

    trajectory = build_trajectory(str(log_path))
    assert trajectory["record_count"] == 3
    assert trajectory["milestone_count"] == 2


def test_append_record_write_failure_raises_trajectory_error(tmp_path):
    # A path naming an existing directory can never be opened for append.
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    with pytest.raises(TrajectoryError, match="could not append"):
        append_record(str(directory), {"turn": 0})


# ---------------------------------------------------------------------------
# milestone detection
# ---------------------------------------------------------------------------


def test_detect_milestones_minimize_basic():
    records = [_record(0, 8298), _record(22, 2010), _record(48, 1304)]
    summary = detect_milestones(records)

    assert summary["polarity"] == "minimize"
    assert summary["baseline"]["objective"] == 8298
    assert summary["best"]["objective"] == 1304
    assert [m["turn"] for m in summary["milestones"]] == [22, 48]
    first = summary["milestones"][0]
    assert first["objective_before"] == 8298
    assert first["objective_after"] == 2010
    assert first["prior_turn"] == 0
    assert first["delta"] == 8298 - 2010


def test_first_record_is_baseline_not_a_milestone():
    summary = detect_milestones([_record(0, 100), _record(1, 90)])
    assert summary["baseline"]["turn"] == 0
    assert [m["turn"] for m in summary["milestones"]] == [1]


def test_detect_milestones_maximize():
    # For a maximize objective, a *higher* value is an improvement.
    records = [
        _record(0, 40.0, polarity="maximize", name="gain_db"),
        _record(5, 55.0, polarity="maximize", name="gain_db"),
        _record(9, 48.0, polarity="maximize", name="gain_db"),  # regression, not a ms
        _record(12, 60.0, polarity="maximize", name="gain_db"),
    ]
    summary = detect_milestones(records)
    assert [m["turn"] for m in summary["milestones"]] == [5, 12]
    assert summary["best"]["objective"] == 60.0
    # The turn-9 regression must not update the best it is measured against.
    assert summary["milestones"][1]["objective_before"] == 55.0


def test_threshold_boundary_just_under_and_just_over():
    # Baseline 100, then a candidate improving by exactly 10, then by 10.0001.
    records = [_record(0, 100.0), _record(1, 90.0), _record(2, 79.9999)]

    # threshold == 10: the turn-1 improvement of exactly 10 is NOT a milestone
    # (strict >), but turn-2's improvement over the running best (90 -> 79.9999
    # = 10.0001) IS.
    summary = detect_milestones(records, threshold=10.0)
    assert [m["turn"] for m in summary["milestones"]] == [2]

    # threshold just under 10: the turn-1 improvement of 10 now clears it.
    summary = detect_milestones(records, threshold=9.9999)
    assert 1 in [m["turn"] for m in summary["milestones"]]


def test_threshold_running_best_updates_below_threshold():
    # Small sub-threshold improvements still advance the running best, so a
    # milestone's objective_before is the true best-so-far, not the last
    # milestone's value.
    records = [_record(0, 100.0), _record(1, 98.0), _record(2, 80.0)]
    summary = detect_milestones(records, threshold=5.0)
    # turn-1 (improvement 2) is sub-threshold; turn-2 improves on the *98*
    # running best by 18 -> milestone, with objective_before == 98.
    assert [m["turn"] for m in summary["milestones"]] == [2]
    assert summary["milestones"][0]["objective_before"] == 98.0


def test_detect_milestones_rejects_mixed_objective():
    records = [
        _record(0, 100, name="gates"),
        _record(1, 90, name="area_um2"),
    ]
    with pytest.raises(TrajectoryError, match="mixes objectives"):
        detect_milestones(records)


def test_detect_milestones_rejects_mixed_polarity():
    records = [
        _record(0, 100, polarity="minimize"),
        _record(1, 110, polarity="maximize"),
    ]
    with pytest.raises(TrajectoryError, match="mixes objectives"):
        detect_milestones(records)


# ---------------------------------------------------------------------------
# renderers
# ---------------------------------------------------------------------------


def test_render_markdown_table_rows_match_expected():
    records = [
        _record(0, 8298),
        _record(22, 2010, description="iterative shift-subtract divider"),
        _record(48, 1304, description="merged reduction modules"),
    ]
    summary = detect_milestones(records)
    md = render_markdown_table(summary)

    lines = md.splitlines()
    assert lines[0].startswith("### Optimization milestones (gates, minimize)")
    assert "| turns | gates | what changed |" in lines
    assert "| 0-22 | 8298 -> 2010 | iterative shift-subtract divider |" in lines
    assert "| 22-48 | 2010 -> 1304 | merged reduction modules |" in lines


def test_render_markdown_table_no_milestones():
    # A monotonically-worsening (or flat) log has a baseline but no milestones.
    records = [_record(0, 100), _record(1, 100), _record(2, 100)]
    md = render_markdown_table(detect_milestones(records))
    assert "No milestones" in md
    assert "| turns |" not in md


def test_render_markdown_table_escapes_pipes():
    records = [_record(0, 100), _record(1, 50, description="a | b")]
    md = render_markdown_table(detect_milestones(records))
    assert "a \\| b" in md


def test_render_plot_svg_is_well_formed_without_optimizer():
    records = [_record(0, 8298), _record(22, 2010), _record(48, 1304)]
    summary = detect_milestones(records)
    svg = render_plot_svg(records, summary)

    assert svg.startswith("<svg")
    assert svg.rstrip().endswith("</svg>")
    assert "gates vs turn" in svg
    assert "polyline" in svg
    # One milestone marker (r=4.5) per milestone turn.
    assert svg.count('r="4.5"') == len(summary["milestones"])


def test_render_plot_svg_handles_single_record():
    # Degenerate one-record log must not divide by zero.
    records = [_record(0, 100)]
    summary = detect_milestones(records)
    svg = render_plot_svg(records, summary)
    assert svg.startswith("<svg")


# ---------------------------------------------------------------------------
# build_trajectory (library entry point)
# ---------------------------------------------------------------------------


def test_build_trajectory_payload_shape(tmp_path):
    path = _write_log(
        tmp_path,
        [
            _record(0, 8298),
            _record(22, 2010, description="shift-subtract"),
            _record(48, 1304),
        ],
    )
    payload = build_trajectory(str(path))

    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["record_count"] == 3
    assert payload["objective_name"] == "gates"
    assert payload["polarity"] == "minimize"
    assert payload["milestone_count"] == 2
    assert payload["baseline"]["objective"] == 8298
    assert payload["best"]["objective"] == 1304
    assert payload["markdown"].startswith("### Optimization milestones")
    assert payload["plot_svg"].startswith("<svg")


# ---------------------------------------------------------------------------
# CLI: format parity, exit codes, --plot
# ---------------------------------------------------------------------------


def test_cli_json_contract(tmp_path, capsys):
    path = _write_log(tmp_path, [_record(0, 100), _record(1, 40, description="win")])

    assert main(["trajectory", str(path), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert data["schema_version"] == SCHEMA_VERSION
    assert data["record_count"] == 2
    assert data["milestone_count"] == 1
    assert data["milestones"][0]["objective_after"] == 40
    assert data["plot_svg"].startswith("<svg")


def test_cli_text_and_json_parity(tmp_path, capsys):
    path = _write_log(tmp_path, [_record(0, 100), _record(1, 40, description="win")])

    assert main(["trajectory", str(path), "--format", "text"]) == 0
    text_out = capsys.readouterr().out

    assert main(["trajectory", str(path), "--format", "json"]) == 0
    json_out = json.loads(capsys.readouterr().out)

    # The text rendering is exactly the payload's markdown table (+ newline).
    assert json_out["markdown"] in text_out


def test_cli_default_format_is_text(tmp_path, capsys):
    path = _write_log(tmp_path, [_record(0, 100), _record(1, 40)])
    assert main(["trajectory", str(path)]) == 0
    out = capsys.readouterr().out
    assert "### Optimization milestones" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_plot_written_to_file(tmp_path, capsys):
    path = _write_log(tmp_path, [_record(0, 100), _record(1, 40)])
    plot_path = tmp_path / "traj.svg"

    assert main(["trajectory", str(path), "--plot", str(plot_path)]) == 0
    assert plot_path.read_text(encoding="utf-8").startswith("<svg")
    assert f"plot written to {plot_path}" in capsys.readouterr().out


def test_cli_malformed_log_exits_1_text(tmp_path, capsys):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json\n", encoding="utf-8")

    assert main(["trajectory", str(bad)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "klt trajectory" in captured.err


def test_cli_empty_log_exits_1_json(tmp_path, capsys):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")

    assert main(["trajectory", str(empty), "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "trajectory"
    assert "no records" in error["error"]["message"]


def test_cli_missing_log_exits_1(tmp_path, capsys):
    assert main(["trajectory", str(tmp_path / "nope.jsonl")]) == 1
    assert "file not found" in capsys.readouterr().err
