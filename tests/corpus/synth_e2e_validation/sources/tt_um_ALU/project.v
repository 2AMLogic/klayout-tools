/*
 * Copyright (c) 2024 Your Name
 * SPDX-License-Identifier: Apache-2.0
 */

`define default_netname none

module tt_um_ALU (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // will go high when the design is enabled
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);

  // All output pins must be assigned. If not used, assign to 0.
    ALU U3 (.A(ui_in[2:0]), .B(ui_in[5:3]), .ctrl(uio_in[2:0]), .Leds(uo_out[5:0]));
    
    assign uio_oe= 8'b 00111111;
    assign uo_out[7:6]= 2'b00;
    assign uio_out[7:0]= 8'b00000000;
    
endmodule
