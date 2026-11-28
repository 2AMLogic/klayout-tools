"""Offline tests for the tt06 sky130A synthesis baseline."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = json.loads((ROOT / "tests/corpus/tt06_sky130_sample.json").read_text())[
    "projects"
]
DECK = {"name": "sky130_fd_sc_hd__tt_025C_1v80", "content_hash": "sha256:" + "a" * 64}
LICENSE = {"spdx": "Apache-2.0", "file": "LICENSE"}


def cache_project(cache, project, excluded=False, sources=("nested/leaf.v", "top.v")):
    info = {"project": {"top_module": project["macro"], "source_files": list(sources)}}
    files = {
        "info.yaml": yaml.safe_dump(info),
        "src/nested/leaf.v": "module leaf; endmodule",
        "src/top.v": "module top; endmodule",
        "src/scaffold.v": "module top; endmodule",
    }
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, contents in files.items():
            member = tarfile.TarInfo("project-sha/" + name)
            payload = contents.encode()
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    digest = hashlib.sha256(data.getvalue()).hexdigest()
    (cache / "blobs").mkdir(exist_ok=True)
    (cache / "blobs" / digest).write_bytes(data.getvalue())
    path = cache / "manifests/tt06" / (project["macro"] + ".json")
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text(
        json.dumps(
            {
                **project,
                "shuttle": "tt06",
                "pdk": "sky130A",
                "license": LICENSE,
                "excluded": excluded,
                "exclusion_reason": "source unavailable" if excluded else None,
                "artifacts": {"sources": {"sha256": digest}},
            }
        )
    )
    return path


def report(project, deck=DECK):
    return {
        "status": "ok",
        "hdl_toplevel": project["macro"],
        "instance_count": 53,
        "instance_counts_by_type": {"leaf": 5},
        "engine_version": "0.67",
        "provenance": {
            "klt_version": "0.9",
            "deck": deck,
            "pdk": {"name": "sky130A", "version": "open_pdks 123"},
        },
    }


def run_fixture(cache, monkeypatch, responses):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import tt_corpus_baseline as baseline

    monkeypatch.setattr(
        baseline, "tool_builds", lambda: {"yosys": "Yosys 0.67", "abc": "ABC 1.01"}
    )
    dirs = []

    def synthesize(command, *, cwd, **kwargs):
        assert command == [
            "klt",
            "synthesize",
            "request.json",
            "--pdk",
            "sky130A",
            "--format",
            "json",
        ]
        assert cwd not in dirs
        dirs.append(cwd)
        request = json.loads((cwd / "request.json").read_text())
        assert request["sources"] == ["src/nested/leaf.v", "src/top.v"]
        assert all((cwd / src).is_file() for src in request["sources"])
        assert not (cwd / "src/scaffold.v").exists()
        assert request["pdk"] == {
            "cell_library": "sky130_fd_sc_hd",
            "corner": "tt_025C_1v80",
        }
        code, data = responses.pop(request["hdl_toplevel"])
        if code == 1:
            return subprocess.CompletedProcess(command, code, "", json.dumps(data))
        return subprocess.CompletedProcess(command, code, json.dumps(data), "")

    monkeypatch.setattr(baseline.subprocess, "run", synthesize)
    output = cache / "baseline.json"
    assert baseline.main(["--cache-dir", str(cache), "--output", str(output)]) == 0
    assert not responses
    return json.loads(output.read_text())


@pytest.mark.parametrize(
    "code,error",
    [
        (
            1,
            {
                "schema_version": 1,
                "error": {"command": "synthesize", "message": "elaboration failed"},
            },
        ),
        (4, {"status": "inconclusive", "reason": "abc_timeout", "instance_count": 1}),
    ],
)
def test_failure_between_measurements_and_ingest_exclusion(
    tmp_path, monkeypatch, code, error
):
    a, b, c, d = PROJECTS
    for project in PROJECTS:
        cache_project(tmp_path, project, excluded=project == d)
    result = run_fixture(
        tmp_path,
        monkeypatch,
        {
            a["macro"]: (0, report(a)),
            b["macro"]: (code, error),
            c["macro"]: (3, report(c)),
        },
    )
    assert (result["total"], result["measured"], result["excluded"]) == (4, 2, 2)
    assert [{key: row[key] for key in a} for row in result["designs"]] == PROJECTS
    first, failed, later, ingest = result["designs"]
    assert (
        first["instance_count"]
        == later["instance_count"]
        == report(a)["instance_count"]
    )
    assert result["pins"]["deck"] == DECK
    assert all(row["license"] == LICENSE for row in result["designs"])
    assert (error.get("reason") or error["error"]["message"]) in failed[
        "exclusion_reason"
    ]
    assert ingest["exclusion_reason"] == "source unavailable"
    assert "instance_count" not in failed and "instance_count" not in ingest


def test_changed_liberty_missing_manifest_and_wrong_commit(tmp_path, monkeypatch):
    a, b, _, d = PROJECTS
    for project in (a, b):
        cache_project(tmp_path, project)
    cache_project(tmp_path, {**d, "commit": "b" * 40})
    result = run_fixture(
        tmp_path,
        monkeypatch,
        {
            a["macro"]: (0, report(a)),
            b["macro"]: (0, report(b, {**DECK, "content_hash": "sha256:" + "f" * 64})),
        },
    )
    assert result["pins"]["deck"] == DECK
    assert result["measured"] == 1
    for row in result["designs"][1:]:
        assert row["exclusion_reason"] and "instance_count" not in row
    assert "pin" in result["designs"][1]["exclusion_reason"].lower()
    assert result["designs"][-1]["commit"] == d["commit"]


def test_untrusted_source_path_and_corrupt_blob(tmp_path, monkeypatch):
    a, b, *_ = PROJECTS
    cache_project(tmp_path, a, sources=["../escape.v"])
    manifest = json.loads(cache_project(tmp_path, b).read_text())
    (tmp_path / "blobs" / manifest["artifacts"]["sources"]["sha256"]).write_bytes(
        b"bad"
    )
    result = run_fixture(tmp_path, monkeypatch, {})
    assert result["excluded"] == result["total"] == 4
    assert "source" in result["designs"][0]["exclusion_reason"].lower()
    assert "sha256" in result["designs"][1]["exclusion_reason"].lower()


def test_malformed_report_leaves_later_measurement_intact(tmp_path, monkeypatch):
    first, second, *_ = PROJECTS
    cache_project(tmp_path, first)
    cache_project(tmp_path, second)
    result = run_fixture(
        tmp_path,
        monkeypatch,
        {first["macro"]: (0, []), second["macro"]: (0, report(second))},
    )
    assert "report" in result["designs"][0]["exclusion_reason"]
    assert result["designs"][1]["instance_count"] == 53
