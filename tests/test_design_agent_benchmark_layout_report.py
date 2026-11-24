"""Tests for layout-flow reporting (issue #3027, epic #2252):
`scripts/design_agent_benchmark_layout_report.py` and `report-layout`.

Credential-free, simulator-free and sandbox-free: reports are built from
synthetic `runs.jsonl` / `journal.jsonl` ledgers alone, which is the contract.
"""

from __future__ import annotations

import copy
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import design_agent_benchmark_layout as layout  # noqa: E402
import design_agent_benchmark_layout_report as rep  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = SCRIPTS_DIR / "design_agent_benchmark.py"
GDS = "a" * 64
NETLIST = "b" * 64
TASK_SHA = {"t1": "1" * 64, "t2": "2" * 64}
_CALLS = {
    "draw-only": [("draw", "draw")],
    "gen-only": [("gen", "generate")],
    "gen+refine": [("gen", "generate"), ("draw", "draw")],
}
_PHASE = {"draw-only": "draw", "gen-only": "generate", "gen+refine": "refine"}


@pytest.fixture(scope="module")
def protocol() -> dict[str, Any]:
    return layout.load_protocol()


def _check(status: str, **extra: str) -> dict[str, Any]:
    return {"status": status, "gds_sha256": GDS, "report_sha256": "c" * 64, **extra}


def make_run(
    protocol: dict[str, Any],
    task: str,
    arm: str,
    area: float | None,
    *,
    agent: str = "a1",
    replicate: int = 1,
    status: str | None = None,
    task_sha: str | None = None,
    klt: str = "0.1.0",
) -> dict[str, Any]:
    """A protocol-valid `layout_run/1` record; `area=None` is an `agent_error`."""
    status = status or ("valid" if area is not None else "agent_error")
    task_sha = task_sha or TASK_SHA[task]
    run: dict[str, Any] = {
        "schema": "klt.design_agent_benchmark.layout_run/1",
        "run_id": f"{task}.{arm}.{agent}.r{replicate}.a1",
        "protocol_sha256": layout.document_sha256(protocol),
        "task_id": task,
        "task_sha256": task_sha,
        "arm": arm,
        "agent": {"id": agent, "provider": "reference", "model": None},
        "replicate": replicate,
        "seed_sha256": layout.run_seed_sha256(
            protocol,
            task_id=task,
            task_sha256=task_sha,
            arm=arm,
            agent_id=agent,
            replicate=replicate,
            seed_artifact_sha256=GDS
            if arm == "gen+refine" and status != "not_run_no_seed"
            else None,
        ),
        "seed_from": None,
        "tool_calls": [],
        "status": status,
        "artifact": None,
        "validity": None,
        "score": None,
        "usage": None,
        "notes": None,
        "runtime_s": 1.0,
        "versions": {"protocol": protocol["protocol_version"], "klt": klt},
        "diagnostics": {},
    }
    if status == "not_run_no_seed":
        return run
    run["tool_calls"] = [
        {"seq": i, "phase": _PHASE[arm], "verb": v, "class": c, "exit_code": 0}
        for i, (v, c) in enumerate(_CALLS[arm], 1)
    ]
    if status == "valid":
        run["artifact"] = {"gds_sha256": GDS, "cell_name": "top", "selected_round": 1}
        run["validity"] = {
            "drc": _check("clean"),
            "lvs": _check("match", netlist_sha256=NETLIST),
        }
        run["score"] = {"area_um2": area, "utilization": None, "sim_margin": None}
    return run


def with_seed(run: dict[str, Any], seed: dict[str, Any]) -> dict[str, Any]:
    """Link a gen+refine record to its gen-only run, as the executor does."""
    run["seed_from"] = {"run_id": seed["run_id"], "gds_sha256": GDS}
    return run


def corpus(protocol: dict[str, Any]) -> list[dict[str, Any]]:
    """t1: gen-only 0.5x and gen+refine 0.4x of draw-only (area 100); t2:
    gen-only 2x; draw-only fails on replicate 2 so that pair is excluded."""
    p = protocol
    records = [
        make_run(p, "t1", "draw-only", 100.0),
        make_run(p, "t1", "gen-only", 50.0),
        make_run(p, "t2", "draw-only", 10.0),
        make_run(p, "t2", "gen-only", 20.0),
        make_run(p, "t1", "draw-only", None, replicate=2),
        make_run(p, "t1", "gen-only", 40.0, replicate=2),
    ]
    seed = records[1]
    records.append(with_seed(make_run(p, "t1", "gen+refine", 40.0), seed))
    return records


def row(report: dict[str, Any], arm: str, agent: str = "a1") -> dict[str, Any]:
    return next(
        c for c in report["comparisons"] if c["arm"] == arm and c["agent_id"] == agent
    )


# --------------------------------------------------------------------------
# Golden rows and geometric means
# --------------------------------------------------------------------------


def test_golden_geomean_and_denominators(protocol: dict[str, Any]) -> None:
    report = rep.build_report(corpus(protocol), protocol)
    gen = row(report, "gen-only")
    # pairs: t1/r1 0.5, t2/r1 2.0 -> sqrt(1.0) = 1.0; t1/r2 is excluded.
    assert gen["geomean_ratio"] == 1.0
    assert gen["denominator"] == {
        "comparable_pairs": 2,
        "excluded_pairs": 1,
        "attempted_pairs": 3,
    }
    assert gen["per_task_geomean_ratio"] == {"t1": 0.5, "t2": 2.0}
    assert gen["excluded"] == [
        {"task_id": "t1", "replicate": 2, "reason": "draw-only_agent_error"}
    ]
    refine = row(report, "gen+refine")
    assert refine["geomean_ratio"] == 0.4
    assert refine["denominator"]["comparable_pairs"] == 1
    assert report["integrity"]["valid"], report["integrity"]


def test_tables_show_valid_counts_and_failures(protocol: dict[str, Any]) -> None:
    report = rep.build_report(corpus(protocol), protocol)
    table = {(t["task_id"], t["arm"]): t for t in report["tables"]}
    draw = table[("t1", "draw-only")]
    assert (draw["runs"], draw["valid_runs"]) == (2, 1)
    assert draw["failures"] == {"agent_error": 1}
    assert draw["area_um2"] == {"best": 100.0, "mean": 100.0}
    gen = table[("t1", "gen-only")]
    assert (gen["runs"], gen["valid_runs"], gen["failures"]) == (2, 2, {})
    assert gen["area_um2"] == {"best": 40.0, "mean": 45.0}
    assert report["totals"]["counted_runs"] == 7
    assert report["totals"]["valid_runs"] == 6


def test_geomean_is_order_independent_and_empty_is_none() -> None:
    assert rep.geomean([]) is None
    values = [0.3, 1.7, 2.2, 0.9]
    assert rep.geomean(values) == rep.geomean(values[::-1])
    assert rep.geomean(values) == round(math.prod(values) ** 0.25, 6)


# --------------------------------------------------------------------------
# Invalid / missing arms are excluded and disclosed, never imputed
# --------------------------------------------------------------------------


def test_invalid_and_missing_arms_are_excluded_not_imputed(
    protocol: dict[str, Any],
) -> None:
    p = protocol
    records = [
        make_run(p, "t1", "draw-only", 100.0),
        make_run(p, "t1", "gen-only", None),  # agent_error: no score
        make_run(p, "t2", "gen-only", 5.0),  # no draw-only run at all
        make_run(p, "t2", "draw-only", 10.0, replicate=2),  # no gen-only run
    ]
    report = rep.build_report(records, p)
    gen = row(report, "gen-only")
    assert gen["geomean_ratio"] is None  # not 1.0, not an imputed ratio
    assert gen["denominator"]["comparable_pairs"] == 0
    assert {(e["task_id"], e["replicate"], e["reason"]) for e in gen["excluded"]} == {
        ("t1", 1, "arm_agent_error"),
        ("t2", 1, "missing_draw-only"),
        ("t2", 2, "missing_arm"),
    }
    assert row(report, "gen+refine")["denominator"]["attempted_pairs"] == 2


def test_not_run_no_seed_counts_but_is_excluded(protocol: dict[str, Any]) -> None:
    records = [
        make_run(protocol, "t1", "draw-only", 100.0),
        make_run(protocol, "t1", "gen+refine", None, status="not_run_no_seed"),
    ]
    report = rep.build_report(records, protocol)
    refine = row(report, "gen+refine")
    assert refine["geomean_ratio"] is None
    assert refine["excluded"][0]["reason"] == "arm_not_run_no_seed"
    table = next(t for t in report["tables"] if t["arm"] == "gen+refine")
    assert table["runs"] == 1 and table["failures"] == {"not_run_no_seed": 1}


def test_interrupted_attempt_is_history_not_an_outcome(
    protocol: dict[str, Any],
) -> None:
    interrupted = make_run(protocol, "t1", "draw-only", None, status="interrupted")
    final = make_run(protocol, "t1", "draw-only", 100.0)
    final["run_id"] = "t1.draw-only.a1.r1.a2"
    report = rep.build_report([interrupted, final], protocol)
    assert report["totals"]["interrupted_attempts"] == 1
    assert report["totals"]["counted_runs"] == 1
    table = report["tables"][0]
    assert (table["runs"], table["valid_runs"], table["failures"]) == (1, 1, {})


def test_corrupted_scored_record_is_rejected_not_scored(
    protocol: dict[str, Any],
) -> None:
    good = make_run(protocol, "t1", "draw-only", 100.0)
    bad = make_run(protocol, "t1", "gen-only", 10.0)
    bad["validity"]["drc"]["status"] = "violations"  # valid status, failing DRC
    report = rep.build_report([good, bad], protocol)
    assert not report["integrity"]["valid"]
    assert report["integrity"]["rejected_records"][0]["run_id"] == bad["run_id"]
    assert row(report, "gen-only")["geomean_ratio"] is None
    assert report["totals"]["rejected"] == 1


def test_duplicate_finished_records_are_surfaced(protocol: dict[str, Any]) -> None:
    first = make_run(protocol, "t1", "draw-only", 100.0)
    second = copy.deepcopy(first)
    second["run_id"] = "t1.draw-only.a1.r1.a2"
    report = rep.build_report([first, second], protocol)
    assert not report["integrity"]["valid"]
    assert report["tables"] == []


# --------------------------------------------------------------------------
# Fingerprints, versions and compatibility
# --------------------------------------------------------------------------


def test_task_fingerprint_change_is_visible_and_blocks_pairing(
    protocol: dict[str, Any],
) -> None:
    base = corpus(protocol)
    before = rep.build_report(base, protocol)["provenance"]
    changed = [*base[:1], make_run(protocol, "t1", "gen-only", 50.0, task_sha="9" * 64)]
    after = rep.build_report(changed, protocol)
    assert after["provenance"]["task_set_fingerprint"] != before["task_set_fingerprint"]
    assert not before["mixed_fingerprints"]
    assert row(after, "gen-only")["excluded"][0]["reason"] == "fingerprint_mismatch"
    assert after["provenance"]["mixed_fingerprints"]


def test_task_set_fingerprint_is_order_independent() -> None:
    a = rep.task_set_fingerprint({"x": {"1"}, "y": {"2"}})
    assert a == rep.task_set_fingerprint({"y": {"2"}, "x": {"1"}})
    assert a != rep.task_set_fingerprint({"x": {"1"}})


def test_provenance_lists_versions_agents_seeds_and_timestamps(
    protocol: dict[str, Any],
) -> None:
    records = corpus(protocol)
    records.append(make_run(protocol, "t2", "draw-only", 12.0, agent="a2", klt="0.2.0"))
    times = {
        records[0]["run_id"]: {"started_at": "2026-10-18T01:00:00+00:00",
                               "finished_at": "2026-10-18T01:05:00+00:00"},
        records[1]["run_id"]: {"started_at": "2026-10-18T01:06:00+00:00",
                               "finished_at": "2026-10-18T01:09:00+00:00"},
    }  # fmt: skip
    report = rep.build_report(records, protocol, times)
    prov = report["provenance"]
    assert prov["versions"]["klt"] == ["0.1.0", "0.2.0"]
    assert prov["versions"]["protocol"] == [protocol["protocol_version"]]
    assert [a["id"] for a in prov["agents"]] == ["a1", "a2"]
    assert prov["protocol_sha256"] == [layout.document_sha256(protocol)]
    assert prov["first_started_at"] == "2026-10-18T01:00:00+00:00"
    assert prov["last_finished_at"] == "2026-10-18T01:09:00+00:00"
    refine = next(r for r in report["runs"] if r["arm"] == "gen+refine")
    assert refine["seed_from"]["gds_sha256"] == GDS and refine["seed_sha256"]
    assert refine["started_at"] is None  # no journal entry for this run


def test_phase1_score_fields_and_sim_margin_prerequisite(
    protocol: dict[str, Any],
) -> None:
    records = corpus(protocol)
    report = rep.build_report(records, protocol)
    assert report["score"]["primary"] == "area_um2"
    assert report["score"]["sim_margin"]["reported"] is False
    assert "#217" in report["score"]["sim_margin"]["prerequisite"]

    records[0]["score"].update(utilization=0.5, sim_margin=0.25)
    report = rep.build_report(records, protocol)
    assert report["score"]["sim_margin"] == {"reported": True, "prerequisite": None}
    t1 = next(
        t for t in report["tables"] if (t["task_id"], t["arm"]) == ("t1", "draw-only")
    )
    assert t1["utilization_mean"] == 0.5 and t1["sim_margin_mean"] == 0.25


def test_existing_consumers_stay_compatible(protocol: dict[str, Any]) -> None:
    """The report only reads record fields; it adds none to the ledger schema
    (`versions.pdk` is an additive string in an open object), and the round
    ledger / pass@k schemas are untouched."""
    schema_dir = REPO_ROOT / "benchmarks" / "design-agent" / "schema"
    run_schema = json.loads((schema_dir / "layout-run.schema.json").read_text())
    assert run_schema["properties"]["versions"]["additionalProperties"] == {
        "type": "string"
    }
    record = make_run(protocol, "t1", "draw-only", 1.0)
    record["versions"]["pdk"] = "sky130"
    assert layout.validate_run(record, protocol)["valid"]


# --------------------------------------------------------------------------
# Reproducibility from ledgers alone, and the CLI
# --------------------------------------------------------------------------


def write_ledgers(out: Path, records: list[dict[str, Any]]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    journal = []
    for i, r in enumerate(records):
        journal.append({"event": "start", "ts": f"2026-10-18T00:{i:02d}:00+00:00",
                        "identity": {"run_id": r["run_id"]}})  # fmt: skip
        journal.append({"event": "finish", "ts": f"2026-10-18T00:{i:02d}:30+00:00",
                        "run_id": r["run_id"], "status": r["status"]})  # fmt: skip
    (out / "journal.jsonl").write_text("".join(json.dumps(e) + "\n" for e in journal))


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "report-layout", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_report_regenerates_byte_identically_with_no_sandboxes(
    protocol: dict[str, Any], tmp_path: Path
) -> None:
    out = tmp_path / "ledgers"
    write_ledgers(out, corpus(protocol))
    # Only the two ledgers exist: no runs/<id>/ sandbox, artifact or score dir.
    assert sorted(p.name for p in out.iterdir()) == ["journal.jsonl", "runs.jsonl"]
    first = run_cli("--out-dir", str(out))
    again = run_cli("--out-dir", str(out))
    assert first.returncode == 0, first.stderr
    assert first.stdout == again.stdout
    report = json.loads(first.stdout)
    assert report["schema"] == rep.REPORT_SCHEMA
    assert report["provenance"]["first_started_at"] == "2026-10-18T00:00:00+00:00"
    assert row(report, "gen-only")["geomean_ratio"] == 1.0
    assert rep.to_json(rep.report_from_dir(out, protocol)) == first.stdout


def test_cli_markdown_and_output_file(protocol: dict[str, Any], tmp_path: Path) -> None:
    out = tmp_path / "ledgers"
    write_ledgers(out, corpus(protocol))
    target = tmp_path / "report.md"
    result = run_cli("--out-dir", str(out), "--format", "markdown", "-o", str(target))
    assert result.returncode == 0, result.stderr
    text = target.read_text()
    assert "| gen-only | a1 | 1.0 | 2 | 1 | 3 |" in text
    assert "no AHRR implementation code is copied" in text
    assert "Task-set fingerprint" in text and "#217" in text


def test_cli_fails_on_integrity_error_and_corrupt_ledger(
    protocol: dict[str, Any], tmp_path: Path
) -> None:
    bad = make_run(protocol, "t1", "gen-only", 10.0)
    bad["seed_sha256"] = "0" * 64
    out = tmp_path / "bad"
    write_ledgers(out, [bad])
    assert run_cli("--out-dir", str(out)).returncode == 1

    torn = tmp_path / "torn"
    write_ledgers(torn, [make_run(protocol, "t1", "draw-only", 1.0)])
    with (torn / "runs.jsonl").open("a") as handle:
        handle.write('{"run_id": ')  # torn final line is ignored
    assert run_cli("--out-dir", str(torn)).returncode == 0
    with (torn / "runs.jsonl").open("a") as handle:
        handle.write(
            "\n" + json.dumps(make_run(protocol, "t1", "gen-only", 1.0)) + "\n"
        )
    corrupt = run_cli("--out-dir", str(torn))
    assert corrupt.returncode == 1 and "corrupt ledger line" in corrupt.stderr


def test_reporting_module_never_touches_artifacts_or_simulators() -> None:
    source = (SCRIPTS_DIR / "design_agent_benchmark_layout_report.py").read_text()
    for forbidden in ("subprocess", "klayout", "artifact.gds", "datetime", "time.time"):
        assert forbidden not in source
