"""Item 9 compares its declared klt pin with the build producing the envelope."""

import json

import pytest

from helpers.signoff_item9 import OK_ARGV, PINS, make_inventory, make_repo
from klayout_tools import signoff, signoff_attest
from klayout_tools.cli import main


def _set_version(monkeypatch, version):
    monkeypatch.setattr(signoff_attest, "build_version", lambda: version)
    monkeypatch.setattr(signoff, "build_version", lambda: version)


def _attest_args(repo, klt_pin):
    inventory = make_inventory(
        repo,
        pins={**PINS, "tools": {**PINS["tools"], "klt": klt_pin}},
        testbenches=["tb/a.spice"],
        cold_start={"argv": OK_ARGV},
    )
    return ["signoff", "--attest", "9", "--inventory", inventory]


def _version_check(envelope):
    (check,) = [
        c for c in envelope["attestation"]["checks"] if c["role"] == "tool_version"
    ]
    return check


@pytest.mark.parametrize("use_reference", [False, True])
def test_matching_pin_records_the_resolved_version(
    tmp_path, capsys, monkeypatch, use_reference
):
    _set_version(monkeypatch, "0.9.0")
    repo = make_repo(tmp_path)
    pin = "0.9.0"
    if use_reference:
        (repo / "tools.json").write_text(json.dumps({"versions": {"klt": pin}}))
        pin = {"from": "tools.json", "key": "versions.klt"}
    args = _attest_args(repo, pin)

    assert main([*args, "--format", "json"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["provenance"]["klt_version"] == "0.9.0"
    assert _version_check(envelope) == {
        "check": "pin",
        "role": "tool_version",
        "tool": "klt",
        "passed": True,
        "value": "0.9.0",
    }


@pytest.mark.parametrize("pinned", ["0.9.0", "0.9.0.dev0+unknown"])
def test_stale_pin_fails_without_writing(tmp_path, capsys, monkeypatch, pinned):
    running = "0.9.0.dev3+g1234567"
    _set_version(monkeypatch, running)
    repo = make_repo(tmp_path)
    args = _attest_args(repo, pinned)
    output = repo / "evidence.json"

    assert main([*args, "-o", str(output), "--format", "json"]) == 3
    assert not output.exists()
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "fail"
    check = _version_check(envelope)
    assert check["passed"] is False
    assert check["value"] == pinned
    assert check["running"] == running

    assert main(args) == 3
    line = next(s for s in capsys.readouterr().out.splitlines() if "[FAIL]" in s)
    assert f"klt {pinned}" in line
    assert running in line


def test_check_regrades_committed_pin(tmp_path, capsys, monkeypatch):
    repo = make_repo(tmp_path)
    args = _attest_args(repo, "0.9.0")
    committed = str(repo / "item9.json")
    check_args = [*args, "--check", committed, "--format", "json"]
    _set_version(monkeypatch, "0.9.0")
    assert main([*args, "-o", committed]) == 0
    capsys.readouterr()

    assert main(check_args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "match"

    _set_version(monkeypatch, "0.8.0")
    assert main(check_args) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "drifted"
    assert any(
        d.get("check", {}).get("role") == "tool_version"
        and d["field"].endswith(".passed")
        for d in report["drift"]
    )


def test_unknown_build_discloses_unverifiable_pin(tmp_path, capsys, monkeypatch):
    repo = make_repo(tmp_path)
    args = _attest_args(repo, "0.9.0")
    committed = str(repo / "item9.json")
    _set_version(monkeypatch, "0.9.0")
    assert main([*args, "-o", committed]) == 0
    capsys.readouterr()

    _set_version(monkeypatch, "0.9.0.dev0+unknown")
    assert main([*args, "--format", "json"]) == 0
    check = _version_check(json.loads(capsys.readouterr().out))
    assert check["passed"] is True
    assert check["unverifiable"] is True

    assert main([*args, "--check", committed, "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "match"
