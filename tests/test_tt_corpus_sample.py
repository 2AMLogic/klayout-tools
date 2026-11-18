"""Offline failure-gate tests; real pinned layouts are exercised by the CI job."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


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


@pytest.mark.parametrize("failed_script", ["tt_corpus_fetch.py", "tt_corpus_run.py"])
def test_nonzero_fetch_or_runner_fails(
    sample_tool, tmp_path, monkeypatch, failed_script
):
    calls = []

    def fail(command, **kwargs):
        calls.append(command)
        if Path(command[1]).name == failed_script:
            raise subprocess.CalledProcessError(2, command)

    monkeypatch.setattr(sample_tool.subprocess, "run", fail)
    assert sample_tool.main(["--output-dir", str(tmp_path)]) == 1
    assert Path(calls[-1][1]).name == failed_script
