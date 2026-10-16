module top (clk, rst_n, d, q1, q2, q3, q4, q5);
  input clk, rst_n, d; output q1, q2, q3, q4, q5;
  wire r1, r2, g;
  sky130_fd_sc_hd__inv_1 i0 (.A(rst_n), .Y(r1));
  sky130_fd_sc_hd__inv_1 i1 (.A(r1), .Y(r2));
  sky130_fd_sc_hd__and2_1 a0 (.A(rst_n), .B(d), .X(g));
  sky130_fd_sc_hd__dfrtp_1 f1 (.CLK(clk), .D(d), .RESET_B(r2), .Q(q1));
  sky130_fd_sc_hd__dfrtp_1 f2 (.CLK(clk), .D(d), .RESET_B(g), .Q(q2));
  sky130_fd_sc_hd__dfxtp_1 f3 (.CLK(clk), .D(d), .Q(q3));
  sky130_fd_sc_hd__dfrtp_1 f4 (.CLK(clk), .D(d), .RESET_B(r1), .Q(q4));
  sky130_fd_sc_hd__dfrtp_1 f5 (.CLK(clk), .D(d), .RESET_B(1'b1), .Q(q5));
  sky130_sram_1kbyte_1rw1r_32x256_8 m0 (.clk0(clk));
endmodule
