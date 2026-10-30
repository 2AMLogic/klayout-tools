"""Drift guard for the klayout-tools.org capability data (issue #4597).

``site/src/data/cli.generated.json`` feeds the landing page's release badge,
verb list, MCP tool count and deck list. It is generated from the CLI
registry by ``scripts/gen_site_cli_data.py`` and committed; these tests fail
when the committed copy no longer matches the code, so the site cannot fall
behind the CLI again.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "gen_site_cli_data.py"


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("gen_site_cli_data", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def data(gen):
    return gen.build_data()


def test_committed_site_data_is_current(gen, data):
    committed = gen.OUTPUT.read_text(encoding="utf-8")
    assert committed == gen.render(data), (
        "site/src/data/cli.generated.json is stale; run "
        "`uv run python scripts/gen_site_cli_data.py` and commit the result"
    )


def test_every_registered_verb_is_listed_once(data):
    from klayout_tools.cli.parser import create_parser

    sub = next(
        a for a in create_parser()._actions if hasattr(a, "choices") and a.choices
    )
    listed = [v["name"] for g in data["groups"] for v in g["verbs"]]
    assert sorted(listed) == sorted(sub.choices)
    assert len(listed) == len(set(listed)) == data["verb_count"]


def test_overrides_and_aliases_name_real_verbs(gen, data):
    listed = {v["name"] for g in data["groups"] for v in g["verbs"]}
    assert set(gen.SUMMARY_OVERRIDES) <= listed
    assert set(gen.DOC_ALIASES) <= listed
    grouped = {v for _, _, members in gen.GROUPS for v in members}
    assert grouped <= listed, f"GROUPS names unknown verbs: {grouped - listed}"


def test_release_comes_from_the_newest_changelog_heading(gen):
    text = "## Unreleased\n\n- x\n\n## 1.2.3 (2027-01-02)\n\n## 1.2.2 (2026-12-01)\n"
    assert gen.latest_release(text) == {"version": "1.2.3", "date": "2027-01-02"}


def test_summaries_drop_internal_issue_references(gen):
    assert gen._summary("quantitative layout-density report (issue #1012)") == (
        "quantitative layout-density report"
    )


def test_summaries_are_plain_text(data):
    for group in data["groups"]:
        for verb in group["verbs"]:
            assert "`" not in verb["summary"], verb["name"]


def test_every_doc_link_resolves_to_a_committed_page(data):
    for group in data["groups"]:
        for verb in group["verbs"]:
            if verb["doc"] is None:
                continue
            page = verb["doc"].split("/blob/main/", 1)[1]
            assert (REPO_ROOT / page).is_file(), page


def test_output_is_valid_json(gen):
    json.loads(gen.OUTPUT.read_text(encoding="utf-8"))
