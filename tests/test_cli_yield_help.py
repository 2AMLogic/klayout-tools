"""Yield verbs' help must not claim the native wheel is unpublished (#5028)."""

import pytest

from klayout_tools.cli.parser import create_parser

VERBS = ["yield", "yield-campaign", "yield-sensitivity"]
STALE = ["not yet on pypi", "supported install today", "separately built"]


@pytest.mark.parametrize("verb", VERBS)
def test_yield_help_has_no_stale_wheel_claim(verb, capsys):
    with pytest.raises(SystemExit):
        create_parser().parse_args([verb, "--help"])
    out = " ".join(capsys.readouterr().out.lower().split())
    for text in STALE:
        assert text not in out


def test_yield_help_names_extra_install_path(capsys):
    with pytest.raises(SystemExit):
        create_parser().parse_args(["yield", "--help"])
    out = "".join(capsys.readouterr().out.split())
    assert "klayout-tools[yield]" in out


def test_top_level_help_has_no_stale_claim(capsys):
    with pytest.raises(SystemExit):
        create_parser().parse_args(["--help"])
    out = " ".join(capsys.readouterr().out.lower().split())
    assert "separately built" not in out
