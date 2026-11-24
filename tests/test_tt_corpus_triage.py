"""Offline tests for `scripts/tt_corpus_triage.py` (issue #5083, Epic #520
Phase 1.C). Records are hand-written in the shape `tt_corpus_run.py` emits."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "tt_corpus_triage", REPO_ROOT / "scripts" / "tt_corpus_triage.py"
)
tr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tr)

SHUTTLE = "ttgf0p2"


def _err(msg: str) -> str:
    return json.dumps(
        {"schema_version": 1, "error": {"command": "drc", "message": msg}}
    )


def _write(root: Path, macro: str, stage: str, outcome: str, **kw) -> None:
    d = root / "runs" / SHUTTLE / macro
    d.mkdir(parents=True, exist_ok=True)
    rec = {
        "macro": macro,
        "stage": stage,
        "outcome": outcome,
        "tool_version": "klt 9.9",
        "duration_s": kw.pop("duration_s", 1.0),
        "command": ["klt", stage, f"{macro}.gds"],
        **kw,
    }
    env = rec.pop("_envelope", None)
    if env is not None:
        (d / f"{stage}.envelope.json").write_text(json.dumps(env))
        rec["envelope_path"] = f"{stage}.envelope.json"
    (d / f"{stage}.json").write_text(json.dumps(rec))
    m = root / "manifests" / SHUTTLE
    m.mkdir(parents=True, exist_ok=True)
    (m / f"{macro}.json").write_text(
        json.dumps(
            {
                "macro": macro,
                "repo": f"owner/{macro}",
                "commit": "c" * 40,
                "license": {"spdx": "Apache-2.0", "file": "LICENSE"},
            }
        )
    )


def _fixture(root: Path) -> None:
    for i, m in enumerate(["a", "b", "c", "d"]):  # same defect, differing paths/numbers
        _write(
            root,
            m,
            "extract",
            "error",
            exit_code=1,
            duration_s=4 - i,
            stderr_tail=_err(
                f"cell 'TOP{i}' not found in /tmp/x{i}/{m}.gds at line {i}"
            ),
        )
    _write(
        root,
        "e",
        "drc",
        "findings",
        exit_code=1,
        _envelope={"rule_counts": {"M1.1": 3, "V1.2": 1}},
    )
    _write(
        root,
        "f",
        "drc",
        "findings",
        exit_code=1,
        _envelope={"rule_counts": {"V1.2": 9, "M1.1": 40}},
    )
    _write(root, "g", "drc", "timeout", exit_code=None, stderr_tail="timed out")
    _write(root, "h", "drc", "pass", exit_code=0)
    _write(root, "i", "lvs", "skipped", reason="no_reference_netlist")


def test_clusters_by_stable_signature(tmp_path):
    _fixture(tmp_path)
    rep = tr.triage(cache_dir=tmp_path, shuttle=SHUTTLE)
    by_sig = {c["signature"]: c for c in rep["clusters"]}
    assert rep["cluster_count"] == 3  # pass/skipped are not defects
    err = next(c for c in rep["clusters"] if c["outcome"] == "error")
    assert err["count"] == 4 and err["macros"] == ["a", "b", "c", "d"]
    assert "/tmp" not in err["signature"] and "TOP" not in err["signature"]
    drc = by_sig["drc|findings|rule:M1.1,V1.2"]
    assert drc["count"] == 2  # rule set, not counts, is the signature
    assert rep["outcomes"]["drc"] == {"findings": 2, "timeout": 1, "pass": 1}


def test_exemplars_capped_minimal_and_attributed(tmp_path):
    _fixture(tmp_path)
    rep = tr.triage(cache_dir=tmp_path, shuttle=SHUTTLE)
    for c in rep["clusters"]:
        assert 1 <= len(c["exemplars"]) <= 3
        for e in c["exemplars"]:
            a = e["attribution"]
            assert (
                a["repo"] and a["commit"] == "c" * 40 and a["license"] == "Apache-2.0"
            )
            assert "klt" in e["reproducer"]
    err = next(c for c in rep["clusters"] if c["outcome"] == "error")
    assert [e["attribution"]["macro"] for e in err["exemplars"]] == ["d", "c", "b"]


def test_report_never_files_and_is_deterministic(tmp_path, capsys):
    _fixture(tmp_path)
    assert tr.main(["--shuttle", SHUTTLE, "--cache-dir", str(tmp_path)]) == 0
    first = capsys.readouterr().out
    assert tr.main(["--shuttle", SHUTTLE, "--cache-dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out == first
    assert "drafts only" in json.loads(first)["filing"]
    assert (
        tr.main(["--shuttle", SHUTTLE, "--cache-dir", str(tmp_path), "--markdown"]) == 0
    )
    assert "Issue draft" in capsys.readouterr().out


def test_missing_records_exit_2(tmp_path, capsys):
    assert tr.main(["--shuttle", SHUTTLE, "--cache-dir", str(tmp_path)]) == 2
