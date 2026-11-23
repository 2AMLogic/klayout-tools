"""``klt version --format json``'s additive ``commands`` field (issue #7324)
and the ``--require`` gate built on it (issue #7331).

A downstream toolchain pin check needs "does this install provide commands
X, Y, Z?" answered from the JSON contract: scraping ``klt --help`` couples
the probe to argparse's rendering (locale, terminal width, and
Python-version-dependent section headings) and fails *silently toward an
empty set* on formatting drift -- which reads as "this install is missing
the commands you require", a pin violation, rather than as a broken probe.

``--require`` turns that list into a gate: the normal payload plus an
additive ``required_commands`` field, exit ``3`` when a required name is
missing. An unknown name is the signal, not a usage error.

The expected verb set in these tests is derived from the live parser,
never hardcoded, so adding a verb never needs a test edit.
"""

from __future__ import annotations

import argparse
import json

import pytest

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


def _pin_reinstall_spec(monkeypatch, reinstall_spec: str | None) -> str:
    """Make ``klt version`` report a fixed ``reinstall_spec`` and return the
    version string it reports, so a test does not depend on whether the
    checkout running it is clean."""
    report = {**build_identity.version_report(), "reinstall_spec": reinstall_spec}
    monkeypatch.setattr(version_cmd, "version_report", lambda: report)
    return report["version"]


def _require_json(capsys, *values: str) -> tuple[int, dict, str]:
    """``(exit code, JSON payload, stderr)`` of ``klt version --format json``
    with one ``--require`` per value."""
    argv = ["version", "--format", "json"]
    for value in values:
        argv += ["--require", value]
    code = main(argv)
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def test_without_require_the_payload_is_unchanged(capsys):
    """AC: without ``--require`` the JSON payload has no ``required_commands``
    key, the exit code is 0 and nothing is written to stderr."""
    assert main(["version", "--format", "json"]) == 0
    captured = capsys.readouterr()

    assert "required_commands" not in json.loads(captured.out)
    assert captured.err == ""


def test_require_all_present_exits_zero_with_nothing_missing(capsys):
    """AC: requiring verbs this install provides exits 0 and adds
    ``required_commands`` with ``missing: []``; a passing gate is silent."""
    code, payload, err = _require_json(capsys, "drc,lvs")

    assert code == 0
    assert payload["schema_version"] == 1
    assert payload["required_commands"] == {
        "required": ["drc", "lvs"],
        "missing": [],
    }
    assert err == ""


def test_require_missing_command_exits_three_and_names_it(capsys, monkeypatch):
    """AC: an unknown name is the signal, not a usage error -- exit 3, the
    full normal payload still on stdout with ``missing: ["nonesuch"]``, and
    a stderr line naming it followed by the ``reinstall_spec`` install
    line."""
    _pin_reinstall_spec(monkeypatch, "klayout-tools==9.9.9")
    code, payload, err = _require_json(capsys, "drc,nonesuch")
    assert main(["version", "--format", "json"]) == 0
    normal = json.loads(capsys.readouterr().out)

    assert code == 3
    assert payload.pop("required_commands") == {
        "required": ["drc", "nonesuch"],
        "missing": ["nonesuch"],
    }
    assert payload == normal
    assert err.splitlines() == [
        "klt: missing required command(s): nonesuch",
        "klt: this build can be reinstalled with: "
        'uvx --from "klayout-tools==9.9.9" klt ...',
    ]


def test_require_without_reinstall_spec_omits_the_install_line(capsys, monkeypatch):
    """Edge: ``reinstall_spec`` is ``null`` (dirty tree) -- stderr names the
    missing command and nothing else, and the exit is still 3."""
    _pin_reinstall_spec(monkeypatch, None)
    code, payload, err = _require_json(capsys, "nonesuch")

    assert code == 3
    assert payload["required_commands"]["missing"] == ["nonesuch"]
    assert err.splitlines() == ["klt: missing required command(s): nonesuch"]


def test_require_accepts_repeats_and_comma_lists_in_input_order(capsys):
    """AC: repeats and comma lists combine; duplicates collapse to their
    first position and ``required``/``missing`` keep input order."""
    code, payload, _ = _require_json(capsys, "lvs,drc", "drc", " deck ,lvs ")
    assert code == 0
    assert payload["required_commands"] == {
        "required": ["lvs", "drc", "deck"],
        "missing": [],
    }

    code, payload, _ = _require_json(capsys, "a-nonesuch,drc,b-nonesuch", "a-nonesuch")
    assert code == 3
    assert payload["required_commands"] == {
        "required": ["a-nonesuch", "drc", "b-nonesuch"],
        "missing": ["a-nonesuch", "b-nonesuch"],
    }


def test_require_in_text_format_keeps_the_one_line_string(capsys, monkeypatch):
    """AC: ``--format text`` still prints only the one-line version string;
    the gate speaks through stderr and the exit code."""
    version = _pin_reinstall_spec(monkeypatch, "klayout-tools==9.9.9")

    assert main(["version", "--require", "drc"]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"klt {version}\n"
    assert captured.err == ""

    assert main(["version", "--require", "nonesuch"]) == 3
    captured = capsys.readouterr()
    assert captured.out == f"klt {version}\n"
    assert captured.err.splitlines() == [
        "klt: missing required command(s): nonesuch",
        "klt: this build can be reinstalled with: "
        'uvx --from "klayout-tools==9.9.9" klt ...',
    ]


@pytest.mark.parametrize("empty", ["", " , "])
def test_require_value_naming_nothing_is_a_usage_error(capsys, empty):
    """``--require ""`` (or only commas and whitespace) must not pass as a
    gate that checks nothing: exit 2 through the documented error envelope,
    with no report on stdout."""
    assert main(["version", "--require", empty, "--format", "json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "schema_version": 1,
        "error": {"command": "version", "message": "--require names no command"},
    }

    assert main(["version", "--require", empty]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "klt version: --require names no command\n"


def test_require_counts_a_hidden_verb_and_a_nested_subcommand_as_missing(
    capsys, monkeypatch
):
    """A name is looked up in ``commands`` -- top-level verbs only, hidden
    ones excluded -- so neither a verb hidden from ``klt --help`` nor a
    nested subcommand satisfies the gate."""
    from klayout_tools.cli import parser as parser_module

    real_create_parser = parser_module.create_parser

    def create_parser_with_hidden_verb():
        parser = real_create_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        subparsers.add_parser("__probe_hidden__", help=argparse.SUPPRESS)
        return parser

    monkeypatch.setattr(parser_module, "create_parser", create_parser_with_hidden_verb)
    code, payload, _ = _require_json(capsys, "__probe_hidden__", "pdk find", "version")

    assert code == 3
    assert payload["required_commands"] == {
        "required": ["__probe_hidden__", "pdk find", "version"],
        "missing": ["__probe_hidden__", "pdk find"],
    }
