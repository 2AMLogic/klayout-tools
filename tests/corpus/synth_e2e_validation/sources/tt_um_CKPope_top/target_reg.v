module target_reg
(
	input wire clk, 
	input wire load, 
	input wire [3:0] data,
	input wire reset,
	output reg [3:0] target_reg
);

	// Reset if needed, increment or decrement if counter is not saturated
	always @ (posedge clk)
	begin
		if (reset)
			target_reg <= 4'b0000;
		else if (load)
		   target_reg <= data ;
	end

endmodule
