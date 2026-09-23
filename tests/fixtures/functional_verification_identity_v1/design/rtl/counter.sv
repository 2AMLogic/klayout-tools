// Free-running counter RTL used by the #2097 identity-contract fixtures.
//
// This file is fixture *bytes*, not a design that is ever compiled: the
// contract tests hash it, mutate it, and relocate it, but never hand it to a
// simulator (issue #2097: the contract pilot must be testable without one).
`include "counter_defs.svh"

module counter (
    input  wire             clk,
    input  wire             rst_n,
    output reg  [WIDTH-1:0] count
);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            count <= {WIDTH{1'b0}};
        end else begin
            count <= count + 1'b1;
        end
    end
endmodule
