module keepout (
    input  wire       clk,
    input  wire [5:0] a,
    input  wire [5:0] b,
    input  wire       sub,
    output reg  [6:0] y
);
  reg [5:0] ar, br;
  reg sr;
  always @(posedge clk) begin
    ar <= a;
    br <= b;
    sr <= sub;
    y <= sr ? ({1'b0, ar} - {1'b0, br}) : ({1'b0, ar} + {1'b0, br});
  end
endmodule
