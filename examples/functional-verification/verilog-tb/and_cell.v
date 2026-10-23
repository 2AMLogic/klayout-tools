`timescale 1ns/1ps
// Leaf cell with a specify block, standing in for a std-cell model.
module and_cell(input A, input B, output Y);
  and (Y, A, B);
  specify
    (A => Y) = 0;
    (B => Y) = 0;
  endspecify
endmodule
