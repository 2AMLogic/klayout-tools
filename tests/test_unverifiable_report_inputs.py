"""Cheap LVS and extraction checks distinguish missing input bytes from drift."""

import hashlib
import json

import pytest

from klayout_tools.cli import main


@pytest.fixture(params=["lvs", "extract"])
def stored_report(tmp_path, request):
    verb = request.param
    paths = {
        name: tmp_path / name for name in ("layout.gds", "reference.sp", "runset.lvs")
    }
    for name, path in paths.items():
        path.write_bytes(name.encode())
    layout, reference, runset = paths.values()

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    provenance = {"runset": {"content_hash": "sha256:" + digest(runset)}}
    report = {
        "engine": "klayout-runset",
        "deck_file": str(runset),
        "provenance": provenance,
    }
    if verb == "lvs":
        report.update(
            layout=str(layout),
            reference=str(reference),
            environment={
                "layout_sha256": digest(layout),
                "reference_sha256": digest(reference),
            },
        )
    else:
        report["file"] = str(layout)
        provenance["input"] = {"content_hash": "sha256:" + digest(layout)}
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    return verb, path, layout, runset


@pytest.mark.parametrize(
    ("runset_state", "layout_state", "verdict", "rc"),
    [
        ("original", "original", "match", 0),
        ("missing", "original", "unverifiable", 4),
        ("changed", "original", "drifted", 3),
        ("missing", "changed", "drifted", 3),
        ("original", "missing", "unverifiable", 4),
        ("missing", "missing", "unverifiable", 4),
    ],
)
def test_cheap_check_json_and_text(
    stored_report, capsys, runset_state, layout_state, verdict, rc
):
    verb, path, layout, runset = stored_report
    layout_field = (
        "environment.layout_sha256"
        if verb == "lvs"
        else "provenance.input.content_hash"
    )
    fields = [
        (runset, "provenance.runset.content_hash", runset_state),
        (layout, layout_field, layout_state),
    ]
    for artifact, _, state in fields:
        if state == "missing":
            artifact.unlink()
        elif state == "changed":
            artifact.write_bytes(b"modified")
    args = [verb, "--check", str(path)]
    assert main([*args, "--format", "json"]) == rc
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == verdict
    by_field = {c["field"]: c for c in result["checks"]}
    assert main([*args, "--format", "text"]) == rc
    text = capsys.readouterr().out
    assert f"status: {verdict}" in text
    for artifact, field, state in fields:
        check = by_field[field]
        assert check["match"] == (state == "original")
        if state == "missing":
            assert check["status"] == "unverifiable"
            assert check["expected"] and check["actual"] is None
            if verb == "lvs":
                assert check["input_not_found"]["resolved"] == str(artifact)
        elif state == "changed":
            assert check["actual"] and check["actual"] != check["expected"]
        label = {"original": "OK", "changed": "DRIFTED", "missing": "UNVERIFIABLE"}[
            state
        ]
        assert f"[{label}] {field}" in text
        artifact.write_bytes(artifact.name.encode())
    assert main([*args, "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "match"


def test_legacy_runset_without_recorded_hash_cannot_pass(stored_report, capsys):
    verb, path, _, _ = stored_report
    report = json.loads(path.read_text())
    del report["provenance"]["runset"]["content_hash"]
    path.write_text(json.dumps(report))
    assert main([verb, "--check", str(path), "--format", "json"]) == 3
    checks = json.loads(capsys.readouterr().out)["checks"]
    runset = next(c for c in checks if c["field"] == "provenance.runset.content_hash")
    assert runset["expected"] is None and runset["match"] is False


def test_missing_report_is_a_command_error(stored_report, capsys):
    verb, path, _, _ = stored_report
    path.unlink()
    assert main([verb, "--check", str(path), "--format", "json"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]
