`timescale 1ns/1ps
// The block under test: one cell instance, the shape a P&R netlist has.
module and2(input a, input b, output y);
  and_cell u1(.A(a), .B(b), .Y(y));
endmodule
