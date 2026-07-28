import json

from klayout_tools import __version__
from klayout_tools.cli import main


def test_version_matches_package():
    assert __version__


def test_cli_runs(capsys):
    assert main([]) == 0
    assert "scaffold" in capsys.readouterr().out


def test_cli_json_contract(capsys):
    assert main(["--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["name"] == "klayout-tools"
    assert data["version"] == __version__
