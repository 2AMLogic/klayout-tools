"""Issue #4605: unquoted `.include` paths with a `-` are quoted before reading."""

from __future__ import annotations

import pytest

from klayout_tools.lvs import _read_reference_netlist
from klayout_tools.netlist_normalize import quote_spice_includes

CELL = ".subckt foo a b\nR1 a b 1k\n.ends\n"


def _circuit_names(netlist) -> set[str]:
    return {c.name for c in netlist.each_circuit()}


def test_quote_helper_rewrites_only_unquoted():
    text = (
        ".INCLUDE /x-y/c.spice\n.inc rel.sp\n"
        ".include '/q-r/c.sp'\n.include \"/d-e/c.sp\"\n"
    )
    out = quote_spice_includes(text, "/base-dir")
    assert out.splitlines() == [
        ".INCLUDE '/x-y/c.spice'",
        ".inc '/base-dir/rel.sp'",
        ".include '/q-r/c.sp'",
        '.include "/d-e/c.sp"',
    ]


def test_quote_helper_unchanged_returns_same_object():
    text = ".subckt t a\n.ends\n"
    assert quote_spice_includes(text, "/b") is text


def test_default_form_hyphenated_absolute_include(tmp_path):
    inc_dir = tmp_path / "a-b"
    inc_dir.mkdir()
    (inc_dir / "c.spice").write_text(CELL)
    ref = tmp_path / "ref.spice"
    original = f".include {inc_dir / 'c.spice'}\n.subckt t a b\nX1 a b foo\n.ends\n"
    ref.write_text(original)
    netlist = _read_reference_netlist(str(ref))
    assert {"FOO", "T"} <= {n.upper() for n in _circuit_names(netlist)}
    assert ref.read_text() == original


@pytest.mark.parametrize("form", ["plain-element", "subckt-call"])
def test_relative_include_resolves_against_reference_dir(tmp_path, form):
    d = tmp_path / "my-dir"
    d.mkdir()
    (d / "c.spice").write_text(CELL)
    ref = d / "ref.spice"
    ref.write_text(".include c.spice\n.subckt t a b\nX1 a b foo\n.ends\n")
    netlist = _read_reference_netlist(str(ref), form=form)
    assert "FOO" in {n.upper() for n in _circuit_names(netlist)}
