"""`klt deck path`: the installed ASAP7 DRC-DSL script path (issue #3789)."""

from __future__ import annotations

import json
import os
import shutil

import pytest

from klayout_tools._provenance import sha256_file
from klayout_tools.cli import main


def test_path_json_shape_and_file_exists(capsys):
    assert main(["deck", "path", "--deck", "asap7", "--format", "json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema_version"] == 1
    assert out["deck"] == "asap7"
    assert os.path.isabs(out["path"])
    assert out["path"].endswith("asap7.drc")
    assert os.path.isfile(out["path"])
    assert out["content_hash"] == f"sha256:{sha256_file(out['path'])}"


def test_path_text_is_bare_path(capsys):
    assert main(["deck", "path", "--deck", "asap7"]) == 0
    printed = capsys.readouterr().out.strip()
    assert os.path.isfile(printed)


@pytest.mark.parametrize("deck", ["sky130", "nope"])
def test_path_errors_exit_1(deck, capsys):
    assert main(["deck", "path", "--deck", deck, "--format", "json"]) == 1
    out = json.loads(capsys.readouterr().err)
    assert out["error"]["command"] == "deck path"


@pytest.mark.skipif(
    shutil.which("klayout") is None,
    reason="klayout binary is not installed on this machine",
)
def test_printed_path_is_accepted_by_drc(tmp_path, capsys):
    import klayout.db as kdb

    gds = tmp_path / "t.gds"
    layout = kdb.Layout()
    layout.create_cell("TOP")
    layout.write(str(gds))
    assert main(["deck", "path", "--deck", "asap7"]) == 0
    deck = capsys.readouterr().out.strip()
    rc = main(
        [
            "drc",
            str(gds),
            "--engine",
            "klayout",
            "--deck-file",
            deck,
            "--format",
            "json",
        ]
    )
    # An empty layout exits non-zero (coverage_unknown); what matters is that
    # klt loaded the printed deck and ran it rather than rejecting the path.
    captured = capsys.readouterr()
    assert captured.out.strip(), f"drc printed no JSON (rc={rc}): {captured.err}"
    report = json.loads(captured.out)
    assert report["deck"] == deck
    assert report["engine"] == "klayout"
    assert report["coverage"]["rule_categories"]
    assert rc != 2
