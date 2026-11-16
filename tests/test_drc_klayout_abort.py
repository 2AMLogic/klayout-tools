"""``klayout_deck_aborted`` structured abort-cause tests (issue #5091; split
from test_drc_klayout_engine.py for the file-size ratchet)."""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.drc import DrcError, run_drc_klayout_engine
from klayout_tools.drc_klayout_abort import deck_abort_info
from klayout_tools.drc_klayout_engine import _klayout_deck_error_lines
from test_drc_klayout_engine import (
    _EMPTY_RDB,
    _stub_klayout_drc_subprocess,
    _write_deck_file,
    _write_gds,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_GF180_3LM_ABORT_STDERR = (
    "Executing deck mslot1 from /pdk/rule_decks/mslot.rb\n"
    "ERROR: In /pdk/tech/drc/gf180mcu.drc: undefined local variable or "
    "method `metal4_drawn' for main:Object\n"
    "  /pdk/tech/drc/gf180mcu.drc:191:in `execute'\n"
)
_GF180_3LM_LOG = (
    "Executing deck antenna from /pdk/rule_decks/antenna.rb\n" + _GF180_3LM_ABORT_STDERR
)


def test_deck_abort_info_parses_deck_symbol_and_location():
    lines = _klayout_deck_error_lines("", _GF180_3LM_LOG)
    info = deck_abort_info("", _GF180_3LM_LOG, lines)
    assert info["decks_executed"] == ["antenna", "mslot1"]
    assert info["abort"]["deck"] == "mslot1"
    assert info["abort"]["deck_file"] == "/pdk/rule_decks/mslot.rb"
    assert info["abort"]["symbol"] == "metal4_drawn"
    assert info["abort"]["location"] == "/pdk/tech/drc/gf180mcu.drc:191"


def test_deck_abort_info_empty_without_deck_lines_or_errors():
    assert deck_abort_info("all fine\n", None, []) == {}


def test_deck_abort_info_without_deck_lines_omits_deck_fields():
    out = "ERROR: boom\n"
    info = deck_abort_info(out, None, _klayout_deck_error_lines(out))
    assert "decks_executed" not in info
    assert info["abort"] == {"message": "ERROR: boom"}


def test_deck_abort_info_scans_full_output_beyond_detail_cap():
    noise = "".join(f"rule {i}\n" for i in range(800))
    out = "Executing deck first from /a/first.rb\n" + noise + _GF180_3LM_ABORT_STDERR
    info = deck_abort_info(out, None, _klayout_deck_error_lines(out))
    assert info["decks_executed"] == ["first", "mslot1"]


def test_klayout_engine_deck_abort_raises_coded_error_with_details(
    tmp_path, monkeypatch
):
    _stub_klayout_drc_subprocess(
        monkeypatch, rdb_xml=_EMPTY_RDB, returncode=1, stderr=_GF180_3LM_LOG
    )
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    with pytest.raises(DrcError) as excinfo:
        run_drc_klayout_engine(gds, deck_file)
    assert excinfo.value.code == "klayout_deck_aborted"
    assert excinfo.value.details["abort"]["symbol"] == "metal4_drawn"


def test_cli_drc_klayout_deck_abort_envelope_carries_structured_cause(
    tmp_path, monkeypatch, capsys
):
    _stub_klayout_drc_subprocess(
        monkeypatch, rdb_xml=_EMPTY_RDB, returncode=1, stderr=_GF180_3LM_LOG
    )
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    exit_code = main(
        [
            "drc",
            gds,
            "--engine",
            "klayout",
            "--deck-file",
            deck_file,
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "klayout_deck_aborted"
    assert error["abort"]["deck"] == "mslot1"
    assert error["decks_executed"] == ["antenna", "mslot1"]
    assert "klayout reported an error" in error["message"]


def test_klayout_engine_allow_deck_errors_records_abort_and_decks(
    tmp_path, monkeypatch
):
    _stub_klayout_drc_subprocess(
        monkeypatch, rdb_xml=_EMPTY_RDB, returncode=1, stderr=_GF180_3LM_LOG
    )
    gds = _write_gds(tmp_path / "test.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    report = run_drc_klayout_engine(gds, deck_file, allow_deck_errors=True)

    assert report["status"] != "clean"
    errs = report["engine_deck_errors"]
    assert errs["decks_executed"] == ["antenna", "mslot1"]
    assert errs["abort"]["location"] == "/pdk/tech/drc/gf180mcu.drc:191"
