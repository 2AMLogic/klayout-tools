from importlib.metadata import version

import pytest

from klayout_tools import __version__, gen_compose
from klayout_tools.cli import main
from klayout_tools.cli.parser import create_parser


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


def test_gen_compose_help_lists_every_supported_placement_strategy():
    # Regression test for #683: `gen_compose_parser`'s description drifted
    # from `gen_compose.SUPPORTED_PLACEMENT_STRATEGIES` (it once claimed
    # "row" was the only strategy after "explicit" landed). Assert every
    # supported strategy name is mentioned so `--help` can't silently go
    # stale again the next time a strategy is added.
    parser = create_parser()
    subparsers_action = next(
        action
        for action in parser._actions
        if getattr(action, "choices", None) and "gen-compose" in action.choices
    )
    description = subparsers_action.choices["gen-compose"].description
    assert description is not None
    for strategy in gen_compose.SUPPORTED_PLACEMENT_STRATEGIES:
        assert f'"{strategy}"' in description, (
            f"gen-compose --help description does not mention placement "
            f"strategy {strategy!r}: {description!r}"
        )
