"""``klt netlist --symbol-path`` (issue #4952): local xschem export."""

# ruff: noqa: F811  (fixtures imported from the sibling test module)

from __future__ import annotations

import os

import pytest

from klayout_tools.netlist import NetlistError, run_netlist
from test_netlist import (  # noqa: F401  (argv_log is a fixture)
    _STUB_NONZERO_EXIT,
    _STUB_PREAMBLE,
    _write_schematic,
    _write_stub,
    argv_log,
)

# --------------------------------------------------------------------------- #
# --symbol-path (issue #4952)
# --------------------------------------------------------------------------- #

_STUB_RECORD_LIBPATH = (
    _STUB_PREAMBLE
    + """
printf '%s' "$XSCHEM_USER_LIBRARY_PATH" > "$LIBPATH_LOG"
printf '%s\\n' "$NETLIST_BODY" > "$out_dir/$stem.spice"
exit 1
"""
)


def test_symbol_path_is_exported_ahead_of_existing_value(
    tmp_path, argv_log, monkeypatch
):
    sym_a = tmp_path / "syms a"
    sym_b = tmp_path / "syms_b"
    sym_a.mkdir()
    sym_b.mkdir()
    log = tmp_path / "libpath.log"
    monkeypatch.setenv("LIBPATH_LOG", str(log))
    monkeypatch.setenv("XSCHEM_USER_LIBRARY_PATH", "/pre/existing")
    binary = _write_stub(tmp_path, _STUB_RECORD_LIBPATH)
    report = run_netlist(
        _write_schematic(tmp_path),
        str(tmp_path / "b.spice"),
        xschem_binary=binary,
        symbol_path=[str(sym_a), str(sym_b)],
    )
    expected = [os.path.realpath(sym_a), os.path.realpath(sym_b)]
    assert log.read_text(encoding="utf-8") == os.pathsep.join(
        [*expected, "/pre/existing"]
    )
    assert report["xschem"]["symbol_path"] == expected


def test_symbol_path_defaults_to_empty_and_leaves_env_alone(
    tmp_path, argv_log, monkeypatch
):
    monkeypatch.delenv("XSCHEM_USER_LIBRARY_PATH", raising=False)
    log = tmp_path / "libpath.log"
    monkeypatch.setenv("LIBPATH_LOG", str(log))
    binary = _write_stub(tmp_path, _STUB_RECORD_LIBPATH)
    report = run_netlist(
        _write_schematic(tmp_path), str(tmp_path / "b.spice"), xschem_binary=binary
    )
    assert log.read_text(encoding="utf-8") == ""
    assert report["xschem"]["symbol_path"] == []


def test_symbol_path_must_be_a_directory(tmp_path, argv_log):
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    missing = tmp_path / "nope"
    with pytest.raises(NetlistError, match="--symbol-path is not a directory"):
        run_netlist(
            _write_schematic(tmp_path),
            str(tmp_path / "b.spice"),
            xschem_binary=binary,
            symbol_path=[str(missing)],
        )
