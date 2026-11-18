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
