"""Each registered workflow must give every push to main its own verdict."""

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
MAIN_VERDICT_WORKFLOWS = ("file-size-ratchet-main.yml",)


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
