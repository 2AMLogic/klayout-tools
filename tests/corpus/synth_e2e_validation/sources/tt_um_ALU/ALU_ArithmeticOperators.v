`timescale 1ns / 1ps
module arithmetic_operators (input [2:0]A,B, output reg [3:0]Y1_ADD, output reg[5:0]Y3_MULT, 
                                  output reg[2:0]Y2_MIN, Y4_DIV, Y5_MOD);
always@(A,B)
begin
Y1_ADD = A + B;
Y2_MIN = A - B;
Y3_MULT = A * B;
Y4_DIV = A / B;
Y5_MOD = A % B;
end
endmodule
