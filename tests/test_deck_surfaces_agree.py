"""The deck-listing surfaces agree about which decks `klt drc` can run
(issue #3783)."""

from __future__ import annotations

import re

import pytest

from klayout_tools.cli import main
from klayout_tools.decks import (
    UnknownDeckError,
    deck_provides,
    drc_deck_names,
    get_deck,
)
from klayout_tools.decks.history import deck_info


def _help(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    with pytest.raises(SystemExit):
        main([*argv, "--help"])
    return re.sub(r"\s+", " ", capsys.readouterr().out)


def test_drc_help_lists_only_curated_decks(capsys: pytest.CaptureFixture[str]) -> None:
    text = _help(capsys, "drc")
    listed = re.search(r"currently: ([\w, ]+?)\)", text)
    assert listed is not None
    names = [n.strip() for n in listed.group(1).split(",")]
    assert names == drc_deck_names()
    for name in names:
        assert get_deck(name)
    assert "asap7" not in names
    assert "--engine klayout --deck-file" in text


def test_layout_metrics_help_excludes_asap7(capsys: pytest.CaptureFixture[str]) -> None:
    text = _help(capsys, "layout-metrics")
    drc_part = text.split("drc.violation_count field (currently:")[1].split(")")[0]
    assert "asap7" not in drc_part


def test_deck_info_provides() -> None:
    by_name = {d["deck"]: d for d in deck_info()["decks"]}
    assert by_name["asap7"]["provides"] == ["extraction", "klayout_drc"]
    assert by_name["sky130"]["provides"] == ["curated_drc", "extraction"]
    for name, entry in by_name.items():
        assert entry["provides"] == deck_provides(name)
        if "curated_drc" in entry["provides"]:
            assert get_deck(name)


def test_asap7_rejected_with_route_hint() -> None:
    with pytest.raises(UnknownDeckError, match=r"--engine klayout --deck-file"):
        get_deck("asap7")


def test_nonexistent_deck_keeps_unknown_message() -> None:
    with pytest.raises(UnknownDeckError, match=r"unknown deck 'nope'"):
        get_deck("nope")


def test_deck_rules_asap7_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["deck", "rules", "--deck", "asap7", "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert "--engine klayout --deck-file" in captured.out + captured.err
