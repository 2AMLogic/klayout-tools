"""``klt netlist --no-provenance-header`` (issue #5085)."""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.netlist import run_netlist
from test_netlist import (  # noqa: F401
    _NETLIST_BODY,
    _STUB_NONZERO_EXIT,
    _write_schematic,
    _write_stub,
)


@pytest.fixture
def argv_log(tmp_path, monkeypatch):
    log = tmp_path / "argv.log"
    monkeypatch.setenv("ARGV_LOG", str(log))
    monkeypatch.setenv("NETLIST_BODY", _NETLIST_BODY)
    return log


def _setup(tmp_path):
    return (
        _write_schematic(tmp_path),
        _write_stub(tmp_path, _STUB_NONZERO_EXIT),
        tmp_path / "block.spice",
    )


def test_default_writes_the_header(tmp_path, argv_log):
    schematic, binary, output = _setup(tmp_path)
    report = run_netlist(schematic, str(output), xschem_binary=binary)
    assert output.read_text().startswith("** klt-netlist-inputs: ")
    assert report["provenance_header"] is True


def test_flag_omits_header_and_hashes_written_bytes(tmp_path, argv_log):
    schematic, binary, output = _setup(tmp_path)
    report = run_netlist(
        schematic, str(output), xschem_binary=binary, provenance_header=False
    )
    text = output.read_text()
    assert "klt-netlist-inputs" not in text
    assert text.rstrip("\n") == _NETLIST_BODY.rstrip("\n")
    assert report["provenance_header"] is False
    assert report["netlist"]["bytes"] == len(output.read_bytes())


def test_check_passes_for_headerless_committed_netlist(tmp_path, argv_log):
    schematic, binary, output = _setup(tmp_path)
    output.write_text(_NETLIST_BODY + "\n", encoding="utf-8")
    report = run_netlist(
        schematic,
        str(output),
        check=True,
        xschem_binary=binary,
        provenance_header=False,
    )
    assert report["status"] == "match"
    assert report["drift"]["inputs"]["committed"] is None


def test_block_mode_with_flag(tmp_path, argv_log):
    schematic, binary, output = _setup(tmp_path)
    run_netlist(
        schematic,
        str(output),
        xschem_binary=binary,
        block=True,
        provenance_header=False,
    )
    assert "klt-netlist-inputs" not in output.read_text()


def test_cli_flag(tmp_path, argv_log, capsys):
    schematic, binary, output = _setup(tmp_path)
    code = main(
        [
            "netlist",
            schematic,
            "-o",
            str(output),
            "--xschem-binary",
            binary,
            "--no-provenance-header",
            "--format",
            "json",
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["provenance_header"] is False
    assert "klt-netlist-inputs" not in output.read_text()
