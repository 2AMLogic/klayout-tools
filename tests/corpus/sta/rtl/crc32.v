// Byte-parallel CRC-32 (IEEE 802.3 polynomial), one byte per cycle.
//
// Written for issue #809's STA corpus slice. Deliberately the *opposite*
// shape from mac16.v: a shallow, XOR-only register-to-register cone with very
// high fanout and reconvergence, and no carry chain at all. Every path is
// short and there are hundreds of them within a few picoseconds of each
// other, which stresses tie-breaking between near-equal endpoints rather than
// the depth of any single path.
module crc32 (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       en,
    input  wire [7:0] data,
    output reg [31:0] crc
);

  function [31:0] next_crc;
    input [31:0] c;
    input [7:0] d;
    integer i;
    reg [31:0] acc;
    begin
      acc = c ^ {24'd0, d};
      for (i = 0; i < 8; i = i + 1) begin
        acc = acc[0] ? ((acc >> 1) ^ 32'hEDB88320) : (acc >> 1);
      end
      next_crc = acc;
    end
  endfunction

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) crc <= 32'hFFFFFFFF;
    else if (en) crc <= next_crc(crc, data);
  end

endmodule
