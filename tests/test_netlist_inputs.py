"""Generation-input record in `klt netlist` output and `--check` drift (issue #4945)."""

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


def test_check_drift_reason_names_changed_generation_inputs(
    tmp_path, argv_log, monkeypatch
):
    """Issue #4945: a record from another xschem build is named in the reason."""
    monkeypatch.setenv("NETLIST_BODY", ".subckt a A B\nR1 A B 1k\n.ends\n.end")
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"
    output.write_text(
        '** klt-netlist-inputs: {"pdk_variant": null, "rcfile": null, '
        '"xschem_version": "3.4.5"}\n.subckt a A B\nR1 A B 2k\n.ends\n.end\n',
        encoding="utf-8",
    )

    report = run_netlist(schematic, str(output), check=True, xschem_binary=binary)

    assert report["status"] == "drifted"
    inputs = report["drift"]["inputs"]
    assert inputs["changed"] == ["xschem_version"]
    assert inputs["committed"]["xschem_version"] == "3.4.5"
    assert "xschem_version: 3.4.5 ->" in report["drift"]["reason"]
    # The record line is not part of the diff.
    assert not any("klt-netlist-inputs" in line for line in report["drift"]["diff"])


def test_check_drift_with_unchanged_inputs_says_so(tmp_path, argv_log, monkeypatch):
    monkeypatch.setenv("NETLIST_BODY", ".subckt a A B\nR1 A B 1k\n.ends\n.end")
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"
    output.write_text(
        _written(".subckt a A B\nR1 A B 2k\n.ends\n.end\n"), encoding="utf-8"
    )

    report = run_netlist(schematic, str(output), check=True, xschem_binary=binary)

    assert report["status"] == "drifted"
    assert report["drift"]["inputs"]["changed"] == []
    assert "unchanged" in report["drift"]["reason"]


def test_check_drift_on_legacy_netlist_reports_no_record(
    tmp_path, argv_log, monkeypatch
):
    monkeypatch.setenv("NETLIST_BODY", ".subckt a A B\nR1 A B 1k\n.ends\n.end")
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"
    output.write_text(".subckt a A B\nR1 A B 2k\n.ends\n.end\n", encoding="utf-8")

    report = run_netlist(schematic, str(output), check=True, xschem_binary=binary)

    assert report["status"] == "drifted"
    assert report["drift"]["inputs"]["committed"] is None
    assert report["drift"]["inputs"]["changed"] is None
    assert "records no generation inputs" in report["drift"]["reason"]


def test_generated_netlist_checks_as_match_against_itself(tmp_path, argv_log):
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"
    run_netlist(schematic, str(output), xschem_binary=binary)

    report = run_netlist(schematic, str(output), check=True, xschem_binary=binary)

    assert report["status"] == "match"
    assert report["drift"]["inputs"]["changed"] == []
