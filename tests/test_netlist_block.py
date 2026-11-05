"""`klt netlist --block` output shape (split from test_netlist.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from klayout_tools.netlist import run_netlist
from test_netlist import (  # noqa: F401
    _NETLIST_BODY,
    _STUB_NONZERO_EXIT,
    _TESTBENCH_SHAPED_BODY,
    _write_schematic,
    _write_stub,
    _written,
)


@pytest.fixture
def argv_log(tmp_path, monkeypatch) -> Path:
    """Same contract as test_netlist.py's fixture: stubs log their argv here."""
    log = tmp_path / "argv.log"
    monkeypatch.setenv("ARGV_LOG", str(log))
    monkeypatch.setenv("NETLIST_BODY", _NETLIST_BODY)
    return log


def test_block_mode_uncomments_subckt_and_drops_trailing_end(
    tmp_path, argv_log, monkeypatch
):
    monkeypatch.setenv("NETLIST_BODY", _TESTBENCH_SHAPED_BODY)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"

    report = run_netlist(schematic, str(output), xschem_binary=binary, block=True)

    written = output.read_text(encoding="utf-8")
    assert written == _written(
        "* stub netlist\n.subckt block a b\n.something x\n.ends\n"
    )
    # includable: no trailing .end to terminate an including testbench deck.
    assert ".end" not in written.splitlines()

    block = report["block"]
    assert block == {
        "requested": True,
        "subckt_uncommented": True,
        "ends_uncommented": True,
        "trailing_end_removed": True,
        "warning": None,
    }


def test_block_mode_rejoins_wrapped_header_continuations(
    tmp_path, argv_log, monkeypatch
):
    body = (
        "* stub netlist\n**.subckt block a b\n*+ c d\n**+ e f\n.something x\n"
        "**.ends\n.end"
    )
    monkeypatch.setenv("NETLIST_BODY", body)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"

    report = run_netlist(schematic, str(output), xschem_binary=binary, block=True)

    assert output.read_text(encoding="utf-8") == _written(
        "* stub netlist\n.subckt block a b\n+ c d\n+ e f\n.something x\n.ends\n"
    )
    assert report["block"]["warning"] is None


def test_block_mode_leaves_non_adjacent_continuation_comment_alone(
    tmp_path, argv_log, monkeypatch
):
    body = "* stub netlist\n**.subckt block a b\n.something x\n*+ note\n**.ends\n.end"
    monkeypatch.setenv("NETLIST_BODY", body)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"

    run_netlist(schematic, str(output), xschem_binary=binary, block=True)

    assert output.read_text(encoding="utf-8") == _written(
        "* stub netlist\n.subckt block a b\n.something x\n*+ note\n.ends\n"
    )


def test_without_block_flag_testbench_shape_is_unchanged(
    tmp_path, argv_log, monkeypatch
):
    """Regression: `--block` is off by default, so the raw (commented
    subckt/ends, trailing .end) testbench export is unaffected."""
    monkeypatch.setenv("NETLIST_BODY", _TESTBENCH_SHAPED_BODY)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"

    report = run_netlist(schematic, str(output), xschem_binary=binary)

    assert output.read_text(encoding="utf-8") == _written(_TESTBENCH_SHAPED_BODY + "\n")
    assert "block" not in report
