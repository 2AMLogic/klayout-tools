// 16x16 multiply-accumulate, one combinational multiply per cycle.
//
// Written for issue #809's STA corpus slice: the point of this design is a
// deep *arithmetic* register-to-register cone (a full 16x16 array multiply
// plus a 40-bit accumulate, all between one pair of register banks), which is
// the shape whose critical path an STA engine is most likely to get wrong --
// long carry chains with heavily reconverging fanout, and by far the largest
// cell count in the slice.
module mac16 (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        en,
    input  wire        clear,
    input  wire [15:0] a,
    input  wire [15:0] b,
    output reg  [39:0] acc
);

  reg [15:0] a_q, b_q;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      a_q <= 16'd0;
      b_q <= 16'd0;
    end else if (en) begin
      a_q <= a;
      b_q <= b;
    end
  end

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) acc <= 40'd0;
    else if (clear) acc <= 40'd0;
    else if (en) acc <= acc + {8'd0, a_q * b_q};
  end

endmodule
