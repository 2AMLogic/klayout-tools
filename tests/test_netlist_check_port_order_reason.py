"""`klt netlist --block --check`'s port-order drift reason (issue #5473).

Issue #4524 gave `--check` the drift entry and the `reason` that names it.
That reason attributed the permutation to "different xschem builds order
ports differently" as fact -- true of #4524's case, false of this one: with a
single xschem on both sides, a block's ports move when the sibling symbol's
pins are reordered. The old text sent a reader looking for a toolchain
difference that need not exist.

These cases pin the corrected wording. The comparison itself -- which cards
drift, the status, the exit code -- is #4524's and is unchanged; the last
case here says so.
"""

from __future__ import annotations

import pytest

from klayout_tools.netlist import run_netlist
from test_netlist import _STUB_NONZERO_EXIT, _write_stub
from test_netlist_block_port_order import _sheet, _symbol


@pytest.fixture
def argv_log(tmp_path, monkeypatch):
    """Same contract as test_netlist.py's fixture: stubs log their argv here."""
    log = tmp_path / "argv.log"
    monkeypatch.setenv("ARGV_LOG", str(log))
    return log


def _check_reason(tmp_path, monkeypatch, *, symbol, committed, fresh):
    schematic = tmp_path / "cell.sch"
    schematic.write_text(_sheet(), encoding="utf-8")
    if symbol is not None:
        (tmp_path / "cell.sym").write_text(_symbol(symbol), encoding="utf-8")
    monkeypatch.setenv("NETLIST_BODY", fresh)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "cell.spice"
    output.write_text(committed, encoding="utf-8")
    report = run_netlist(
        str(schematic),
        str(output),
        check=True,
        block=True,
        xschem_binary=binary,
    )
    return report


_FRESH = "**.subckt cell vdda vssa\n**.ends\n.end"
_FRESH_PORTS = ["vdda", "vssa"]
_COMMITTED = ".subckt cell vssa vdda\n.ends\n"


def test_check_reason_names_the_sibling_symbol_as_the_order_source(
    tmp_path, argv_log, monkeypatch
):
    """The corrected attribution: with one xschem on both sides, "different
    xschem builds order ports differently" sent the reader after a toolchain
    difference that need not exist."""
    report = _check_reason(
        tmp_path,
        monkeypatch,
        symbol=("vdda", "vssa"),
        committed=_COMMITTED,
        fresh=_FRESH,
    )

    assert report["status"] == "drifted"
    assert report["drift"]["subckt_port_order"] == [
        {"subckt": "cell", "committed": ["vssa", "vdda"], "regenerated": _FRESH_PORTS}
    ]
    reason = report["drift"]["reason"]
    assert "sibling symbol cell.sym" in reason
    assert "a different xschem build can also order ports differently" in reason
    assert "positional subckt-call callers" in reason


def test_check_reason_without_a_sibling_symbol_offers_both_causes(
    tmp_path, argv_log, monkeypatch
):
    """No symbol: an xschem build difference is one possible cause among
    others, stated as such rather than as the diagnosis."""
    report = _check_reason(
        tmp_path, monkeypatch, symbol=None, committed=_COMMITTED, fresh=_FRESH
    )

    reason = report["drift"]["reason"]
    assert "sibling" not in reason
    assert "pin order of its symbol" in reason
    assert "a different xschem build can order ports differently" in reason


def test_check_exit_contract_is_unchanged(tmp_path, argv_log, monkeypatch):
    """Rewording the reason is not a behaviour change: the drift entry, the
    status and therefore `--check`'s exit code stay exactly as #4524 set them."""
    report = _check_reason(
        tmp_path,
        monkeypatch,
        symbol=("vdda", "vssa"),
        committed=_COMMITTED,
        fresh=_FRESH,
    )

    assert report["status"] == "drifted"
    assert report["drift"]["subckt_port_order"][0]["subckt"] == "cell"
