"""Merge queue checks must run independently for each combined tree."""

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


@pytest.mark.parametrize("filename", ["ci.yml", "tt-corpus-sample.yml"])
def test_merge_group_trigger_and_concurrency(filename: str) -> None:
    workflow = yaml.safe_load((WORKFLOWS / filename).read_text(encoding="utf-8"))
    # PyYAML reads an unquoted `on` key as True.
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["merge_group"] == {"types": ["checks_requested"]}

    concurrency = workflow["concurrency"]
    assert "github.event.merge_group.head_sha || github.ref" in concurrency["group"]
    assert (
        concurrency["cancel-in-progress"]
        == "${{ github.event_name == 'pull_request' }}"
    )


def test_authorization_rechecks_only_same_repository_merge_group_ci() -> None:
    workflow = yaml.safe_load((WORKFLOWS / "merge-group-check.yml").read_text())
    ci = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert triggers == {
        "merge_group": {"types": ["checks_requested"]},
        "workflow_run": {
            "workflows": [ci["name"]],
            "types": ["completed"],
            "branches": ["gh-readonly-queue/**"],
        },
    }
    job = workflow["jobs"]["group-check"]
    assert " ".join(job["if"].split()) == (
        "github.event_name == 'merge_group' || "
        "(github.event_name == 'workflow_run' && "
        "github.event.workflow_run.event == 'merge_group' && "
        "github.event.workflow_run.head_repository.full_name == github.repository)"
    )
    group_sha = (
        "${{ github.event.merge_group.head_sha || github.event.workflow_run.head_sha }}"
    )
    assert workflow["concurrency"] == {
        "group": "merge-group-check-" + group_sha,
        "cancel-in-progress": False,
    }
    (verifier,) = [
        step
        for step in job["steps"]
        if "forge merge-queue group-check" in step.get("run", "")
    ]
    assert verifier["env"]["GROUP_SHA"] == group_sha
    assert verifier["env"]["GH_REPO"] == "${{ github.repository }}"
    assert '--commit "$GROUP_SHA" --repo "$GH_REPO"' in verifier["run"]


def test_authorization_uses_trusted_configuration_and_scoped_permissions() -> None:
    workflow = yaml.safe_load((WORKFLOWS / "merge-group-check.yml").read_text())
    assert workflow["permissions"] == {}
    job = workflow["jobs"]["group-check"]
    assert job["permissions"] == {
        "contents": "read",
        "pull-requests": "read",
        "checks": "read",
        "statuses": "write",
    }
    (checkout,) = [
        step
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert checkout["with"]["ref"] == "refs/heads/main"
    assert checkout["with"]["persist-credentials"] is False
    assert checkout["with"]["sparse-checkout"] == ".loom"

    (install,) = [step for step in job["steps"] if "LOOM_SHA256" in step.get("env", {})]
    assert install["env"]["LOOM_VERSION"].startswith("v")
    assert len(bytes.fromhex(install["env"]["LOOM_SHA256"])) == 32
    assert "github.com/2AMLogic/loom/releases/download/" in install["run"]
    assert "sha256sum --check -" in install["run"]
