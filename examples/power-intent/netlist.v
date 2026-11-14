module top (clk, iso_n, iso, d, o_a, o_b, o_c, o_ret);
  input clk, iso_n, iso, d; output o_a, o_b, o_c, o_ret;
  wire ga, gb, gc, gq, ga_buf, ret_q;
  // gated domain: xor0 -> ga, xor1 -> gb, xor2 -> gc, ff_g -> gq
  sky130_fd_sc_hd__xor2_1 xor0 (.A(d), .B(d), .X(ga));
  sky130_fd_sc_hd__xor2_1 xor1 (.A(d), .B(d), .X(gb));
  sky130_fd_sc_hd__xor2_1 xor2 (.A(d), .B(d), .X(gc));
  sky130_fd_sc_hd__dfxtp_1 ff_g (.CLK(clk), .D(d), .Q(gq));
  // isolation: ga clamped low (through a buffer), gb clamped high, gc unclamped
  sky130_fd_sc_hd__buf_1 b0 (.A(ga), .X(ga_buf));
  sky130_fd_sc_hd__and2_1 iso_a (.A(ga_buf), .B(iso_n), .X(o_a));
  sky130_fd_sc_hd__or2_1 iso_b (.A(gb), .B(iso), .X(o_b));
  sky130_fd_sc_hd__buf_1 leak_c (.A(gc), .X(o_c));
  // retained flop, fed through the isolation clamp
  sky130_fd_sc_hd__dfxtp_1 ff_r (.CLK(clk), .D(o_a), .Q(o_ret));
endmodule
