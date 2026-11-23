module divider(input clk, input data, output base_q, output slow_q);
  wire div_q;
  wire toggle;
  sky130_fd_sc_hd__inv_1 inv (.A(div_q), .Y(toggle));
  sky130_fd_sc_hd__dfxtp_1 div_ff (.CLK(clk), .D(toggle), .Q(div_q));
  sky130_fd_sc_hd__dfxtp_1 source_ff (.CLK(clk), .D(data), .Q(base_q));
  sky130_fd_sc_hd__dfxtp_1 sink_ff (.CLK(div_q), .D(base_q), .Q(slow_q));
endmodule
