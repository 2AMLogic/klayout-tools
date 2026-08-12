// 16 x 8-bit synchronous FIFO with flags.
//
// Written for issue #809's STA corpus slice. Control-dominated rather than
// arithmetic: pointer comparisons, a one-hot write decode, and a 16-way read
// mux over a register file. This is the design in the slice whose registers
// are *not* all in one clock domain shape -- the storage array's flip-flops
// are clock-enabled by a decoded write pointer, so most register data pins
// are reached through a mux rather than through logic, and the critical path
// runs through the read mux, not the pointers.
module fifo16 (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       push,
    input  wire       pop,
    input  wire [7:0] din,
    output wire [7:0] dout,
    output wire       full,
    output wire       empty
);

  reg [7:0] mem[0:15];
  reg [4:0] wptr, rptr;
  integer i;

  wire [3:0] waddr = wptr[3:0];
  wire [3:0] raddr = rptr[3:0];

  assign empty = (wptr == rptr);
  assign full = (wptr[3:0] == rptr[3:0]) && (wptr[4] != rptr[4]);
  assign dout = mem[raddr];

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      wptr <= 5'd0;
      rptr <= 5'd0;
      for (i = 0; i < 16; i = i + 1) mem[i] <= 8'd0;
    end else begin
      if (push && !full) begin
        mem[waddr] <= din;
        wptr <= wptr + 5'd1;
      end
      if (pop && !empty) rptr <= rptr + 5'd1;
    end
  end

endmodule
