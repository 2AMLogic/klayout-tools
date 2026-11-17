"""Tests for the command-evidence timeout reason token (issue #5763).

`tests/test_signoff.py` is frozen at its `file-size-baseline.json` ceiling,
so the tests pinning the new `command_timeout` token -- split out of
`command_failed` -- live here. The split is one `except` pair in
`_resolve_evidence` (`src/klayout_tools/signoff.py`): a
`subprocess.TimeoutExpired` on a command-backed manifest evidence entry
grades `unmet` with `reason: "command_timeout"` (the bound, not the check,
produced the grade); an `OSError` launch failure and a nonzero exit without
a parseable envelope keep `command_failed`.
"""

from __future__ import annotations

import subprocess

from helpers.subprocess_fakes import fake_completed
from klayout_tools import signoff as signoff_module
from klayout_tools.signoff import build_tier_report


def _manifest(**overrides) -> dict:
    base = {"block": "demo-block", "kind": "analog", "evidence": {}}
    base.update(overrides)
    return base


def _report(monkeypatch, fake_run) -> dict:
    monkeypatch.setattr(signoff_module.subprocess, "run", fake_run)
    return build_tier_report(_manifest(evidence={"3": {"command": ["klt", "sim"]}}))


def _item(result: dict, item_id: int) -> dict:
    return next(item for item in result["items"] if item["id"] == item_id)


def test_timeout_renders_command_timeout(monkeypatch):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout"))

    result = _report(monkeypatch, fake_run)

    item_3 = _item(result, 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "command_timeout"
    assert item_3["citation"] is None


def test_launch_failure_still_renders_command_failed(monkeypatch):
    def fake_run(command, **kwargs):
        raise OSError("No such file or directory: 'klt'")

    result = _report(monkeypatch, fake_run)

    item_3 = _item(result, 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "command_failed"
    assert item_3["citation"] is None


def test_nonzero_exit_without_envelope_still_renders_command_failed(monkeypatch):
    # Application-level error per docs/json-contract.md: exit 1 leaves stdout
    # empty, so there is no envelope to grade -- a failure, not a timeout.
    def fake_run(command, **kwargs):
        return fake_completed(returncode=1, stdout="")

    result = _report(monkeypatch, fake_run)

    item_3 = _item(result, 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "command_failed"
    assert item_3["citation"] is None
