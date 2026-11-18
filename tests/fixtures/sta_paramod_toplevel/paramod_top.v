// Issue #5708 fixture: a netlist whose TOP module is a Yosys-style
// `$paramod` escaped identifier. The module NAME is
//   $paramod\some_module\N=s32'00000000000000000000000000000010
// -- the exact shape Yosys emits for every parameterized module. In the
// Verilog source it is written as an escaped identifier (one leading
// backslash, terminated by the whitespace before the port list); the
// single quotes and `=` are part of the name.
//
// Body is the captured `sta_worst_paths` boundary design (see
// `boundary_captures.json`), so an engine linking this top reproduces the
// same worst-path identities the plain-name live run asserts.
module \$paramod\some_module\N=s32'00000000000000000000000000000010 (clk, rst_n, a_in, b_in, bus, q_a, q_b, q_bus);
  input clk, rst_n, a_in, b_in;
  input [1:0] bus;
  output q_a, q_b;
  output [1:0] q_bus;
  sky130_fd_sc_hd__dfrtp_1 ff_a (.CLK(clk), .D(a_in), .RESET_B(rst_n), .Q(q_a));
  sky130_fd_sc_hd__dfxtp_1 ff_b (.CLK(clk), .D(b_in), .Q(q_b));
  sky130_fd_sc_hd__dfxtp_1 ff_bus0 (.CLK(clk), .D(bus[0]), .Q(q_bus[0]));
  sky130_fd_sc_hd__dfxtp_1 ff_bus1 (.CLK(clk), .D(bus[1]), .Q(q_bus[1]));
endmodule
