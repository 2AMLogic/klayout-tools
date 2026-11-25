"""Offline GF180 corpus shard/collection contract, including malformed fleet output."""

import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

COMMIT = "a" * 40


@pytest.fixture
def batch(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import tt_corpus_batch

    return tt_corpus_batch


def test_pinned_population_and_runtime_budget(batch):
    rows = batch.catalog()
    plan = batch.plan(rows, 4, 300, 7200, 900)
    assert len(rows) == 108
    assert sum(shard["slots"] for shard in plan) == 108
    assert sum(shard["eligible"] for shard in plan) == 82
    assert max(shard["seconds"] for shard in plan) == 7200
    assert [sum(r["shuttle"] == s for r in rows) for s in batch.SHUTTLES] == [
        24,
        52,
        32,
    ]
    assert len({(r["shuttle"], r["address"]) for r in rows}) == 108
    with pytest.raises(ValueError, match="required"):
        batch.plan(rows, 3, 300, 7200, 900)


def fake_klt(argv, timeout):
    if argv[1] == "--version":
        return 0, "klt test-version", ""
    assert Path(argv[2]).read_bytes() == b"layout"
    if argv[1] == "extract":
        raise subprocess.TimeoutExpired(argv, timeout)
    return 0, '{"status":"clean"}', ""


@pytest.fixture
def fleet(batch, tmp_path, monkeypatch):
    """Two groups re-use physical s00, with two shuttle slots sharing a macro."""
    monkeypatch.setenv("RS_SOURCE_COMMIT", COMMIT)
    rows = []
    for shuttle in batch.SHUTTLES:
        for name in ("alpha", "beta"):
            row = {
                "shuttle": shuttle,
                "macro": name,
                "repo": "https://github.com/example/design",
                "commit": "1" * 40,
                "spdx": "Apache-2.0",
                "license_file": "LICENSE",
                "license_blob": "f" * 40,
                "layout_sha256": ""
                if shuttle == "ttgf0p1"
                else hashlib.sha256(b"layout").hexdigest(),
            }
            rows.append(row)
    groups = []
    for index in range(2):
        group = tmp_path / f"group{index}"
        groups.append(group)
        job = {"job": group.name + "-s00", "ok": True, "state": "done"}
        folder = group / "s00"
        batch.write_json(group / "group.json", {"group": group.name, "shards": [job]})
        batch.write_json(
            folder / "run.json",
            {
                "kind": "repo-script",
                "repo": "2AMLogic/klayout-tools",
                "script": batch.SCRIPT,
                "shard": "s00",
                "commit": COMMIT,
                "exit_code": 0,
                "outputs": {"found": [str(batch.OUTPUT)], "missing": []},
                "input_sha256": {"source_tarball": "b" * 64},
            },
        )
        batch.run_shard(
            rows,
            (index, 2),
            300,
            folder / "files" / batch.OUTPUT,
            lambda _: b"layout",
            runner=fake_klt,
        )
    return rows, groups


def test_two_groups_merge_outcomes_and_attribution(batch, fleet, tmp_path):
    rows, groups = fleet
    out = tmp_path / "merged"
    batch.collect(rows, groups, 2, COMMIT, out)
    summary = batch.read_json(out / "summary.json")
    assert sum(s["records"] for s in summary.values()) == 12
    assert summary["ttgf0p1"]["outcomes"]["drc"] == {"skipped": 2}
    assert summary["ttgf0p3"]["outcomes"]["extract"] == {"timeout": 2}
    assert summary["ttgf0p2"]["outcomes"]["drc"] == {"pass": 2}
    example = batch.read_json(out / "ttgf0p2.json")["clusters"][0]["exemplars"][0]
    assert example["attribution"]["license"] == "Apache-2.0"
    assert COMMIT in example["reproducer"]
    assert len(batch.read_json(out / "jobs.json")) == 2
    assert not list(out.rglob("work"))


@pytest.mark.parametrize(
    "fields",
    [
        {"macro": "beta"},
        {"outcome": "skipped"},
        {"tool_version": "other"},
        {"timeout_s": 3},
        {"envelope_path": "../unrelated.json"},
    ],
)
def test_invalid_stage_record(batch, fleet, tmp_path, fields):
    rows, groups = fleet
    rec = groups[0] / "s00/files" / batch.OUTPUT / "records/ttgf0p2/alpha/drc.json"
    batch.write_json(rec, {**batch.read_json(rec), **fields})
    with pytest.raises(ValueError):
        batch.collect(rows, groups, 2, COMMIT, tmp_path / "merged")
    assert not (tmp_path / "merged").exists()


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "extra",
        "missing_shard",
        "duplicate",
        "wrong_source",
        "envelope",
        "job_failed",
    ],
)
def test_incomplete_or_mixed_runs_are_rejected(batch, fleet, tmp_path, damage):
    rows, groups = fleet
    payload = groups[0] / "s00/files" / batch.OUTPUT
    rec = payload / "records/ttgf0p2/alpha/drc.json"
    out = tmp_path / "merged"
    if damage == "missing":
        rec.unlink()
    elif damage == "extra":
        shutil.copyfile(rec, rec.with_name("other.json"))
    elif damage == "missing_shard":
        groups.pop()
    elif damage == "duplicate":
        groups.append(groups[0])
    elif damage == "wrong_source":
        meta = payload / "shard.json"
        batch.write_json(meta, dict(batch.read_json(meta), commit="0" * 40))
    elif damage == "envelope":
        rec.with_name("drc.envelope.json").unlink()
    elif damage == "job_failed":
        info = groups[0] / "group.json"
        doc = json.loads(info.read_text())
        doc["shards"][0]["ok"] = False
        batch.write_json(info, doc)
    with pytest.raises((ValueError, FileNotFoundError)):
        batch.collect(rows, groups, 2, COMMIT, out)
    assert not out.exists()


def test_reject_changed_bytes_before_simulation(batch):
    row = next(
        r
        for r in batch.catalog()
        if r["macro"] != "tt_um_chip_rom" and not batch.skip_reason(r)
    )
    with pytest.raises(ValueError, match="pinned SHA-256"):
        batch.layout_bytes(row, lambda _: b"changed")


@pytest.mark.skipif(sys.platform == "win32", reason="the Spot worker is POSIX")
def test_timed_out_engine_cannot_outlive_klt(batch, tmp_path):
    child_pid = tmp_path / "engine.pid"
    script = (
        "import pathlib, subprocess, sys, time\n"
        "sleeper = [sys.executable, '-c', 'import time; time.sleep(10)']\n"
        "child = subprocess.Popen(sleeper)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
        "time.sleep(10)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        batch.run_tool([sys.executable, "-c", script, str(child_pid)], 1)
    pid = child_pid.read_text()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = subprocess.run(
            ["ps", "-o", "stat=", "-p", pid], capture_output=True, text=True
        )
        if not status.stdout.strip() or status.stdout.lstrip().startswith("Z"):
            return
        time.sleep(0.05)
    pytest.fail("timed-out stage left its engine running")
