"""Offline tests for `scripts/tt_corpus_run.py` (issue #5082, Epic #520
Phase 1.B). The runner is injected, so no real `klt` or network is used; the
fixture is a 6-design cache of hand-written manifests."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "tt_corpus_run", REPO_ROOT / "scripts" / "tt_corpus_run.py"
)
rr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rr)

SHUTTLE = "ttgf0p2"


def _make_cache(root: Path) -> Path:
    """Six designs: ok, drc-findings, crash, timeout, excluded, no-gds."""
    (root / "blobs").mkdir(parents=True)
    (root / "manifests" / SHUTTLE).mkdir(parents=True)
    names = ["d_ok", "d_findings", "d_crash", "d_timeout", "d_excluded", "d_nogds"]
    for name in names:
        gds = {"sha256": name, "bytes": 4} if name != "d_nogds" else None
        if gds:
            (root / "blobs" / name).write_bytes(b"\x00\x06\x00\x02")
        manifest = {
            "macro": name,
            "pdk": "gf180mcuD",
            "excluded": name == "d_excluded",
            "exclusion_reason": "no readable license" if name == "d_excluded" else None,
            "artifacts": {"gds": gds},
        }
        (root / "manifests" / SHUTTLE / f"{name}.json").write_text(json.dumps(manifest))
    return root


class FakeKlt:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout_s: float):
        self.calls.append(argv)
        if argv[1] == "--version":
            return 0, "klt 9.9.9\n", ""
        joined = " ".join(argv)
        if "d_crash" in joined:
            return -11, "", "Segmentation fault\n"
        if "d_timeout" in joined:
            raise subprocess.TimeoutExpired(argv, timeout_s)
        if "d_findings" in joined and argv[1] == "drc":
            return 3, json.dumps({"status": "violations", "count": 2}), ""
        return 0, json.dumps({"status": "clean"}), ""


def _run(tmp_path: Path, runner, **kw):
    return rr.run(
        cache_dir=tmp_path / "cache",
        shuttle=SHUTTLE,
        results_dir=tmp_path / "runs",
        stages=("drc", "extract"),
        runner=runner,
        **kw,
    )


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    return _make_cache(tmp_path / "cache")


def _by(summary, macro, stage):
    return next(
        r for r in summary["records"] if r["macro"] == macro and r["stage"] == stage
    )


def test_every_pair_gets_a_record_and_failures_do_not_stop_the_run(tmp_path, cache):
    summary = _run(tmp_path, FakeKlt())
    assert len(summary["records"]) == 6 * 2
    assert _by(summary, "d_ok", "drc")["outcome"] == "pass"
    assert _by(summary, "d_findings", "drc")["outcome"] == "findings"
    assert _by(summary, "d_findings", "extract")["outcome"] == "pass"
    crash = _by(summary, "d_crash", "drc")
    assert crash["outcome"] == "crash" and crash["exit_code"] == -11
    assert "Segmentation" in crash["stderr_tail"]
    timeout = _by(summary, "d_timeout", "extract")
    assert timeout["outcome"] == "timeout" and timeout["exit_code"] is None
    assert _by(summary, "d_excluded", "drc")["outcome"] == "skipped"
    assert _by(summary, "d_nogds", "drc")["reason"] == "no_gds"


def test_records_carry_required_fields(tmp_path, cache):
    summary = _run(tmp_path, FakeKlt())
    for rec in summary["records"]:
        for key in (
            "outcome",
            "exit_code",
            "duration_s",
            "stderr_tail",
            "tool_version",
        ):
            assert key in rec
        assert rec["tool_version"] == "klt 9.9.9"
    on_disk = json.loads(
        (tmp_path / "runs" / SHUTTLE / "d_ok" / "drc.json").read_text()
    )
    assert on_disk["outcome"] == "pass"


def test_rerun_skips_completed_pairs(tmp_path, cache):
    first = FakeKlt()
    _run(tmp_path, first)
    second = FakeKlt()
    summary = _run(tmp_path, second)
    assert summary["ran"] == 0 and summary["resumed"] == 12
    assert [c for c in second.calls if c[1] != "--version"] == []


def test_resume_after_partial_run_and_retry(tmp_path, cache):
    _run(tmp_path, FakeKlt(), limit=2)
    runner = FakeKlt()
    summary = _run(tmp_path, runner)
    macros_run = {
        c[2].rsplit("/", 1)[-1].split(".")[0] for c in runner.calls if c[1] == "drc"
    }
    # sorted order: d_crash and d_excluded were done by the limit=2 run
    assert "d_crash" not in macros_run and "d_findings" in macros_run
    retry = FakeKlt()
    _run(tmp_path, retry, retry=True)
    retried = {
        c[2].rsplit("/", 1)[-1].split(".")[0] for c in retry.calls if c[1] == "drc"
    }
    assert retried == {"d_crash", "d_timeout"}
    assert summary["designs"] == 6


def test_truncated_record_is_redone(tmp_path, cache):
    _run(tmp_path, FakeKlt())
    (tmp_path / "runs" / SHUTTLE / "d_ok" / "drc.json").write_text('{"outc')
    runner = FakeKlt()
    summary = _run(tmp_path, runner)
    assert summary["ran"] == 1


def test_lvs_skipped_without_reference_and_runs_with_one(tmp_path, cache):
    ref = tmp_path / "refs"
    ref.mkdir()
    (ref / "d_ok.spice").write_text(".subckt d_ok\n.ends\n")
    runner = FakeKlt()
    summary = rr.run(
        cache_dir=tmp_path / "cache",
        shuttle=SHUTTLE,
        results_dir=tmp_path / "runs",
        stages=("lvs",),
        reference_dir=ref,
        runner=runner,
    )
    assert _by(summary, "d_ok", "lvs")["outcome"] == "pass"
    assert _by(summary, "d_findings", "lvs")["reason"] == "no_reference_netlist"


def test_unlaunchable_tool_is_a_crash_record(tmp_path, cache):
    def broken(argv, timeout_s):
        raise FileNotFoundError("klt")

    summary = _run(tmp_path, broken, limit=1)
    assert {r["outcome"] for r in summary["records"] if r["macro"] == "d_crash"} == {
        "crash"
    }


def test_main_requires_manifests(tmp_path, capsys):
    code = rr.main(["--shuttle", "nope", "--cache-dir", str(tmp_path)])
    assert code == 2
    assert "tt_corpus_fetch" in capsys.readouterr().out
