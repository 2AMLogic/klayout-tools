"""Pair identity, count admission and pinned synthesis for the tt06 sweep."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from test_tt_corpus_baseline import DECK, PROJECTS, cache_project

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sweep(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import tt_corpus_optimize

    return tt_corpus_optimize


def make_pair(directory, macro, candidate_count=92, baseline_count=100):
    row = {
        "macro": macro,
        "commit": "a" * 40,
        "baseline_count": baseline_count,
        "candidate_count": candidate_count,
        "top": macro,
    }
    for side in ("baseline", "candidate"):
        path = directory / f"{macro}-{side}.v"
        path.write_text(f"// {macro}: {side}\n")
        row[f"{side}_netlist"] = path.name
        row[f"{side}_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        evidence = directory / f"{macro}-{side}.json"
        evidence.write_text(
            json.dumps(
                {
                    "instance_count": row[f"{side}_count"],
                    "netlist_sha256": row[f"{side}_sha256"],
                }
            )
        )
        row[f"{side}_report"] = evidence.name
    return row


def proof(row, verdict="equivalent", reason=None):
    keys = (
        "macro",
        "commit",
        "top",
        "baseline_sha256",
        "candidate_sha256",
        "baseline_count",
        "candidate_count",
    )
    return {
        **{k: row[k] for k in keys},
        "liberty_sha256": DECK["content_hash"].removeprefix("sha256:"),
        "proof": {
            "status": "equivalent" if verdict == "equivalent" else "inconclusive"
        },
        "verdict": verdict,
        "exclusion_reason": reason,
    }


def report(sweep, tmp_path, rows, proofs):
    pairs = tmp_path / "pairs.json"
    pairs.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "shuttle": "tt06",
                "pins": {"deck": DECK},
                "optimization": {},
                "total": len(rows),
                "designs": rows,
            }
        )
    )
    verdicts = tmp_path / "verdicts.json"
    verdicts.write_text(json.dumps({"designs": proofs}))
    output = tmp_path / "results.json"
    assert (
        sweep.main(
            [
                "report",
                "--pairs",
                str(pairs),
                "--verdicts",
                str(verdicts),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    return json.loads(output.read_text())


def test_signed_deltas_and_every_exclusion_once(sweep, tmp_path):
    rows = [
        make_pair(tmp_path, name, count)
        for name, count in [
            ("smaller", 92),
            ("unchanged", 100),
            ("larger", 103),
            ("no_proof", 0),
            ("mismatch", 5),
            ("unknown", 5),
        ]
    ]
    proofs = [proof(row) for row in rows[:3]] + [
        proof(rows[4], "not-equivalent", "output mismatch"),
        proof(rows[5], "unestablished", "proof timed out"),
    ]
    rows.extend(
        [
            {
                "macro": "failed",
                "commit": "b",
                "baseline_count": 100,
                "exclusion_reason": "candidate mapping failed",
            },
            {
                "macro": "ingest",
                "commit": "c",
                "exclusion_reason": "source unavailable",
            },
        ]
    )
    result = report(sweep, tmp_path, rows, proofs)
    assert (result["total"], result["reported"], result["excluded"]) == (8, 3, 5)
    designs = result["designs"]
    assert [r["macro"] for r in designs] == [r["macro"] for r in rows]
    assert [r["optimized_count"] for r in designs[:3]] == [92, 100, 103]
    assert [r["delta"] for r in designs[:3]] == [-8, 0, 3]
    for actual, expected in zip(
        designs[3:],
        [
            "missing",
            "output mismatch",
            "proof timed out",
            "candidate mapping failed",
            "source unavailable",
        ],
        strict=True,
    ):
        assert expected in actual["exclusion_reason"]
        assert not {"candidate_count", "optimized_count", "delta"} & actual.keys()
    assert designs[-2]["baseline_count"] == 100
    assert "baseline_count" not in designs[-1]


@pytest.mark.parametrize("side", ["baseline", "candidate"])
def test_replaced_netlist_with_old_proof(sweep, tmp_path, side):
    row = make_pair(tmp_path, "rerun")
    old = proof(row)
    path = tmp_path / row[f"{side}_netlist"]
    path.write_bytes(b"new mapped netlist")
    # A cached manifest must not hide an in-place edit.
    assert report(sweep, tmp_path, [row], [old])["excluded"] == 1
    # A fresh sweep rehashes the artifact, but the old proof is still stale.
    row[f"{side}_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    report_file = tmp_path / row[f"{side}_report"]
    measured = json.loads(report_file.read_text())
    report_file.write_text(
        json.dumps({**measured, "netlist_sha256": row[f"{side}_sha256"]})
    )
    result = report(sweep, tmp_path, [row], [old])
    assert "optimized_count" not in result["designs"][0]
    # The checker may retain verdicts from multiple runs. Only this pair counts.
    assert report(sweep, tmp_path, [row], [old, proof(row)])["reported"] == 1
    assert (
        report(sweep, tmp_path, [row], [proof(row), proof(row, "not-equivalent")])[
            "excluded"
        ]
        == 1
    )


def test_missing_verdicts_file_preserves_baselines(sweep, tmp_path):
    row = make_pair(tmp_path, "pending")
    report(sweep, tmp_path, [row], [])
    result = sweep.results(tmp_path / "pairs.json", tmp_path / "absent.json")
    assert result["reported"] == 0
    assert result["designs"][0]["baseline_count"] == 100
    assert "missing" in result["designs"][0]["exclusion_reason"]


@pytest.mark.parametrize(
    "changes",
    [
        {"liberty_sha256": "f" * 64},
        {"top": "leaf"},
        {"proof": {"status": "inconclusive"}},
        {"baseline_count": 20},
        {"candidate_count": 50},
        {"exclusion_reason": "invalid count in gate input"},
    ],
)
def test_gate_evidence_must_match_current_measurement(sweep, tmp_path, changes):
    row = make_pair(tmp_path, "same_bytes")
    result = report(sweep, tmp_path, [row], [{**proof(row), **changes}])
    assert result["excluded"] == 1
    assert "delta" not in result["designs"][0]


def test_absolute_delta_for_proven_zero_baseline(sweep, tmp_path):
    row = make_pair(tmp_path, "empty", candidate_count=2, baseline_count=0)
    checked = proof(row, reason="percentage undefined for a zero baseline")
    del checked["candidate_count"]  # Phase 2.C omits the percentage and this count.
    result = report(sweep, tmp_path, [row], [checked])
    assert result["reported"] == 1
    assert result["designs"][0]["delta"] == 2


def test_unestablished_before_gate_could_hash_inputs(sweep, tmp_path):
    row = make_pair(tmp_path, "unreadable")
    unavailable = {
        "macro": row["macro"],
        "commit": row["commit"],
        "verdict": "unestablished",
        "exclusion_reason": "Liberty differs from the pinned baseline deck",
    }
    result = report(sweep, tmp_path, [row], [unavailable])
    assert "Liberty differs" in result["designs"][0]["exclusion_reason"]
    assert "delta" not in result["designs"][0]


def test_count_must_come_from_this_netlists_report(sweep, tmp_path):
    row = make_pair(tmp_path, "wrong_count")
    row["candidate_count"] = 200
    result = report(sweep, tmp_path, [row], [proof(row)])
    assert result["excluded"] == 1
    assert "candidate" in result["designs"][0]["exclusion_reason"]
    assert "delta" not in result["designs"][0]


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ("mapping", "ABC mapping failed"),
        ("pin", "pins"),
        ("count", "pinned instance_count"),
        ("script", "ABC script"),
        ("digest", "netlist"),
    ],
)
def test_prepare_keeps_reports_and_only_changes_mapping(
    sweep, tmp_path, monkeypatch, failure, reason
):
    inputs = []
    for project in PROJECTS[:3]:
        manifest = json.loads(cache_project(tmp_path, project).read_text())
        inputs.append(
            {
                **project,
                "source_sha256": manifest["artifacts"]["sources"]["sha256"],
                "license": manifest["license"],
                "instance_count": 100,
            }
        )
    inputs.append({**PROJECTS[3], "exclusion_reason": "upstream exclusion"})
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "shuttle": "tt06",
                "pins": {"version": "pinned"},
                "total": len(inputs),
                "designs": inputs,
            }
        )
    )
    requests = {}

    def synthesize(work, top):
        request = json.loads((work / "request.json").read_text())
        assert request["sources"] == ["src/nested/leaf.v", "src/top.v"]
        candidate = "synthesis" in request
        middle = top == PROJECTS[1]["macro"]
        abc = None
        if candidate:
            assert request["synthesis"] == {"abc_script": "area.abc"}
            abc = {
                "path": "area.abc",
                "sha256": hashlib.sha256((work / "area.abc").read_bytes()).hexdigest(),
            }
            assert {k: v for k, v in request.items() if k != "synthesis"} == requests[
                top
            ]
            if middle and failure == "mapping":
                raise ValueError("ABC mapping failed")
        else:
            requests[top] = request
        out = work / ".klt/synthesize/run1"
        out.mkdir(parents=True)
        netlist = out / f"{top}_synth.v"
        netlist.write_text(f"// mapped {top} {candidate}")
        count = 92 if candidate else 100
        if middle and failure == "count" and not candidate:
            count = 99
        broken = middle and candidate
        digest = hashlib.sha256(netlist.read_bytes()).hexdigest()
        return {
            "run_id": "run1",
            "instance_count": count,
            "netlist_sha256": "old" if broken and failure == "digest" else digest,
            "pins": {"version": "drift" if broken and failure == "pin" else "pinned"},
            "synthesis": {
                "abc_script": None if broken and failure == "script" else abc
            },
        }

    monkeypatch.setattr(sweep.baseline, "synthesize", synthesize)
    monkeypatch.setattr(sweep.baseline, "report_pins", lambda r: r["pins"])
    run = tmp_path / "sweep"
    assert (
        sweep.main(
            [
                "prepare",
                "--baseline",
                str(snapshot),
                "--cache-dir",
                str(tmp_path),
                "--work-dir",
                str(run),
            ]
        )
        == 0
    )
    rows = json.loads((run / "pairs.json").read_text())["designs"]
    assert [r["macro"] for r in rows] == [p["macro"] for p in PROJECTS]
    for row in (rows[0], rows[2]):
        for side in ("baseline", "candidate"):
            contents = (run / row[f"{side}_netlist"]).read_bytes()
            assert row[f"{side}_sha256"] == hashlib.sha256(contents).hexdigest()
            assert (
                json.loads((run / row[f"{side}_report"]).read_text())["instance_count"]
                == row[f"{side}_count"]
            )
    assert reason in rows[1]["exclusion_reason"]
    assert rows[1]["baseline_count"] == 100
    assert rows[3]["exclusion_reason"] == "upstream exclusion"
