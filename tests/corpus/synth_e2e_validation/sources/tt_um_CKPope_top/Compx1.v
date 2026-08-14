
module Compx1
(
   input a,
	input b,
 	output reg aeqb,		
	output reg agtb,		
	output reg altb		
	
); 

always @(*)
begin
	aeqb = (a & b) | ((!(a)) & (!(b)));
	
	agtb = a & (!(b));
		
	altb = (!(a)) & b ;
end		
endmodule
