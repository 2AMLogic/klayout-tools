"""`klt evidence record` / `klayout_tools.evidence_record` (issue #4044)."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

import pytest

from klayout_tools import evidence_record as er
from klayout_tools.cli import main

NOW = datetime(2026, 10, 22, 18, 30, 5, tzinfo=timezone.utc)


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "sim").mkdir()
    (root / "sim" / "req.json").write_text('{"corners": {}}\n')
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _report(root, *, with_artifacts=True):
    art = root.parent / "arts"
    art.mkdir(exist_ok=True)
    corner = {"corner_id": "tt_1v8_27", "measurements": [], "artifacts": {}}
    if with_artifacts:
        (art / "corner.cir").write_text("* deck\n")
        corner["artifacts"] = {
            "log": str(art / "ngspice.log"),
            "deck": str(art / "corner.cir"),
        }
        (art / "ngspice.log").write_text("log\n")
    report = {
        "schema_version": 1,
        "provenance": {
            "pdk": {"name": "sky130A"},
            "deck": {"name": "d", "content_hash": "sha256:abc"},
        },
        "corner_count": 1,
        "corners": [corner],
    }
    path = root.parent / "report.json"
    path.write_text(json.dumps(report))
    return path, report


def _write(repo, report_path, **kw):
    return er.write_record(
        str(report_path),
        str(repo / "sim" / "req.json"),
        block="b",
        scope="s",
        root=str(repo),
        now=NOW,
        **kw,
    )


def test_record_minted_with_expected_shape(repo):
    report_path, report = _report(repo)
    summary = _write(repo, report_path)
    assert er.RECORD_ID_PATTERN.match(summary["record_id"])
    assert summary["record_id"].startswith("20261022-183005-")
    record = json.loads((repo / summary["record_path"]).read_text())
    assert er.validate_record_shape(record) == []
    assert record["result"] == report
    assert record["pdk_pin"] == {
        "name": "sky130A",
        "expected_deck_content_hash": "sha256:abc",
    }
    assert record["git"]["dirty_inputs"] is False
    assert record["artifacts"]["netlist_snapshot"]["status"] == "present"
    sidecar = repo / "evidence/sim/b/s" / f"{summary['record_id']}.d/tt_1v8_27"
    assert (sidecar / "corner.cir").read_text() == "* deck\n"
    assert (sidecar / "log").read_text() == "log\n"


def test_second_identical_call_refuses_to_overwrite(repo):
    report_path, _ = _report(repo)
    _write(repo, report_path)
    with pytest.raises(er.EvidenceRecordError) as exc:
        _write(repo, report_path)
    assert exc.value.code == er.CODE_EXISTS


def test_dirty_request_sets_dirty_inputs(repo):
    report_path, _ = _report(repo)
    (repo / "sim" / "req.json").write_text('{"changed": 1}\n')
    summary = _write(repo, report_path)
    assert summary["dirty_inputs"] is True


def test_non_git_root_is_an_error(tmp_path):
    root = tmp_path / "plain"
    (root / "sim").mkdir(parents=True)
    (root / "sim" / "req.json").write_text("{}")
    report_path, _ = _report(root)
    with pytest.raises(er.EvidenceRecordError) as exc:
        er.write_record(
            str(report_path),
            str(root / "sim" / "req.json"),
            block="b",
            scope="s",
            root=str(root),
        )
    assert exc.value.code == er.CODE_NOT_GIT


def test_missing_deck_artifact_reported_explicitly(repo):
    report_path, _ = _report(repo, with_artifacts=False)
    summary = _write(repo, report_path)
    assert summary["netlist_snapshot"] == "unavailable"
    record = json.loads((repo / summary["record_path"]).read_text())
    assert "keep_artifacts" in record["artifacts"]["netlist_snapshot"]["reason"]


def test_supersedes_missing_file_errors(repo):
    report_path, _ = _report(repo)
    with pytest.raises(er.EvidenceRecordError):
        _write(repo, report_path, supersedes=["evidence/nope.json"])


def test_wrapper_leak_is_refused(repo):
    report_path, _ = _report(repo)
    with pytest.raises(er.EvidenceRecordError) as exc:
        _write(repo, report_path, subset_reason="ran in /home/someone/work")
    assert exc.value.code == er.CODE_LEAK
    assert not (repo / "evidence").exists() or not list(
        (repo / "evidence").rglob("*.json")
    )


def test_cli_json_envelope_and_exists_error(repo, capsys):
    report_path, _ = _report(repo)
    argv = [
        "evidence",
        "record",
        str(report_path),
        "--request",
        str(repo / "sim" / "req.json"),
        "--block",
        "b",
        "--scope",
        "s",
        "--root",
        str(repo),
        "--format",
        "json",
    ]
    assert main(argv) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema_version"] == 1 and out["status"] == "recorded"
    assert (repo / out["record_path"]).is_file()


def test_validate_record_shape_flags_problems():
    assert er.validate_record_shape([]) == ["record must be a JSON object"]
    assert any("missing field" in p for p in er.validate_record_shape({}))
