"""``klt netlist`` diagnostics capture and the ``--erc`` gate (issue #4953).

Split from ``test_netlist.py``, which is at its file-size ceiling.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from klayout_tools.cli import main
from klayout_tools.netlist import NetlistError, run_netlist
from klayout_tools.netlist_diagnostics import DIAGNOSTICS_TAIL_BYTES, diagnostics_block
from test_netlist import (  # noqa: F401  (argv_log is a fixture)
    _STUB_NONZERO_EXIT,
    _STUB_PREAMBLE,
    _write_schematic,
    _write_stub,
    argv_log,
)

pytestmark = pytest.mark.usefixtures("argv_log")

# --------------------------------------------------------------------------- #
# diagnostics + --erc (issue #4953)
# --------------------------------------------------------------------------- #

_STUB_ERC_OUTPUT = (
    _STUB_PREAMBLE
    + """
echo "Error: undriven node: #net1"
echo "some stderr note" >&2
printf '%s\\n' "$NETLIST_BODY" > "$out_dir/$stem.spice"
exit 10
"""
)


def test_diagnostics_captured_without_erc_and_output_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("NETLIST_BODY", "* stub\n.end")
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_ERC_OUTPUT)
    output = tmp_path / "b.spice"

    payload = run_netlist(schematic, str(output), xschem_binary=binary)

    assert payload["status"] == "generated"
    assert output.exists()
    assert "undriven node: #net1" in payload["diagnostics"]["stdout"]
    assert "some stderr note" in payload["diagnostics"]["stderr"]
    assert payload["diagnostics"]["stdout_truncated"] is False


def test_diagnostics_are_tail_capped():
    block = diagnostics_block("x" * 10000 + "END", "")
    assert block["stdout_truncated"] is True
    assert len(block["stdout"]) == DIAGNOSTICS_TAIL_BYTES
    assert block["stdout"].endswith("END")
    assert block["stderr"] == "" and block["stderr_truncated"] is False


def test_erc_fails_on_findings_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("NETLIST_BODY", "* stub\n.end")
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_ERC_OUTPUT)
    output = tmp_path / "b.spice"

    with pytest.raises(NetlistError) as excinfo:
        run_netlist(schematic, str(output), xschem_binary=binary, erc=True)

    assert excinfo.value.code == "erc_findings"
    assert "undriven node: #net1" in str(excinfo.value)
    assert excinfo.value.details["finding_count"] == 1
    assert not output.exists()


def test_erc_clean_run_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("NETLIST_BODY", "* stub\n.end")
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "b.spice"

    payload = run_netlist(schematic, str(output), xschem_binary=binary, erc=True)

    assert payload["status"] == "generated"
    assert payload["diagnostics"]["stdout"] == ""


def test_erc_with_check_mode_fails_on_findings(tmp_path, monkeypatch):
    monkeypatch.setenv("NETLIST_BODY", "* stub\n.end")
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_ERC_OUTPUT)
    output = tmp_path / "b.spice"
    output.write_text("* stub\n.end\n")

    with pytest.raises(NetlistError) as excinfo:
        run_netlist(schematic, str(output), xschem_binary=binary, check=True, erc=True)
    assert excinfo.value.code == "erc_findings"


def test_erc_rejected_with_batch_backend(tmp_path, capsys):
    schematic = _write_schematic(tmp_path)
    code = main(
        [
            "netlist",
            schematic,
            "-o",
            str(tmp_path / "o.spice"),
            "--erc",
            "--backend",
            "batch",
        ]
    )
    assert code == 1
    assert "--erc" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# real xschem: shorted pins (issue #4962)
# --------------------------------------------------------------------------- #

_SHORTED_PINS = Path(__file__).parent / "data" / "netlist" / "shorted_pins.sch"
_XSCHEM_DEVICES_DIRS = (
    "/usr/share/xschem/xschem_library",
    "/usr/local/share/xschem/xschem_library",
)


@pytest.mark.skipif(shutil.which("xschem") is None, reason="xschem not on PATH")
def test_erc_flags_shorted_pins_with_real_xschem(tmp_path):
    library = next((d for d in _XSCHEM_DEVICES_DIRS if Path(d).is_dir()), None)
    if library is None:
        pytest.skip("xschem device library not found")
    rcfile = tmp_path / "xschemrc"
    rcfile.write_text(f"set XSCHEM_LIBRARY_PATH {library}\n")

    with pytest.raises(NetlistError) as excinfo:
        run_netlist(
            str(_SHORTED_PINS), str(tmp_path / "o.spice"), rcfile=str(rcfile), erc=True
        )

    assert excinfo.value.code == "erc_findings"
    assert "Net shorted: A - B" in str(excinfo.value)
    assert not (tmp_path / "o.spice").exists()
