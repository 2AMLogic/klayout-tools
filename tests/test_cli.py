import pytest

from klayout_tools import __version__
from klayout_tools.cli import main


def test_version_matches_package():
    assert __version__


def test_cli_no_subcommand_prints_scaffold(capsys):
    assert main([]) == 0
    assert "scaffold" in capsys.readouterr().out


def test_cli_version_flag(capsys):
    # argparse's `action="version"` prints and raises SystemExit(0).
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_layers_subcommand_requires_file():
    # The `layers` subcommand exists and requires a file argument;
    # argparse exits 2 when the required positional is missing.
    with pytest.raises(SystemExit):
        main(["layers"])
