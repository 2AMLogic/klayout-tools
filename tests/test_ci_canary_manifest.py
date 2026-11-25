"""Exercise the live inventory gate from the lint and scheduled workflows."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
GATE = "scripts/ci-canary-manifest.sh"


def _workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


@pytest.fixture
def checkout(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    script = tmp_path / GATE
    script.parent.mkdir()
    shutil.copy2(ROOT / GATE, script)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base", "--allow-empty")
    # Stand in for the live comparison, including its nonzero exit on drift.
    uv = tmp_path / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALL_LOG"\nexit 1\n')
    uv.chmod(0o755)
    return tmp_path


@pytest.mark.parametrize(
    ("event", "path", "must_check"),
    [
        ("pull_request", "docs/cli/lvs.md", False),
        ("pull_request", "blocks/canaries.json", True),
        ("pull_request", "scripts/ingest-canary.py", True),
        ("pull_request", GATE, True),
        ("pull_request", ".github/workflows/canary-manifest.yml", True),
        ("merge_group", "README.md", False),
        ("merge_group", "blocks/canaries.json", True),
        ("push", "README.md", True),
        ("schedule", "README.md", True),
    ],
)
def test_inventory_gate(checkout, event, path, must_check):
    base = _git(checkout, "rev-parse", "HEAD")
    changed = checkout / path
    changed.parent.mkdir(parents=True, exist_ok=True)
    with changed.open("a") as out:
        out.write("\n# changed\n")
    _git(checkout, "add", "--", path)
    _git(checkout, "commit", "-qm", "change")
    head = _git(checkout, "rev-parse", "HEAD")
    # A base-only manifest change must not become part of an unrelated PR's diff.
    if event == "pull_request" and not must_check:
        _git(checkout, "checkout", "-q", "-b", "base-advanced", base)
        manifest = checkout / "blocks/canaries.json"
        manifest.parent.mkdir()
        manifest.write_text("[]\n")
        _git(checkout, "add", "blocks/canaries.json")
        _git(checkout, "commit", "-qm", "base inventory update")
        base = _git(checkout, "rev-parse", "HEAD")
        _git(checkout, "checkout", "-q", "--detach", head)

    assert _run_gate(checkout, event, base, head) == (must_check, int(must_check))


def test_unavailable_diff_falls_back_to_live_check(checkout):
    head = _git(checkout, "rev-parse", "HEAD")
    assert _run_gate(checkout, "pull_request", "0" * 40, head) == (True, 1)


def _run_gate(checkout, event, base, head):
    if event == "schedule":
        workflow = _workflow("canary-manifest.yml")
        assert workflow[True]["schedule"]  # PyYAML reads the Actions key "on" as True.
        job = workflow["jobs"]["inventory"]
    else:
        job = _workflow("ci.yml")["jobs"]["lint"]
    step = next(
        s for s in job["steps"] if s.get("name") == "Canary manifest matches the forge"
    )
    env = dict(
        os.environ,
        GITHUB_EVENT_NAME=event,
        CALL_LOG=str(checkout / "calls"),
        PATH=f"{checkout}{os.pathsep}{os.environ['PATH']}",
    )
    if event in {"pull_request", "merge_group"}:
        context = "github.event." + event
        for var, pr_key, group_key in (
            ("CANARY_BASE_SHA", "base.sha", "base_sha"),
            ("CANARY_HEAD_SHA", "head.sha", "head_sha"),
        ):
            key = pr_key if event == "pull_request" else group_key
            assert f"{context}.{key}" in step["env"][var]
        env.update(CANARY_BASE_SHA=base, CANARY_HEAD_SHA=head)
    result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=checkout, env=env)
    calls = checkout / "calls"
    if calls.exists():
        assert calls.read_text().splitlines() == [
            "run --locked --extra dev python scripts/ingest-canary.py --check-manifest"
        ]
    return calls.exists(), result.returncode
