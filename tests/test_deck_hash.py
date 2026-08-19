"""`klt deck hash`: the no-input deck-identity query (issue #1202).

`provenance.deck.content_hash` is the field that actually pins a run's rule
set, but obtaining it used to require running a check -- and therefore having
a layout to run it against. These cover the direct query, and (critically)
that its answer is byte-identical to what a real run records.
"""

from __future__ import annotations

import hashlib
import json

import klayout.db as kdb
import pytest

from klayout_tools import _provenance
from klayout_tools.cli import main
from klayout_tools.decks import deck_names, deck_source_path
from klayout_tools.decks import history as deck_history
from klayout_tools.drc import run_drc

# --------------------------------------------------------------------------- #
# library: deck name registry + identity
# --------------------------------------------------------------------------- #


def test_deck_names_lists_the_built_in_decks():
    names = deck_names()
    assert {"sky130", "gf180mcu", "sg13g2"} <= set(names)
    assert names == sorted(names)


def test_deck_identity_rejects_a_non_deck_sibling_module():
    """`deck_source_path` will happily import any sibling module of the deck
    package, so an unvalidated name would hash `history.py` and report it as
    a deck. The name must be checked against the registry first."""
    assert deck_source_path("history") is not None  # importable, but not a deck
    with pytest.raises(_provenance.UnknownProvenanceDeckError):
        _provenance.deck_identity("history")


def test_deck_identity_matches_an_independent_hash_of_the_deck_module():
    block = _provenance.deck_identity("sky130")
    with open(deck_source_path("sky130"), "rb") as handle:
        expected = hashlib.sha256(handle.read()).hexdigest()
    assert block["content_hash"] == f"sha256:{expected}"


# --------------------------------------------------------------------------- #
# the acceptance criterion: same hash a report carries
# --------------------------------------------------------------------------- #


def test_deck_hash_matches_a_real_drc_runs_provenance(tmp_path, capsys):
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(64, 20)).insert(kdb.Box(0, 0, 1000, 1000))
    path = tmp_path / "a.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert main(["deck", "hash", "--deck", "sky130", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["content_hash"] == report["provenance"]["deck"]["content_hash"]
    assert payload["deck"] == report["provenance"]["deck"]["name"]
    assert payload["released"] == report["provenance"]["deck"]["released"]


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


def test_cli_deck_hash_json(capsys):
    assert main(["deck", "hash", "--deck", "gf180mcu", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert set(payload) == {"schema_version", "deck", "content_hash", "released"}
    assert payload["schema_version"] == 1
    assert payload["deck"] == "gf180mcu"
    assert payload["content_hash"].startswith("sha256:")
    assert payload["released"] in (True, False, None)


def test_cli_deck_hash_text(capsys):
    assert main(["deck", "hash", "--deck", "sky130"]) == 0
    out = capsys.readouterr().out
    assert "deck: sky130" in out
    assert "content_hash: sha256:" in out
    assert "released: " in out


def test_cli_deck_hash_unknown_deck_is_a_clean_error_envelope(capsys):
    exit_code = main(["deck", "hash", "--deck", "nope", "--format", "json"])

    assert exit_code == 1  # not argparse's usage-error 2, and not a traceback
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)["error"]
    assert error["command"] == "deck hash"
    assert "unknown deck 'nope'" in error["message"]
    assert "sky130" in error["message"]  # names what is available


def test_cli_deck_hash_unknown_deck_text(capsys):
    assert main(["deck", "hash", "--deck", "nope"]) == 1
    assert "klt deck hash: unknown deck 'nope'" in capsys.readouterr().err


def test_cli_deck_hash_requires_a_deck_name():
    with pytest.raises(SystemExit) as exc_info:
        main(["deck", "hash"])
    assert exc_info.value.code == 2


def test_cli_deck_hash_needs_no_layout_and_no_history_table(
    tmp_path, monkeypatch, capsys
):
    """The query answers "what does *this build* ship", so it must work with
    no input file at all and survive a missing release-history table -- the
    table only decorates the answer with `released`."""
    monkeypatch.setattr(deck_history, "_HISTORY_PATH", tmp_path / "missing.json")

    assert main(["deck", "hash", "--deck", "sky130", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["content_hash"].startswith("sha256:")
    assert payload["released"] is None  # unknown, never a fabricated False


def test_cli_deck_hash_reports_unreadable_deck_source_as_an_error(monkeypatch, capsys):
    monkeypatch.setattr(
        _provenance,
        "deck_identity",
        lambda name: {"name": name, "content_hash": None, "released": None},
    )
    # `deck_cmd` imported the symbol directly, so patch it there too.
    from klayout_tools.cli import deck_cmd

    monkeypatch.setattr(deck_cmd, "deck_identity", _provenance.deck_identity)

    assert main(["deck", "hash", "--deck", "sky130", "--format", "json"]) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["command"] == "deck hash"
    assert "no content hash" in error["message"]
