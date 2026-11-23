"""``klt version --format json``'s additive ``commands`` field (issue #7324).

A downstream toolchain pin check needs "does this install provide commands
X, Y, Z?" answered from the JSON contract: scraping ``klt --help`` couples
the probe to argparse's rendering (locale, terminal width, and
Python-version-dependent section headings) and fails *silently toward an
empty set* on formatting drift -- which reads as "this install is missing
the commands you require", a pin violation, rather than as a broken probe.

The expected verb set in these tests is derived from the live parser,
never hardcoded, so adding a verb never needs a test edit.
"""

from __future__ import annotations

import argparse
import json

from klayout_tools import build_identity
from klayout_tools.cli import main, version_cmd
from klayout_tools.cli.parser import create_parser


def _top_level_subparsers() -> argparse._SubParsersAction:
    """The live parser's ``<command>`` action -- the accept-set ``klt <cmd>``
    actually dispatches on."""
    return next(
        action
        for action in create_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )


def _version_json(capsys) -> dict:
    assert main(["version", "--format", "json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_commands_names_equal_the_live_parsers_top_level_choices(capsys):
    """AC: the ``name`` set equals the live parser's top-level choices, so
    the list cannot drift from what ``klt <cmd>`` actually accepts (a verb
    registered without a help line would drop out of the field and fail
    here, forcing it to carry one)."""
    commands = _version_json(capsys)["commands"]

    assert [c["name"] for c in commands] == sorted(_top_level_subparsers().choices)


def test_commands_entries_are_sorted_name_summary_objects(capsys):
    commands = _version_json(capsys)["commands"]

    assert all(set(c) == {"name", "summary"} for c in commands)
    names = [c["name"] for c in commands]
    assert names == sorted(names)
    assert all(isinstance(c["name"], str) and c["name"] for c in commands)
    assert all(isinstance(c["summary"], str) and c["summary"] for c in commands)


def test_version_lists_itself_and_preserves_the_identity_fields(capsys):
    """AC: ``version`` is in the list; ``schema_version`` stays ``1`` and the
    payload is exactly the library identity report plus the one new field --
    every pre-existing field keeps its value and type."""
    payload = _version_json(capsys)
    commands = payload.pop("commands")

    assert payload == build_identity.version_report()
    assert payload["schema_version"] == 1
    assert isinstance(payload["version"], str)
    assert "version" in [c["name"] for c in commands]


def test_text_format_still_prints_the_one_line_string(capsys):
    """AC: ``--format text`` is byte-identical -- exactly the one line it
    always printed; the new field renders nothing in text mode."""
    assert main(["version"]) == 0
    text_out = capsys.readouterr().out

    assert main(["version", "--format", "json"]) == 0
    version = json.loads(capsys.readouterr().out)["version"]

    assert text_out == f"klt {version}\n"


def test_hidden_verb_is_excluded_from_commands(monkeypatch):
    """AC: a verb registered with ``help=argparse.SUPPRESS`` (hidden from
    ``klt --help``) stays hidden from the machine-readable list too -- the
    field must not become a way to discover internal verbs."""
    from klayout_tools.cli import parser as parser_module

    real_create_parser = parser_module.create_parser

    def create_parser_with_probe_verbs():
        parser = real_create_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        subparsers.add_parser("__probe_hidden__", help=argparse.SUPPRESS)
        subparsers.add_parser("__probe_visible__", help="probe verb for the test")
        return parser

    monkeypatch.setattr(parser_module, "create_parser", create_parser_with_probe_verbs)

    names = [c["name"] for c in version_cmd._list_commands()]

    assert "__probe_visible__" in names
    assert "__probe_hidden__" not in names
