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


def test_help_doc_pointers_exist_in_packaged_set():
    packaged = {p.name for p in (_ROOT / "docs" / "cli").glob("*.md")}
    named = {
        m for text in _all_help_texts(create_parser()) for m in _POINTER.findall(text)
    }
    assert named, "expected --help text to name docs/cli pages"
    assert named <= packaged, (
        f"--help names pages not shipped: {sorted(named - packaged)}"
    )
