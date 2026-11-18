"""``docs/cli/<verb>.md`` pointers in ``--help`` must resolve for a wheel-only
install (issue #5288): the pages are force-included into the wheel."""

import argparse
import re
from collections import Counter
from pathlib import Path

import pytest

import klayout_tools
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
# or any `../../` target), or a bare `<name>.md` from the force-included
# contract docs themselves. Measured against the shipped pages (issues #5819,
# #6232); the sets are exact and per-source so a newly cited sibling contract
# doc fails here, named by its doc, instead of dangling in the wheel.
_CLI_PAGE_SIBLINGS = {
    "ARCHITECTURE.md",
    "coverage-contract.md",
    "design-evidence-tiers.md",
    "json-contract.md",
    "releasing-native.md",
}
_CONTRACT_DOC_SIBLINGS = {
    "json-contract.md": {
        "coverage-contract.md",
        "type-checking.md",
    },
    "coverage-contract.md": {
        "design-evidence-tiers.md",
        "json-contract.md",
    },
    "type-checking.md": {
        "json-contract.md",
    },
    "releasing-native.md": set(),
    "design-evidence-tiers.md": set(),
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
    cited_contract = set()
    for name, expected in _CONTRACT_DOC_SIBLINGS.items():
        cited = set(_OWN_SIBLING_LINK.findall((_ROOT / "docs" / name).read_text()))
        assert cited == expected, (
            f"{name} sibling-.md citations drifted: {sorted(cited)}"
        )
        cited_contract |= cited
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


def test_help_points_at_bundled_pages_for_every_parser(tmp_path, monkeypatch):
    # An unwrapped checkout render gives the full filename even for pointers
    # that argparse splits at a hyphen on a normal terminal.
    monkeypatch.setenv("COLUMNS", "10000")
    source = list(_all_help_texts(create_parser()))
    package = tmp_path / "wheel install" / "klayout_tools"
    docs = package / "data" / "cli"
    docs.mkdir(parents=True)
    for page in (_ROOT / "docs" / "cli").glob("*.md"):
        (docs / page.name).touch()
    monkeypatch.setattr(klayout_tools, "__file__", str(package / "__init__.py"))
    monkeypatch.setenv("COLUMNS", "80")
    for before, after in zip(source, _all_help_texts(create_parser()), strict=True):
        assert "docs/cli/" not in after
        for name, count in Counter(_POINTER.findall(before)).items():
            assert after.count(str(docs / name)) == count, name


def test_help_rewrites_only_standalone_shipped_page_names(tmp_path, monkeypatch):
    monkeypatch.setattr(klayout_tools, "__file__", str(tmp_path / "__init__.py"))
    page = tmp_path / "data" / "cli" / "sim.md"
    page.parent.mkdir(parents=True)
    page.touch()
    parser = create_parser()
    parser.epilog = (
        "See `docs/cli/sim.md`, docs/cli/sim.md#options and docs/cli/sim.md. "
        "Keep /other/docs/cli/sim.md, docs/cli/sim.md.bak and docs/cli/missing.md."
    )
    rendered = parser.format_help()
    for token in (
        f"`{page}`,",
        f"{page}#options",
        f"{page}.",
        "/other/docs/cli/sim.md,",
        "docs/cli/sim.md.bak",
        "docs/cli/missing.md.",
    ):
        assert token in rendered
