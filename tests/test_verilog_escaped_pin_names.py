"""Escaped scalar cell pins in named gate-level instance connections (#7491)."""

import pytest

from klayout_tools.verilog_netlist import (
    VerilogNetlistError,
    convert_gate_level_verilog,
    parse_gate_level_verilog,
    parse_subckt_pin_orders,
)


@pytest.mark.parametrize("input_pin", ["A", r"\A "])
def test_escaped_bit_pins_bind_in_library_order(input_pin):
    text = rf"""
module top (i, n0, n1);
  input i;
  output n0, n1;
  macro u1 (.\DOUT[1] (n1), .{input_pin}(i), .\DOUT[0] (n0));
endmodule
"""
    instance = parse_gate_level_verilog(text)[0]["instances"][0]
    assert instance["connections"] == {"DOUT[1]": "n1", "A": "i", "DOUT[0]": "n0"}

    orders = parse_subckt_pin_orders(".SUBCKT macro DOUT[0] A DOUT[1]\n.ENDS macro\n")
    spice = convert_gate_level_verilog(text, pin_order_lookup=orders.get)
    assert "Xu1 n0 i n1 macro" in spice.splitlines()


@pytest.mark.parametrize("bad_pin", ["1bad", "", "DOUT[0]", r"\DOUT[0]"])
def test_invalid_pin_name_explains_identifier_syntax(bad_pin):
    text = f"module top (i); input i; macro u1 (.{bad_pin}(i) ); endmodule"
    with pytest.raises(VerilogNetlistError, match=r"instance 'u1'.*pin.*identifier"):
        parse_gate_level_verilog(text)
