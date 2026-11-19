"""`klt deck hash`: the no-input deck-identity query (issue #1202).

`provenance.deck.content_hash` is the field that actually pins a run's rule
set, but obtaining it used to require running a check -- and therefore having
a layout to run it against. These cover the direct query, and (critically)
that its answer is byte-identical to what a real run records.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

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
    """An importable sibling is a deck only if the registry declares it."""
    assert deck_source_path("history") is not None  # importable, but not a deck
    with pytest.raises(_provenance.UnknownProvenanceDeckError):
        _provenance.deck_identity("history")


@pytest.mark.parametrize(
    ("name", "sources"),
    [
        ("gf180mcu", ("gf180mcu.py",)),
        ("sky130", ("sky130.py", "sky130_offgrid.py", "sky130_unmodeled.py")),
        (
            "sg13g2",
            (
                "sg13g2.py",
                "sg13g2_cont_grid.py",
                "sg13g2_runset_notes.py",
                "sg13g2_svaricap.py",
            ),
        ),
    ],
)
def test_deck_identity_hashes_its_rule_sources(name, sources):
    directory = Path(deck_source_path(name)).parent
    records = [
        (filename, hashlib.sha256((directory / filename).read_bytes()).digest())
        for filename in sources
    ]
    digest = (
        records[0][1]
        if len(records) == 1
        else hashlib.sha256(
            b"".join(filename.encode() + b"\0" + raw for filename, raw in records)
        ).digest()
    )
    assert _provenance.deck_identity(name)["content_hash"] == f"sha256:{digest.hex()}"


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


def test_saved_report_detects_a_sibling_edit_and_missing_source(tmp_path):
    package = tmp_path / "klayout_tools"
    shutil.copytree(
        Path(_provenance.__file__).parent,
        package,
        ignore=shutil.ignore_patterns("__pycache__"),
    )

    def klt(*args, expected_exit=0):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "klayout_tools.cli",
                *args,
                "--format",
                "json",
            ],
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": str(tmp_path)},
            capture_output=True,
            text=True,
        )
        assert result.returncode == expected_exit, (result.stdout, result.stderr)
        return json.loads(result.stderr if expected_exit == 1 else result.stdout)

    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(8, 0)).insert(kdb.Box(0, 0, 5000, 5000))
    stream = str(tmp_path / "top.gds")
    layout.write(stream)
    report = klt("drc", stream, "--deck", "sg13g2")
    saved = tmp_path / "drc.json"
    saved.write_text(json.dumps(report))
    assert klt("drc", "--check", str(saved), stream)["status"] == "match"
    rules = ("deck", "rules", "--deck", "sg13g2", "--rule", "cont.ongrid.1")
    before = klt(*rules)
    assert before["rules"][0]["limits"]["grid_um"] == 0.005
    assert before["content_hash"] == report["provenance"]["deck"]["content_hash"]

    hook = package / "decks/sg13g2_cont_grid.py"
    hook.write_bytes(hook.read_bytes().replace(b"grid_um=0.005", b"grid_um=0.010"))
    after = klt(*rules)
    assert after["rules"][0]["limits"]["grid_um"] == 0.010
    assert after["content_hash"] != before["content_hash"]
    check = klt("drc", "--check", str(saved), stream, expected_exit=3)
    deck_check = next(
        c for c in check["checks"] if c["field"] == "provenance.deck.content_hash"
    )
    assert (check["status"], deck_check["actual"]) == ("drifted", after["content_hash"])

    hook.unlink()
    missing = klt("deck", "hash", "--deck", "sg13g2", expected_exit=1)
    assert "no content hash" in missing["error"]["message"]
    assert (
        klt("deck", "hash", "--deck", "sky130")["content_hash"]
        == _provenance.deck_identity("sky130")["content_hash"]
    )


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
