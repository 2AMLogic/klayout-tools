module ud_counter
(
	input wire clk, 
	input wire count_en, 
	input wire count_up1_dwn0, 
	input wire reset,
	output reg [3:0] count
);

	// Reset if needed, increment or decrement if counter is not saturated
	always @ (posedge clk)
	begin
		if (reset)
			count <= 4'b0000;
		else if ((count_en & count_up1_dwn0) & (count!= 4'b1111))
			count <= count + 4'd0001;
		else if ((count_en & (!count_up1_dwn0)) & (count!= 4'b0000))
			count <= count - 4'd0001;
	end

endmodule
