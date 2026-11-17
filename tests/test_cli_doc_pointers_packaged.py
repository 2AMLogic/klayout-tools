"""``docs/cli/<verb>.md`` pointers in ``--help`` must resolve for a wheel-only
install (issue #5288): the pages are force-included into the wheel."""

import argparse
import re
from pathlib import Path

import pytest

from klayout_tools.cli.parser import create_parser

_ROOT = Path(__file__).resolve().parent.parent
_POINTER = re.compile(r"docs/cli/([A-Za-z0-9_-]+\.md)")


def _all_help_texts(parser):
    yield parser.format_help()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                yield from _all_help_texts(sub)


def test_wheel_force_includes_cli_docs():
    # `tomllib` is stdlib only from 3.11 (PEP 680); this project supports 3.10.
    tomllib = pytest.importorskip("tomllib")
    cfg = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    force = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force.get("docs/cli") == "klayout_tools/data/cli"
    # Shared contract docs the shipped pages cite as authority (issue #5819).
    assert force.get("docs/json-contract.md") == "klayout_tools/data/json-contract.md"
    assert (
        force.get("docs/coverage-contract.md")
        == "klayout_tools/data/coverage-contract.md"
    )
    assert (
        force.get("docs/releasing-native.md")
        == "klayout_tools/data/releasing-native.md"
    )
    assert force.get("docs/type-checking.md") == "klayout_tools/data/type-checking.md"


# Sibling links of the shipped pages: `../<name>.md` from a `docs/cli/*.md`
# page (a doc directly in `docs/`, not `../design/`, `../guides/`, `../schemas/`
# or any `../../` target), or a bare `<name>.md` from `docs/json-contract.md`
# itself. Measured against the shipped pages (issue #5819); the sets are exact
# so a newly cited sibling contract doc fails here instead of dangling in the
# wheel.
_CLI_PAGE_SIBLINGS = {
    "ARCHITECTURE.md",
    "coverage-contract.md",
    "design-evidence-tiers.md",
    "json-contract.md",
    "releasing-native.md",
}
_JSON_CONTRACT_SIBLINGS = {
    "coverage-contract.md",
    "type-checking.md",
}
_SIBLING_LINK = re.compile(r"\]\(\.\./([A-Za-z0-9._-]+\.md)(?:#[^)]*)?\)")
_OWN_SIBLING_LINK = re.compile(r"\]\(([A-Za-z0-9._-]+\.md)(?:#[^)]*)?\)")

# Cited siblings that deliberately do not ship. Each entry needs a reason:
# shipping it would dangle links of its own that are out of scope.
_DELIBERATELY_UNSHIPPED = {
    # Cited by 2 CLI pages, but it links `../scripts/README.md` and
    # `library/README.md`, neither of which ships; shipping it would trade
    # two dangling CLI-page links for two new dangling ones (issue #5819).
    "ARCHITECTURE.md",
}


def test_shipped_pages_cite_only_packaged_sibling_docs():
    tomllib = pytest.importorskip("tomllib")
    cli_pages = sorted((_ROOT / "docs" / "cli").glob("*.md"))
    assert cli_pages, "expected shipped docs/cli pages to exist"
    cited_cli = set()
    for page in cli_pages:
        cited_cli |= set(_SIBLING_LINK.findall(page.read_text()))
    assert cited_cli == _CLI_PAGE_SIBLINGS, (
        f"docs/cli sibling-.md citations drifted: {sorted(cited_cli)}"
    )
    json_contract = _ROOT / "docs" / "json-contract.md"
    cited_contract = set(_OWN_SIBLING_LINK.findall(json_contract.read_text()))
    assert cited_contract == _JSON_CONTRACT_SIBLINGS, (
        f"json-contract.md sibling-.md citations drifted: {sorted(cited_contract)}"
    )
    cfg = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    force = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    shipped = {
        Path(dest).name
        for dest in force.values()
        if dest.startswith("klayout_tools/data/")
    }
    cited = cited_cli | cited_contract
    unresolved = cited - shipped - _DELIBERATELY_UNSHIPPED
    assert not unresolved, (
        "shipped pages cite sibling docs that do not ship and are not "
        f"deliberately unshipped: {sorted(unresolved)}"
    )


def test_help_doc_pointers_exist_in_packaged_set():
    packaged = {p.name for p in (_ROOT / "docs" / "cli").glob("*.md")}
    named = {
        m for text in _all_help_texts(create_parser()) for m in _POINTER.findall(text)
    }
    assert named, "expected --help text to name docs/cli pages"
    assert named <= packaged, (
        f"--help names pages not shipped: {sorted(named - packaged)}"
    )
