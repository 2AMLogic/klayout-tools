// Flat gate-level netlist: one async-reset flop and one flop with no reset.
module top (clk, rst_n, d_rst, d_free, q_rst, q_free);
  input clk, rst_n, d_rst, d_free;
  output q_rst, q_free;
  sky130_fd_sc_hd__dfrtp_1 f_rst (.CLK(clk), .D(d_rst), .RESET_B(rst_n), .Q(q_rst));
  sky130_fd_sc_hd__dfxtp_1 f_free (.CLK(clk), .D(d_free), .Q(q_free));
endmodule
