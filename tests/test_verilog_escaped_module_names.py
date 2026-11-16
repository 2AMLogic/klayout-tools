"""Escaped-identifier module names in gate-level Verilog (issue #4810)."""

from __future__ import annotations

from klayout_tools.verilog_module_names import reference_module_names
from klayout_tools.verilog_netlist import (
    _parse_with_names,
    convert_gate_level_verilog,
    parse_gate_level_verilog,
)

CHILD = "\\$paramod\\child\\W=s32'd64"

VERILOG = f"""
module {CHILD} (a, y);
  input a;
  output y;
  BUF u0 (.A(a), .X(y));
endmodule

module top (i, o);
  input i;
  output o;
  {CHILD} u_child (.a(i), .y(o));
endmodule
"""


def _lookup(cell):
    return ["A", "X"] if cell == "BUF" else None


def test_declaration_and_instantiation_share_one_subckt():
    spice = convert_gate_level_verilog(VERILOG, pin_order_lookup=_lookup)
    mapped = _parse_with_names(VERILOG)[1]
    assert mapped == {"$paramod\\child\\W=s32'd64": "_paramod_child_W_s32_d64"}
    assert ".SUBCKT _paramod_child_W_s32_d64 a y" in spice
    assert "Xu_child i o _paramod_child_W_s32_d64" in spice
    assert ".ENDS _paramod_child_W_s32_d64" in spice


def test_colliding_names_get_distinct_subckts():
    text = """
module \\a$b (x); input x; endmodule
module \\a#b (x); input x; endmodule
module top (i); input i; \\a$b u1 (.x(i)); \\a#b u2 (.x(i)); endmodule
"""
    mapped = _parse_with_names(text)[1]
    assert len(set(mapped.values())) == 2
    modules = parse_gate_level_verilog(text)
    top = modules[-1]
    cells = [inst["cell"] for inst in top["instances"]]
    assert cells == [mapped["a$b"], mapped["a#b"]]
    assert cells[0] != cells[1]


def test_plain_names_have_empty_map():
    text = "module m (a); input a; endmodule\n"
    assert _parse_with_names(text)[1] == {}


def test_reference_module_names_report_field(tmp_path):
    path = tmp_path / "ref.v"
    path.write_text(VERILOG)
    out = reference_module_names(str(path), "gate-level-verilog")
    assert out == {
        "reference_module_names": {
            "$paramod\\child\\W=s32'd64": "_paramod_child_W_s32_d64"
        }
    }
    assert reference_module_names(str(path), "spice") == {}
    plain = tmp_path / "plain.v"
    plain.write_text("module m (a); input a; endmodule\n")
    assert reference_module_names(str(plain), "gate-level-verilog") == {}


def test_escaped_module_reference_reads_into_a_netlist(tmp_path):
    """The reference reader reaches a real `kdb.Netlist` (the point where an
    LVS compare starts) with the escaped module as a distinct sub-circuit."""
    from klayout_tools import lvs

    path = tmp_path / "ref.v"
    path.write_text(VERILOG)
    netlist = lvs._read_reference_netlist(
        str(path),
        form="gate-level-verilog",
        pin_orders={"BUF": ["A", "X"]},
    )
    names = {c.name.upper() for c in netlist.each_circuit()}
    assert "_PARAMOD_CHILD_W_S32_D64" in names
    assert "TOP" in names
