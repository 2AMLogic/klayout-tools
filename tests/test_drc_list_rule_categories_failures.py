"""Fail-closed behaviour of ``klt drc --list-rule-categories`` (#5444).

A listing that cannot be trusted must be an **error with nothing on
stdout**, never a short list: a partial report's category set is exactly
the silently-wrong manifest this mode exists to stop a consumer
hand-writing, and it would satisfy a weaker assertion than the caller
intended. The deck fixtures live in ``tests/helpers/drc_list_categories.py``;
the happy path is in ``tests/test_drc_list_rule_categories.py``.
"""

from __future__ import annotations

import json

import pytest

from helpers.drc_list_categories import INCLUDED_DECK, MAIN_DECK
from helpers.drc_list_categories import (
    SKIP_NO_KLAYOUT_BINARY as _SKIP_NO_KLAYOUT_BINARY,
)
from helpers.drc_list_categories import (
    fixture_deck as _fixture_deck,
)
from helpers.drc_list_categories import (
    write_deck as _write_deck,
)
from klayout_tools.cli import main
from klayout_tools.drc import DrcError
from klayout_tools.drc_klayout_list import list_klayout_deck_rule_categories

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

# --------------------------------------------------------------------------- #
# Fail-closed: a listing that cannot be trusted is an error, never a short
# list on stdout.
# --------------------------------------------------------------------------- #


@_SKIP_NO_KLAYOUT_BINARY
def test_deck_that_errors_fails_closed(tmp_path, capsys):
    deck = _write_deck(
        tmp_path,
        "broken.drc",
        "if $input\n  source($input)\nend\n"
        'if $report\n  report("b", $report)\nend\n'
        "no_such_method_at_all(1)\n",
    )

    exit_code = main(
        [
            "drc",
            "--engine",
            "klayout",
            "--deck-file",
            deck,
            "--list-rule-categories",
            "--timeout-s",
            "60",
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "could not list rule categories" in captured.err
    assert "empty layout" in captured.err


@_SKIP_NO_KLAYOUT_BINARY
def test_zero_declared_categories_fails_closed(tmp_path, capsys):
    """Every rule gated behind an unset `--deck-var`: an empty manifest
    would satisfy a weaker assertion than the caller intended, so it is
    refused instead of printed."""
    deck = _write_deck(
        tmp_path,
        "gated.drc",
        "if $input\n  source($input)\nend\n"
        'if $report\n  report("g", $report)\nend\n'
        'if $feol == "true"\n'
        '  input(3, 0).width(0.5.um).output("diff.1", "gated")\n'
        "end\n",
    )

    exit_code = main(
        [
            "drc",
            "--engine",
            "klayout",
            "--deck-file",
            deck,
            "--list-rule-categories",
            "--timeout-s",
            "60",
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "declared no rule category at all" in captured.err


@_SKIP_NO_KLAYOUT_BINARY
def test_timeout_fails_closed(tmp_path):
    deck = _fixture_deck(tmp_path)

    with pytest.raises(DrcError) as excinfo:
        list_klayout_deck_rule_categories(deck, timeout_s=0.001)

    message = str(excinfo.value)
    assert "could not list rule categories" in message
    assert "did not complete" in message


# --------------------------------------------------------------------------- #
# `--pdk` resolution, and "a listing is not signoff evidence".
# --------------------------------------------------------------------------- #


@_SKIP_NO_KLAYOUT_BINARY
def test_pdk_flags_resolve_the_deck_the_way_a_run_does(tmp_path, capsys):
    """Same `pdk.drc_deck_file` path a run takes (the IHP-shaped install
    `tests/test_drc_klayout_engine.py` already fabricates)."""
    pdk_root = tmp_path / "ihp-sg13cmos5l"
    drc_dir = pdk_root / "libs.tech" / "klayout" / "tech" / "drc"
    drc_dir.mkdir(parents=True)
    (drc_dir / "included.drc").write_text(INCLUDED_DECK, encoding="utf-8")
    (drc_dir / "ihp-sg13cmos5l.drc").write_text(MAIN_DECK, encoding="utf-8")

    exit_code = main(
        [
            "drc",
            "--engine",
            "klayout",
            "--pdk",
            "ihp-sg13cmos5l",
            "--pdk-root",
            str(pdk_root),
            "--list-rule-categories",
            "--timeout-s",
            "60",
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["deck_file"] == str(drc_dir / "ihp-sg13cmos5l.drc")
    assert payload["rule_categories"] == ["M1.a", "M2.a"]
    assert payload["provenance"]["pdk"]["name"] == "ihp-sg13cmos5l"


@_SKIP_NO_KLAYOUT_BINARY
def test_listing_is_not_signoff_evidence(tmp_path):
    """Same treatment `klt signoff` gives a `--list-rules` listing (see
    `tests/test_signoff.py::test_drc_list_rules_output_is_not_signoff_evidence`):
    an unrecognised envelope, never graded."""
    from klayout_tools.signoff import SignoffError, build_signoff

    deck = _fixture_deck(tmp_path)
    listing = list_klayout_deck_rule_categories(deck, timeout_s=60.0)
    path = tmp_path / "listing.json"
    path.write_text(json.dumps(listing), encoding="utf-8")

    with pytest.raises(SignoffError):
        build_signoff([str(path)])
