"""Tests for `klt trajectory` and the `trajectory` library module.

Every fixture is a hand-written JSONL log built inside the test — which is
also the point of the feature: the renderer must work on a human-curated
log with no optimizer, no proposer, and no candidate artifacts on disk (see
`docs/cli/trajectory.md`). Flat test layout, mirroring `tests/test_drc.py`.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.trajectory import (
    RECORD_SCHEMA_VERSION,
    SCHEMA_VERSION,
    TrajectoryError,
    analyze,
    append_record,
    build_trajectory_report,
    make_record,
    read_records,
    render_plot_svg,
)

# The worked example from issue #388's Context section, trimmed to five
# records: a baseline, two real milestones, one gate-failing candidate whose
# objective looks better than the incumbent, and one sub-threshold nudge.
_FIXTURE = [
    {
        "turn": 1,
        "candidate_ref": "runs/t1/div.gds",
        "objective": {
            "name": "gate_count",
            "value": 8298,
            "polarity": "minimize",
            "unit": "gates",
        },
        "gate_results": [{"check": "drc", "status": "clean"}],
        "wall_clock_s": 12.5,
        "note": "baseline hardware modulo divider",
    },
    {
        "turn": 22,
        "candidate_ref": "runs/t22/div.gds",
        "objective": {
            "name": "gate_count",
            "value": 2010,
            "polarity": "minimize",
            "unit": "gates",
        },
        "gate_results": [
            {"check": "drc", "status": "clean"},
            {"check": "lvs", "status": "match"},
        ],
        "eval_ref": "runs/t22/eval.json",
        "wall_clock_s": 41.2,
        "note": "iterative shift-subtract",
    },
    {
        "turn": 30,
        "candidate_ref": "runs/t30/div.gds",
        "objective": {
            "name": "gate_count",
            "value": 1500,
            "polarity": "minimize",
            "unit": "gates",
        },
        "gate_results": [{"check": "drc", "status": "violations"}],
        "wall_clock_s": 8.0,
        "note": "abandoned: narrowed register broke DRC",
    },
    {
        "turn": 48,
        "candidate_ref": "runs/t48/div.gds",
        "objective": {
            "name": "gate_count",
            "value": 1304,
            "polarity": "minimize",
            "unit": "gates",
        },
        "gate_results": [{"check": "drc", "status": "clean"}],
        "wall_clock_s": 55.0,
        "note": "bypassed REDUCE stage",
    },
    {
        "turn": 60,
        "candidate_ref": "runs/t60/div.gds",
        "objective": {
            "name": "gate_count",
            "value": 1300,
            "polarity": "minimize",
            "unit": "gates",
        },
        "gate_results": [{"check": "drc", "status": "clean"}],
        "wall_clock_s": 30.0,
        "note": "minor register prune",
    },
]


def _write_log(tmp_path, records, name="trajectory.jsonl") -> str:
    path = tmp_path / name
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    return str(path)


def _minimal(turn: int, value: float, polarity: str = "minimize") -> dict:
    return {
        "turn": turn,
        "candidate_ref": f"runs/t{turn}/out.gds",
        "objective": {"name": "area_um2", "value": value, "polarity": polarity},
    }


# ---------------------------------------------------------------------------
# record round-trip
# ---------------------------------------------------------------------------


def test_record_round_trip(tmp_path):
    """A record built by `make_record`, appended, and read back is the same
    record -- the append-only log is a lossless carrier for the schema."""
    path = str(tmp_path / "log.jsonl")
    written = [
        make_record(
            turn=1,
            candidate_ref="runs/t1/out.gds",
            objective_name="gate_count",
            objective_value=8298,
            polarity="minimize",
            wall_clock_s=12.5,
            unit="gates",
            gate_results=[{"check": "drc", "status": "clean"}],
            eval_ref="runs/t1/eval.json",
            note="baseline",
        ),
        make_record(
            turn=22,
            candidate_ref="runs/t22/out.gds",
            objective_name="gate_count",
            objective_value=2010,
            polarity="minimize",
            wall_clock_s=41.2,
            unit="gates",
        ),
    ]
    for record in written:
        append_record(path, record)

    assert read_records(path) == written
    assert written[0]["record_schema_version"] == RECORD_SCHEMA_VERSION


def test_append_record_is_append_only(tmp_path):
    """A second append never rewrites the first line."""
    path = str(tmp_path / "log.jsonl")
    first = make_record(1, "a.gds", "area_um2", 100.0, "minimize", 1.0)
    append_record(path, first)
    before = (tmp_path / "log.jsonl").read_text(encoding="utf-8")

    append_record(path, make_record(2, "b.gds", "area_um2", 90.0, "minimize", 1.0))
    after = (tmp_path / "log.jsonl").read_text(encoding="utf-8")

    assert after.startswith(before)
    assert len(after.splitlines()) == 2


def test_read_records_skips_blank_lines(tmp_path):
    """A hand-edited log with blank lines still reads -- blank lines are the
    one non-JSON line a log tolerates."""
    path = tmp_path / "log.jsonl"
    path.write_text(
        f"\n{json.dumps(_minimal(1, 100))}\n\n{json.dumps(_minimal(2, 90))}\n",
        encoding="utf-8",
    )

    records = read_records(str(path))

    assert [record["turn"] for record in records] == [1, 2]


def test_make_record_rejects_bad_polarity():
    with pytest.raises(TrajectoryError, match="polarity"):
        make_record(1, "a.gds", "area_um2", 100.0, "smaller", 1.0)


def test_make_record_rejects_inline_artifact_instead_of_ref():
    """`candidate_ref` must be a non-empty string -- the schema has no place
    to inline a candidate's contents (docs/cli/trajectory.md)."""
    with pytest.raises(TrajectoryError, match="candidate_ref"):
        make_record(1, "", "area_um2", 100.0, "minimize", 1.0)


def test_read_records_rejects_future_record_schema_version(tmp_path):
    record = _minimal(1, 100)
    record["record_schema_version"] = RECORD_SCHEMA_VERSION + 1
    path = _write_log(tmp_path, [record])

    with pytest.raises(TrajectoryError, match="newer than this klt"):
        read_records(path)


# ---------------------------------------------------------------------------
# milestone detection: thresholds, boundaries, polarity
# ---------------------------------------------------------------------------


def test_milestone_threshold_boundary_just_under_and_just_over():
    """The absolute threshold is strict: an improvement landing exactly on
    it is not a milestone, one a hair over it is."""
    records = [_minimal(1, 100.0), _minimal(2, 90.0)]  # improvement == 10.0

    exactly_at = analyze(records, threshold=10.0)
    just_under = analyze([_minimal(1, 100.0), _minimal(2, 90.01)], threshold=10.0)
    just_over = analyze([_minimal(1, 100.0), _minimal(2, 89.99)], threshold=10.0)

    assert exactly_at["milestone_count"] == 0
    assert just_under["milestone_count"] == 0
    assert just_over["milestone_count"] == 1


def test_milestone_percent_threshold_boundary():
    """The percentage threshold is strict too, and is applied against the
    best prior value."""
    records = [_minimal(1, 100.0), _minimal(2, 90.0)]  # exactly 10%

    assert analyze(records, threshold_pct=10.0)["milestone_count"] == 0
    assert analyze(records, threshold_pct=9.9)["milestone_count"] == 1


def test_milestone_requires_both_thresholds():
    """`--threshold` and `--threshold-pct` are ANDed: clearing one but not
    the other is not a milestone."""
    records = [_minimal(1, 1000.0), _minimal(2, 950.0)]  # 50 units == 5%

    assert analyze(records, threshold=10.0, threshold_pct=1.0)["milestone_count"] == 1
    # Clears the absolute bar, misses the relative one.
    assert analyze(records, threshold=10.0, threshold_pct=50.0)["milestone_count"] == 0
    # Clears the relative bar, misses the absolute one.
    assert analyze(records, threshold=100.0, threshold_pct=1.0)["milestone_count"] == 0


def test_polarity_minimize_flags_decrease_only():
    payload = analyze([_minimal(1, 100.0), _minimal(2, 90.0), _minimal(3, 95.0)])

    assert [m["turn"] for m in payload["milestones"]] == [2]
    assert payload["best"]["value"] == 90.0
    assert payload["records"][2]["improvement"] == pytest.approx(-5.0)


def test_polarity_maximize_flags_increase_only():
    """The same value sequence under `maximize` produces the mirror-image
    result -- the improvement sign is polarity-derived, never assumed."""
    records = [
        _minimal(1, 100.0, "maximize"),
        _minimal(2, 90.0, "maximize"),
        _minimal(3, 110.0, "maximize"),
    ]

    payload = analyze(records)

    assert [m["turn"] for m in payload["milestones"]] == [3]
    assert payload["best"]["value"] == 110.0
    assert payload["total_improvement"] == pytest.approx(10.0)


def test_baseline_is_never_a_milestone():
    """With no prior record there is no improvement to measure, so the first
    record is the 'before' value, not a milestone row."""
    payload = analyze([_minimal(1, 100.0)])

    assert payload["milestone_count"] == 0
    assert payload["baseline"] == payload["best"]
    assert payload["total_improvement"] == pytest.approx(0.0)


def test_best_so_far_tracks_every_improvement_not_just_milestones():
    """Sub-threshold nudges still raise the bar, so they cannot accumulate
    into a spurious milestone later."""
    records = [_minimal(1, 100.0), _minimal(2, 96.0), _minimal(3, 92.0)]

    payload = analyze(records, threshold=5.0)

    assert payload["milestone_count"] == 0
    assert payload["best"]["value"] == 92.0
    assert [entry["best_so_far"] for entry in payload["records"]] == [True, True, True]


def test_gate_failing_candidate_is_ineligible():
    """A candidate that improves the objective but fails a gate is kept in
    the log and the output, but never becomes best and is never a
    milestone -- an objective win that fails its checks is not a win."""
    failing = _minimal(2, 10.0)
    failing["gate_results"] = [{"check": "drc", "status": "violations"}]
    records = [_minimal(1, 100.0), failing, _minimal(3, 90.0)]

    payload = analyze(records)

    assert payload["records"][1]["gate_status"] == "fail"
    assert payload["records"][1]["milestone"] is False
    assert payload["records"][1]["best_so_far"] is False
    assert payload["best"]["value"] == 90.0
    assert [m["turn"] for m in payload["milestones"]] == [3]
    # The abandoned candidate is still evidence: it stays in records[].
    assert payload["record_count"] == 3


def test_missing_gate_results_is_unknown_and_eligible():
    """A hand-curated log that never ran gates still renders -- `unknown` is
    eligible, not a silent failure."""
    payload = analyze([_minimal(1, 100.0), _minimal(2, 90.0)])

    assert {entry["gate_status"] for entry in payload["records"]} == {"unknown"}
    assert payload["milestone_count"] == 1


def test_milestone_spans_from_best_prior_record():
    """A milestone's `from_turn`/`from_value` name the best *prior* record,
    not the immediately preceding one -- that is what makes the rendered
    turn span honest."""
    records = [_minimal(1, 100.0), _minimal(2, 99.0), _minimal(3, 50.0)]

    payload = analyze(records, threshold=10.0)

    (milestone,) = payload["milestones"]
    assert milestone["from_turn"] == 2
    assert milestone["from_value"] == 99.0
    assert milestone["improvement"] == pytest.approx(49.0)


def test_zero_reference_improvement_is_not_swallowed_by_percent_threshold():
    records = [
        _minimal(1, 0.0, "maximize"),
        _minimal(2, 5.0, "maximize"),
    ]

    payload = analyze(records, threshold_pct=99.0)

    assert payload["milestone_count"] == 1


# ---------------------------------------------------------------------------
# malformed / inconsistent logs
# ---------------------------------------------------------------------------


def test_empty_log_is_an_error(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("\n\n", encoding="utf-8")

    with pytest.raises(TrajectoryError, match="contains no records"):
        read_records(str(path))


def test_malformed_line_names_the_line_number(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text(
        f"{json.dumps(_minimal(1, 100))}\nnot json at all\n", encoding="utf-8"
    )

    with pytest.raises(TrajectoryError, match=r"bad\.jsonl:2: not valid JSON"):
        read_records(str(path))


def test_non_object_line_is_an_error(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text("[1, 2, 3]\n", encoding="utf-8")

    with pytest.raises(TrajectoryError, match="must be a JSON object"):
        read_records(str(path))


def test_missing_required_field_is_an_error(tmp_path):
    record = _minimal(1, 100)
    del record["candidate_ref"]
    path = _write_log(tmp_path, [record])

    with pytest.raises(TrajectoryError, match="candidate_ref"):
        read_records(path)


def test_mixed_objective_name_is_an_error():
    second = _minimal(2, 90)
    second["objective"]["name"] = "gate_count"

    with pytest.raises(TrajectoryError, match="one log tracks one objective"):
        analyze([_minimal(1, 100), second])


def test_mixed_polarity_is_an_error():
    with pytest.raises(TrajectoryError, match="polarity"):
        analyze([_minimal(1, 100), _minimal(2, 90, "maximize")])


def test_backwards_turn_is_an_error():
    with pytest.raises(TrajectoryError, match="must not go backwards"):
        analyze([_minimal(5, 100), _minimal(2, 90)])


def test_equal_turns_are_allowed():
    """Two candidates evaluated in the same batch share a turn."""
    payload = analyze([_minimal(1, 100), _minimal(1, 90)])

    assert payload["record_count"] == 2
    assert payload["milestone_count"] == 1


def test_missing_file_is_an_error():
    with pytest.raises(TrajectoryError, match="file not found"):
        read_records("/no/such/path/trajectory.jsonl")


# ---------------------------------------------------------------------------
# rendering: markdown table + plot
# ---------------------------------------------------------------------------


def test_markdown_milestone_table_rows(tmp_path):
    """The hand-written milestone table from the issue's Context section is
    derived from the log: one row per milestone, with the turn span, the
    objective transition, and the record's own note."""
    path = _write_log(tmp_path, _FIXTURE)

    payload = build_trajectory_report(path, threshold=100)

    assert payload["milestone_count"] == 2
    table_rows = [
        line for line in payload["markdown"].splitlines() if line.startswith("| ")
    ]
    assert table_rows == [
        "| turns | gate_count (gates) | what changed |",
        "| --- | --- | --- |",
        "| 1-22 | 8,298 -> 2,010 | iterative shift-subtract |",
        "| 22-48 | 2,010 -> 1,304 | bypassed REDUCE stage |",
    ]


def test_text_rendering_carries_the_same_rows(tmp_path):
    path = _write_log(tmp_path, _FIXTURE)

    payload = build_trajectory_report(path, threshold=100)

    assert "1-22   8,298 -> 2,010" in payload["text"]
    assert "22-48  2,010 -> 1,304" in payload["text"]
    assert "Baseline: 8,298 at turn 1" in payload["text"]
    assert "Best: 1,300 at turn 60" in payload["text"]


def test_no_milestones_renders_a_clear_state_not_an_empty_table(tmp_path):
    path = _write_log(tmp_path, _FIXTURE)

    payload = build_trajectory_report(path, threshold=1_000_000)

    assert payload["milestone_count"] == 0
    assert "No milestones" in payload["text"]
    assert "No milestones" in payload["markdown"]


def test_plot_is_written_without_an_optimizer(tmp_path):
    """The plot generation call succeeds against a hand-written log alone --
    no optimizer, no candidate artifacts on disk."""
    path = _write_log(tmp_path, _FIXTURE)
    plot_path = tmp_path / "trajectory.svg"

    payload = build_trajectory_report(path, threshold=100, plot_path=str(plot_path))

    assert payload["plot"] == {"format": "svg", "path": str(plot_path)}
    svg = plot_path.read_text(encoding="utf-8")
    assert svg.startswith("<svg ")
    assert svg.rstrip().endswith("</svg>")
    assert "gate_count (gates) vs turn" in svg
    # One labelled marker per milestone turn.
    assert ">22</text>" in svg
    assert ">48</text>" in svg
    assert payload["markdown"].endswith(f"![objective vs turn]({plot_path})\n")


def test_plot_handles_a_single_flat_record(tmp_path):
    """Degenerate input (one record, zero-span axes) must not divide by
    zero."""
    payload = analyze([_minimal(1, 100.0)])
    svg = render_plot_svg(payload)

    assert svg.startswith("<svg ")
    assert "nan" not in svg.lower()


def test_plot_path_unwritable_is_a_clean_error(tmp_path):
    path = _write_log(tmp_path, _FIXTURE)

    with pytest.raises(TrajectoryError, match="could not write plot"):
        build_trajectory_report(path, plot_path=str(tmp_path / "no" / "dir" / "p.svg"))


# ---------------------------------------------------------------------------
# CLI: JSON contract, format parity, exit codes
# ---------------------------------------------------------------------------


def test_json_contract(tmp_path, capsys):
    path = _write_log(tmp_path, _FIXTURE)

    assert main(["trajectory", path, "--threshold", "100", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert set(data.keys()) == {
        "schema_version",
        "record_schema_version",
        "source",
        "objective",
        "threshold",
        "record_count",
        "first_turn",
        "last_turn",
        "baseline",
        "best",
        "total_improvement",
        "total_improvement_pct",
        "wall_clock_s_total",
        "milestone_count",
        "milestones",
        "records",
        "plot",
        "markdown",
        "text",
    }
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["record_schema_version"] == RECORD_SCHEMA_VERSION
    assert data["source"] == path
    assert data["objective"] == {
        "name": "gate_count",
        "polarity": "minimize",
        "unit": "gates",
    }
    assert data["threshold"] == {"absolute": 100.0, "percent": 0.0}
    assert data["record_count"] == 5
    assert data["first_turn"] == 1
    assert data["last_turn"] == 60
    assert data["baseline"] == {
        "turn": 1,
        "value": 8298,
        "candidate_ref": "runs/t1/div.gds",
    }
    assert data["best"] == {
        "turn": 60,
        "value": 1300,
        "candidate_ref": "runs/t60/div.gds",
    }
    assert data["wall_clock_s_total"] == pytest.approx(146.7)
    assert data["milestone_count"] == len(data["milestones"]) == 2
    assert data["plot"] is None

    for milestone in data["milestones"]:
        assert set(milestone.keys()) == {
            "turn",
            "from_turn",
            "from_value",
            "value",
            "improvement",
            "improvement_pct",
            "candidate_ref",
            "gate_status",
            "note",
        }
        assert milestone["gate_status"] in {"pass", "unknown"}

    assert len(data["records"]) == data["record_count"]
    for entry in data["records"]:
        assert set(entry.keys()) == {
            "turn",
            "candidate_ref",
            "value",
            "gate_status",
            "eval_ref",
            "wall_clock_s",
            "note",
            "milestone",
            "improvement",
            "improvement_pct",
            "best_so_far",
        }
        assert isinstance(entry["turn"], int)
        assert isinstance(entry["candidate_ref"], str)
        assert entry["gate_status"] in {"pass", "fail", "unknown"}

    # Turn order is preserved from the log, not re-sorted.
    assert [entry["turn"] for entry in data["records"]] == [1, 22, 30, 48, 60]


def test_json_never_inlines_candidate_or_eval_payloads(tmp_path, capsys):
    """Artifacts are referenced by path, never embedded -- the storage
    contract that keeps a 500-turn log small."""
    path = _write_log(tmp_path, _FIXTURE)

    assert main(["trajectory", path, "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    entry = next(e for e in data["records"] if e["turn"] == 22)
    assert entry["eval_ref"] == "runs/t22/eval.json"
    assert entry["candidate_ref"] == "runs/t22/div.gds"
    # No gate payload, no artifact contents anywhere in the record view.
    assert "gate_results" not in entry


def test_default_format_is_text(tmp_path, capsys):
    path = _write_log(tmp_path, _FIXTURE)

    assert main(["trajectory", path, "--threshold", "100"]) == 0
    out = capsys.readouterr().out

    assert "Optimization Trajectory: gate_count (gates)" in out
    assert "8,298 -> 2,010" in out
    # Not JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_format_parity_text_matches_json_text_field(tmp_path, capsys):
    """`--format text` stdout is exactly the JSON envelope's `text` field,
    and `--format github-summary` is exactly its `markdown` field."""
    path = _write_log(tmp_path, _FIXTURE)

    assert main(["trajectory", path, "--threshold", "100", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert main(["trajectory", path, "--threshold", "100"]) == 0
    assert capsys.readouterr().out == data["text"]

    assert (
        main(["trajectory", path, "--threshold", "100", "--format", "github-summary"])
        == 0
    )
    assert capsys.readouterr().out == data["markdown"]


def test_cli_writes_plot(tmp_path, capsys):
    path = _write_log(tmp_path, _FIXTURE)
    plot_path = tmp_path / "trajectory.svg"

    assert main(["trajectory", path, "--plot", str(plot_path), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert data["plot"] == {"format": "svg", "path": str(plot_path)}
    assert plot_path.exists()


def test_empty_log_exits_1_text(tmp_path, capsys):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")

    assert main(["trajectory", str(path)]) == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "klt trajectory" in captured.err
    assert "contains no records" in captured.err


def test_malformed_log_exits_1_json(tmp_path, capsys):
    """A malformed log takes the documented error path (exit 1 via
    `emit_error`), never a traceback."""
    path = tmp_path / "bad.jsonl"
    path.write_text("{not json}\n", encoding="utf-8")

    assert main(["trajectory", str(path), "--format", "json"]) == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "trajectory"
    assert "not valid JSON" in error["error"]["message"]


def test_missing_log_exits_1(tmp_path, capsys):
    assert main(["trajectory", str(tmp_path / "missing.jsonl")]) == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "file not found" in captured.err


def test_github_summary_error_falls_back_to_text(tmp_path, capsys):
    """`github-summary` is a rendering choice, not an error format -- errors
    under it use the plain-text stderr line, matching `klt report`."""
    path = tmp_path / "missing.jsonl"

    assert main(["trajectory", str(path), "--format", "github-summary"]) == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err.startswith("klt trajectory: ")


def test_stdin_source(tmp_path, capsys, monkeypatch):
    """`-` reads the log from stdin, like every other klt verb that takes
    a document."""
    import io

    text = "".join(json.dumps(record) + "\n" for record in _FIXTURE)
    monkeypatch.setattr("sys.stdin", io.StringIO(text))

    assert main(["trajectory", "-", "--threshold", "100", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert data["source"] == "-"
    assert data["record_count"] == 5
