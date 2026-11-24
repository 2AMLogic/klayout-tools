"""Offline failure-gate tests; real pinned layouts are exercised by the CI job."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import time
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GDS_ERROR = "HTTP Error 503: Backend.max_conn reached"
FETCHED = {"artifacts": {"gds": {"url": "https://example.org/a.oas", "sha256": "abc"}}}
UNFETCHED = {
    "artifacts": {"gds": {"url": "https://example.org/a.oas", "error": GDS_ERROR}}
}


def load_sample():
    return json.loads((ROOT / "tests/corpus/tt_gf0p2_drc_sample.json").read_text())


def write_manifest(cache, sample, macro, manifest):
    """Write ``manifest`` for one design: a dict, raw text, or ``None`` for none."""
    if manifest is None:
        return
    path = cache / "manifests" / sample["id"] / f"{macro}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest if isinstance(manifest, str) else json.dumps(manifest))


def stage_run(results, sample, cache):
    """Lay out what ``tt_corpus_run.py`` leaves behind.

    Like ``resolve_layout``, a design whose manifest has no layout is recorded
    ``skipped`` with no envelope; every other design passes.
    """
    records = []
    for project in sample["projects"]:
        macro = project["macro"]
        manifest = cache / "manifests" / sample["id"] / f"{macro}.json"
        record = {
            "macro": macro,
            "shuttle": sample["id"],
            "stage": "drc",
            "outcome": "skipped",
            "exit_code": None,
            "duration_s": 0.0,
        }
        if (json.loads(manifest.read_text())["artifacts"]["gds"] or {}).get("sha256"):
            directory = results / sample["id"] / macro
            directory.mkdir(parents=True)
            (directory / "drc.envelope.json").write_text(
                json.dumps({"status": "clean", "violation_count": 0})
            )
            record.update(outcome="pass", exit_code=0, duration_s=51.2)
        records.append(record)
    (results.parent / "run.json").write_text(json.dumps({"records": records}))


def stub_scripts(monkeypatch, sample_tool, on_fetch, on_run):
    """Replace both subprocesses with ``on_fetch(attempt)`` and ``on_run()``."""
    calls = []

    def fake(command, **kwargs):
        name = Path(command[1]).name
        calls.append(name)
        if name == "tt_corpus_fetch.py":
            on_fetch(calls.count(name))
        else:
            on_run()

    monkeypatch.setattr(sample_tool.subprocess, "run", fake)
    return calls


@pytest.fixture
def sample_tool():
    spec = importlib.util.spec_from_file_location(
        "tt_corpus_sample", ROOT / "scripts/tt_corpus_sample.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def result(tmp_path):
    sample = json.loads((ROOT / "tests/corpus/tt_gf0p2_drc_sample.json").read_text())
    records = []
    for project in sample["projects"]:
        macro = project["macro"]
        directory = tmp_path / sample["id"] / macro
        directory.mkdir(parents=True)
        (directory / "drc.envelope.json").write_text(
            json.dumps({"status": "clean", "violation_count": 0})
        )
        records.append(
            {
                "macro": macro,
                "shuttle": sample["id"],
                "stage": "drc",
                "outcome": "pass",
                "exit_code": 0,
                "duration_s": 51.2,
                "envelope_path": "drc.envelope.json",
            }
        )
    return sample, {"records": records}, tmp_path


def test_clean_sample_prints_each_design_time(sample_tool, result, capsys):
    sample_tool.verify(*result)
    output = capsys.readouterr().out
    for project in result[0]["projects"]:
        assert f"{project['macro']}: pass, 51.2 s" in output


@pytest.mark.parametrize(
    "outcome", ["findings", "error", "crash", "timeout", "skipped"]
)
def test_every_non_pass_fails(sample_tool, result, outcome):
    result[1]["records"][0]["outcome"] = outcome
    with pytest.raises(ValueError, match="DRC did not pass"):
        sample_tool.verify(*result)


@pytest.mark.parametrize("change", ["missing", "duplicate", "extra", "wrong_stage"])
def test_exact_sample_must_run(sample_tool, result, change):
    records = result[1]["records"]
    if change == "missing":
        records.pop()
    elif change == "duplicate":
        records.append(deepcopy(records[0]))
    elif change == "extra":
        records.append({**records[0], "macro": "unexpected"})
    else:
        records[0]["stage"] = "extract"
    with pytest.raises(ValueError, match="sample records"):
        sample_tool.verify(*result)


@pytest.mark.parametrize(
    "envelope",
    [
        {"status": "violations", "violation_count": 2},
        {"status": "clean", "violation_count": 1},
        {"status": "clean_partial", "violation_count": 0},
        {"status": "not_checked", "violation_count": 0},
        {"status": "clean", "violation_count": 0, "error": "failed"},
        {},
    ],
)
def test_exit_zero_alone_is_insufficient(sample_tool, result, envelope):
    sample, summary, directory = result
    record = summary["records"][0]
    (directory / sample["id"] / record["macro"] / "drc.envelope.json").write_text(
        json.dumps(envelope)
    )
    with pytest.raises(ValueError, match="not clean"):
        sample_tool.verify(*result)


def test_nonzero_tool_exit_fails(sample_tool, result):
    result[1]["records"][0]["exit_code"] = 3
    with pytest.raises(ValueError, match="DRC did not pass"):
        sample_tool.verify(*result)


@pytest.mark.parametrize(
    "manifest, reason",
    [
        (FETCHED, None),
        (UNFETCHED, GDS_ERROR),
        ({"artifacts": {"gds": None}}, "the fetch recorded no GDS"),
        (
            {"excluded": True, "exclusion_reason": "source fetch failed: 503"},
            "source fetch failed: 503",
        ),
        (None, "no readable manifest"),
        ("{not json", "no readable manifest"),
    ],
    ids=["fetched", "fetch-error", "no-gds", "excluded", "no-manifest", "bad-json"],
)
def test_missing_gds_reports_why_a_design_has_no_layout(
    sample_tool, tmp_path, manifest, reason
):
    sample = load_sample()
    first, second = [project["macro"] for project in sample["projects"]]
    write_manifest(tmp_path, sample, first, manifest)
    write_manifest(tmp_path, sample, second, FETCHED)
    missing = {} if reason is None else {first: reason}
    assert sample_tool._missing_gds(sample, tmp_path) == missing


def test_clean_fetch_runs_each_script_once(sample_tool, tmp_path, monkeypatch):
    output = tmp_path / "out"
    sample = load_sample()
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)

    def fetch(attempt):
        for project in sample["projects"]:
            write_manifest(output / "cache", sample, project["macro"], FETCHED)

    calls = stub_scripts(
        monkeypatch,
        sample_tool,
        fetch,
        lambda: stage_run(output / "results", sample, output / "cache"),
    )
    assert sample_tool.main(["--output-dir", str(output)]) == 0
    assert calls == ["tt_corpus_fetch.py", "tt_corpus_run.py"]
    assert sleeps == []


def test_transient_fetch_error_is_retried(sample_tool, tmp_path, monkeypatch, capsys):
    output = tmp_path / "out"
    sample = load_sample()
    clean, flaky = [project["macro"] for project in sample["projects"]]
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)

    def fetch(attempt):
        write_manifest(output / "cache", sample, clean, FETCHED)
        write_manifest(
            output / "cache", sample, flaky, FETCHED if attempt > 1 else UNFETCHED
        )

    calls = stub_scripts(
        monkeypatch,
        sample_tool,
        fetch,
        lambda: stage_run(output / "results", sample, output / "cache"),
    )
    assert sample_tool.main(["--output-dir", str(output)]) == 0
    assert calls == ["tt_corpus_fetch.py"] * 2 + ["tt_corpus_run.py"]
    logged = capsys.readouterr().out
    assert f"{flaky}: {GDS_ERROR}" in logged
    assert "retrying" in logged
    assert sleeps == [sample_tool.RETRY_DELAY_S]


@pytest.mark.parametrize("unfetched", [[1], [0, 1]], ids=["one-design", "every-design"])
@pytest.mark.parametrize(
    "manifest, recorded",
    [(UNFETCHED, GDS_ERROR), (None, "no readable manifest")],
    ids=["fetch-error", "no-manifest"],
)
def test_exhausted_retries_is_an_infrastructure_error(
    sample_tool, tmp_path, monkeypatch, capsys, unfetched, manifest, recorded
):
    output = tmp_path / "out"
    sample = load_sample()
    macros = [project["macro"] for project in sample["projects"]]
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)

    def fetch(attempt):
        for index, macro in enumerate(macros):
            write_manifest(
                output / "cache",
                sample,
                macro,
                FETCHED if index not in unfetched else manifest,
            )

    calls = stub_scripts(
        monkeypatch,
        sample_tool,
        fetch,
        lambda: stage_run(output / "results", sample, output / "cache"),
    )
    assert sample_tool.main(["--output-dir", str(output)]) == 1
    assert calls == ["tt_corpus_fetch.py"] * sample_tool.MAX_FETCH_ATTEMPTS
    error = capsys.readouterr().err
    assert "infrastructure" in error
    assert "DRC did not pass" not in error
    for index in unfetched:
        assert f"{macros[index]}: {recorded}" in error
    assert sleeps == [sample_tool.RETRY_DELAY_S * attempt for attempt in (1, 2)]


@pytest.mark.parametrize("failed_script", ["tt_corpus_fetch.py", "tt_corpus_run.py"])
def test_nonzero_fetch_or_runner_fails(
    sample_tool, tmp_path, monkeypatch, failed_script
):
    calls = []
    sample = load_sample()
    for project in sample["projects"]:
        write_manifest(tmp_path / "cache", sample, project["macro"], FETCHED)

    def fail(command, **kwargs):
        calls.append(command)
        if Path(command[1]).name == failed_script:
            raise subprocess.CalledProcessError(2, command)

    monkeypatch.setattr(sample_tool.subprocess, "run", fail)
    assert sample_tool.main(["--output-dir", str(tmp_path)]) == 1
    assert Path(calls[-1][1]).name == failed_script
