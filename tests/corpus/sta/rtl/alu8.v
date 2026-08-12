// 8-bit ALU with an accumulator and flags.
//
// Written for issue #809's STA corpus slice. Its shape is a wide *mux* of
// short cones rather than one deep cone: eight operations computed in
// parallel and selected, so the critical path runs through the select tree
// and the tools have many near-equal candidate paths to choose between. That
// is the case where two STA engines are most likely to report *different*
// startpoint/endpoint pairs at nearly the same delay -- which the comparison
// harness reports separately from the delay delta.
module alu8 (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       en,
    input  wire [2:0] op,
    input  wire [7:0] operand,
    output reg  [7:0] acc,
    output reg        zero,
    output reg        carry
);

  reg [8:0] result;

  always @* begin
    case (op)
      3'd0: result = {1'b0, acc} + {1'b0, operand};
      3'd1: result = {1'b0, acc} - {1'b0, operand};
      3'd2: result = {1'b0, acc & operand};
      3'd3: result = {1'b0, acc | operand};
      3'd4: result = {1'b0, acc ^ operand};
      3'd5: result = {acc, 1'b0};
      3'd6: result = {1'b0, 1'b0, acc[7:1]};
      default: result = {1'b0, operand};
    endcase
  end

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      acc   <= 8'd0;
      zero  <= 1'b1;
      carry <= 1'b0;
    end else if (en) begin
      acc   <= result[7:0];
      zero  <= (result[7:0] == 8'd0);
      carry <= result[8];
    end
  end

endmodule
