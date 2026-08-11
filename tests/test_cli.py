from importlib.metadata import version

import pytest

from klayout_tools import __version__
from klayout_tools.cli import main
from klayout_tools.cli.parser import create_parser
from klayout_tools.gen_compose import SUPPORTED_PLACEMENT_STRATEGIES


def test_version_matches_package():
    assert __version__ == version("klayout-tools")


def test_cli_no_subcommand_prints_scaffold(capsys):
    assert main([]) == 0
    assert "scaffold" in capsys.readouterr().out


def test_cli_version_flag(capsys):
    # argparse's `action="version"` prints and raises SystemExit(0).
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_gen_compose_help_mentions_every_supported_placement_strategy():
    # Regression for #683: the `gen-compose` subcommand's description text
    # once claimed placement.strategy "row" was the only supported value
    # after "explicit" placement shipped. Assert every strategy currently
    # implemented in gen_compose.SUPPORTED_PLACEMENT_STRATEGIES is named in
    # the parser's description, so this can't silently drift again.
    parser = create_parser()
    subparsers_action = next(
        action
        for action in parser._subparsers._group_actions
        if action.dest == "command"
    )
    gen_compose_parser = subparsers_action.choices["gen-compose"]
    description = gen_compose_parser.description or ""

    assert SUPPORTED_PLACEMENT_STRATEGIES, "expected at least one strategy to check"
    for strategy in SUPPORTED_PLACEMENT_STRATEGIES:
        assert f'"{strategy}"' in description, (
            f"gen-compose parser description does not mention placement "
            f"strategy {strategy!r} -- see SUPPORTED_PLACEMENT_STRATEGIES "
            f"in gen_compose.py"
        )
