"""Native DRC report replay restores the original deck's script globals."""

import json

import pytest

from klayout_tools.cli import main
from test_drc_klayout_engine import (
    _assert_setup,
    _categories_rdb,
    _stub_klayout_drc_subprocess,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def test_native_rerun_replays_deck_vars(tmp_path, monkeypatch, capsys):
    commands = []
    _stub_klayout_drc_subprocess(
        monkeypatch, rdb_xml=_categories_rdb("A"), captured_cmds=commands
    )
    gds, deck = _assert_setup(tmp_path)
    assert (
        main(
            [
                "drc",
                gds,
                "--engine",
                "klayout",
                "--deck-file",
                deck,
                "--deck-var",
                "decks=mslot",
                "--deck-var",
                "variant=A",
                "--expect-rule-categories",
                "1",
                "--format",
                "json",
            ]
        )
        == 0
    )
    path = tmp_path / "report.json"
    path.write_text(capsys.readouterr().out)
    assert json.loads(path.read_text())["provenance"]["deck"]["options"] == {
        "decks": "mslot",
        "variant": "A",
    }
    assert main(["drc", "--check", str(path), "--rerun", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "match"
    assert len(commands) == 2
    for args in commands:
        globals_ = [
            value for flag, value in zip(args, args[1:], strict=False) if flag == "-rd"
        ]
        assert "decks=mslot" in globals_ and "variant=A" in globals_


@pytest.mark.parametrize(
    "options",
    [["decks"], {"decks": True}, {"input": "other.gds"}, {"report": "other.rdb"}],
)
def test_invalid_recorded_options_fail_before_native_launch(
    tmp_path, monkeypatch, capsys, options
):
    from test_drc_rerun_klayout_provenance import _committed_report

    path, report = _committed_report(tmp_path, monkeypatch, capsys)
    report["provenance"]["deck"]["options"] = options
    path.write_text(json.dumps(report))
    commands = []
    _stub_klayout_drc_subprocess(monkeypatch, captured_cmds=commands)
    assert main(["drc", "--check", str(path), "--rerun", "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert "provenance.deck.options" in captured.out + captured.err
    assert commands == []
