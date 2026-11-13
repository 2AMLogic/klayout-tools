"""`klt netlist` unresolved-symbol PDK-env note under `--symbol-path` (issue #5441).

Split out of `test_netlist.py` to keep that module under its size ceiling. The
stub-`xschem` contract and helpers are shared with it; see its module docstring.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from klayout_tools.netlist import NetlistError, run_netlist
from test_netlist import (  # noqa: F401
    _MISSING_BODY,
    _NETLIST_BODY,
    _STUB_NONZERO_EXIT,
    _fake_pdk_root,
    _write_schematic,
    _write_stub,
)


@pytest.fixture
def argv_log(tmp_path, monkeypatch) -> Path:
    """Same contract as test_netlist.py's fixture: stubs log their argv here."""
    log = tmp_path / "argv.log"
    monkeypatch.setenv("ARGV_LOG", str(log))
    monkeypatch.setenv("NETLIST_BODY", _NETLIST_BODY)
    return log


def _symbol_path_error(tmp_path, monkeypatch, **kwargs) -> NetlistError:
    """Netlist a schematic whose symbol does not resolve, with `--symbol-path`.

    Returns the raised `NetlistError` so each test can assert on its message.
    `kwargs` are passed through to `run_netlist` (e.g. `pdk`/`pdk_root`).
    """
    monkeypatch.setenv("NETLIST_BODY", _MISSING_BODY)
    syms = tmp_path / "syms"
    syms.mkdir()
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    with pytest.raises(NetlistError) as excinfo:
        run_netlist(
            _write_schematic(tmp_path),
            str(tmp_path / "b.spice"),
            xschem_binary=binary,
            symbol_path=[str(syms)],
            **kwargs,
        )
    return excinfo.value


def test_unresolved_symbol_message_with_symbol_path_and_no_pdk(
    tmp_path, argv_log, monkeypatch
):
    """--symbol-path alone must not turn the error into a KeyError (#5441)."""
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.delenv("PDK_ROOT", raising=False)
    error = _symbol_path_error(tmp_path, monkeypatch)
    assert error.code == "unresolved_symbol"
    message = str(error)
    assert "--rcfile none" in message
    assert "PDK_ROOT=not set PDK=not set (inherited, not set by klt)" in message


def test_unresolved_symbol_message_labels_inherited_pdk_with_symbol_path(
    tmp_path, argv_log, monkeypatch
):
    """Inherited PDK vars are not "exported by klt" just because klt built an
    env for --symbol-path (#5441)."""
    monkeypatch.setenv("PDK_ROOT", "/inherited")
    monkeypatch.setenv("PDK", "sky130A")
    message = str(_symbol_path_error(tmp_path, monkeypatch))
    assert "PDK_ROOT=/inherited PDK=sky130A (inherited, not set by klt)" in message
    assert "(exported by klt)" not in message


@pytest.mark.parametrize("present", ["PDK_ROOT", "PDK"])
def test_unresolved_symbol_message_with_half_inherited_env(
    tmp_path, argv_log, monkeypatch, present
):
    """Only one of PDK_ROOT/PDK inherited must still not raise KeyError (#5441)."""
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.setenv(present, "/half" if present == "PDK_ROOT" else "sky130A")
    error = _symbol_path_error(tmp_path, monkeypatch)
    assert error.code == "unresolved_symbol"
    assert "(inherited, not set by klt)" in str(error)


def test_unresolved_symbol_message_with_symbol_path_and_resolved_pdk(
    tmp_path, argv_log, monkeypatch
):
    """A resolved PDK is still reported as exported by klt (#5441)."""
    root = _fake_pdk_root(tmp_path)
    message = str(
        _symbol_path_error(tmp_path, monkeypatch, pdk="sky130A", pdk_root=str(root))
    )
    assert f"PDK_ROOT={root} PDK=sky130A (exported by klt)" in message
