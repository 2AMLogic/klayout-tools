
module Compx4
(
   input [3:0] a_hex,
	input [3:0] b_hex,
 	output reg a_eq_b,		
	output reg a_gt_b,		
	output reg a_lt_b	
); 
	
	wire [3:0] aeqb;	
	wire [3:0] agtb;	
	wire [3:0] altb;	

// connections to single bit comparators
	Compx1	Bit3_COMP(.a(a_hex[3]), .b(b_hex[3]), .aeqb(aeqb[3]), .agtb(agtb[3]), .altb(altb[3]));
	Compx1	Bit2_COMP(.a(a_hex[2]), .b(b_hex[2]), .aeqb(aeqb[2]), .agtb(agtb[2]), .altb(altb[2]));
	Compx1	Bit1_COMP(.a(a_hex[1]), .b(b_hex[1]), .aeqb(aeqb[1]), .agtb(agtb[1]), .altb(altb[1]));
	Compx1	Bit0_COMP(.a(a_hex[0]), .b(b_hex[0]), .aeqb(aeqb[0]), .agtb(agtb[0]), .altb(altb[0]));

		always @(*)
		begin
			a_eq_b = aeqb[3] & aeqb[2] & aeqb[1] & aeqb[0];
	
			a_gt_b = agtb[3] | (aeqb[3] & agtb[2]) | (aeqb[3] & aeqb[2] & agtb[1]) | (aeqb[3] & aeqb[2] & aeqb[1] & agtb[0]);
	
			a_lt_b = altb[3] | (aeqb[3] & altb[2]) | (aeqb[3] & aeqb[2] & altb[1]) | (aeqb[3] & aeqb[2] & aeqb[1] & altb[0]);
		end
		
	endmodule
