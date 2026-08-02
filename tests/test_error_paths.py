"""Parametrized error-path tests shared across every `klt <verb>` CLI command.

Each verb's error handling (missing file / non-layout file / JSON error
envelope / missing required positional) is wired through the same shared
helper (see `docs/json-contract.md`), so these behaviors are identical
across verbs. Rather than duplicating the same four test functions in every
verb's test module, this file drives them from a single table.

Verb-specific tests (text-format markers, verb-specific exceptions, `drc`'s
`--deck` handling, `pdk`'s different arg shape, etc.) stay in their own test
modules — only the mechanically identical error paths live here.

`sim`'s positional is a request JSON file rather than a GDSII/OASIS layout,
but the same missing-file / unreadable-input / JSON-envelope behavior
applies: a missing request file is "not found" (`klayout_tools.sim.SimError`)
exactly like a missing layout file, and an unparseable request file (e.g.
plain text) fails the same way a non-layout-stream file fails `klt layers`.
"""

import json

import pytest

from klayout_tools.cli import main

# (verb, extra CLI args required for a valid invocation, e.g. drc's --deck)
VERBS = [
    ("layers", []),
    ("cells", []),
    ("stats", []),
    ("drc", ["--deck", "sky130"]),
    ("precheck", []),
    ("render", []),
    ("sim", []),
]


@pytest.mark.parametrize("verb,extra_args", VERBS, ids=[v for v, _ in VERBS])
def test_missing_file(verb, extra_args, tmp_path, capsys):
    missing = tmp_path / "nope.gds"
    assert main([verb, str(missing), *extra_args]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"klt {verb}" in captured.err
    assert "not found" in captured.err


@pytest.mark.parametrize("verb,extra_args", VERBS, ids=[v for v, _ in VERBS])
def test_non_layout_file(verb, extra_args, tmp_path, capsys):
    bogus = tmp_path / "notes.txt"
    bogus.write_text("this is not a layout stream\n" * 4)

    assert main([verb, str(bogus), *extra_args]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"klt {verb}" in captured.err


@pytest.mark.parametrize("verb,extra_args", VERBS, ids=[v for v, _ in VERBS])
def test_missing_file_json_format(verb, extra_args, tmp_path, capsys):
    """`--format json` errors emit the documented JSON error envelope on
    stderr, leave stdout empty, and exit 1."""
    missing = tmp_path / "nope.gds"

    assert main([verb, str(missing), *extra_args, "--format", "json"]) == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == verb
    assert "not found" in error["error"]["message"]


@pytest.mark.parametrize("verb,extra_args", VERBS, ids=[v for v, _ in VERBS])
def test_subcommand_requires_file(verb, extra_args, tmp_path):
    # Every verb requires a file positional; argparse exits 2 when it's
    # missing, regardless of any other required flags (e.g. drc's --deck).
    with pytest.raises(SystemExit):
        main([verb])
