"""Each registered workflow must give every push to main its own verdict."""

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
MAIN_VERDICT_WORKFLOWS = ("file-size-ratchet-main.yml", "main-lint.yml")


def test_main_verdict_registry_is_not_empty() -> None:
    # An empty parametrize set makes pytest skip the test below rather than
    # fail it, so emptying the registry would turn CI green with no coverage.
    # This test is deliberately not parametrized, so it cannot be skipped.
    assert MAIN_VERDICT_WORKFLOWS, "main verdict workflow registry is empty"


@pytest.mark.parametrize("filename", MAIN_VERDICT_WORKFLOWS)
def test_main_verdict_workflow_concurrency(filename: str) -> None:
    workflow = yaml.safe_load((WORKFLOWS / filename).read_text(encoding="utf-8"))
    # PyYAML reads an unquoted `on` key as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert "main" in triggers["push"]["branches"]

    concurrency = workflow["concurrency"]
    group = concurrency["group"]
    assert re.search(r"\$\{\{\s*github\.sha\s*\}\}", group)
    assert "github.ref" not in group
    assert concurrency["cancel-in-progress"] is False
