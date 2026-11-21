module sky130_fd_sc_hd__inv_1 (Y, A, VPWR, VGND, VPB, VNB);
  output Y; input A, VPWR, VGND, VPB, VNB;
  wire not0_out_Y;
  not not0 (not0_out_Y, A);
  buf buf0 (Y, not0_out_Y);
endmodule
module sky130_fd_sc_hd__and2_1 (X, A, B, VPWR, VGND, VPB, VNB);
  output X; input A, B, VPWR, VGND, VPB, VNB;
  and and0 (X, A, B);
endmodule
module sky130_fd_sc_hd__dfrtp_1 (Q, CLK, D, RESET_B, VPWR, VGND, VPB, VNB);
  output Q; input CLK, D, RESET_B, VPWR, VGND, VPB, VNB;
  wire buf_Q, RESET;
  not not0 (RESET, RESET_B);
  sky130_fd_sc_hd__udp_dff$PR_pp$PG$N dff0 (buf_Q, D, CLK, RESET, , VPWR, VGND);
  buf buf0 (Q, buf_Q);
endmodule
module sky130_fd_sc_hd__dfxtp_1 (Q, CLK, D, VPWR, VGND, VPB, VNB);
  output Q; input CLK, D, VPWR, VGND, VPB, VNB;
  wire buf_Q;
  sky130_fd_sc_hd__udp_dff$P_pp$PG$N dff0 (buf_Q, D, CLK, , VPWR, VGND);
  buf buf0 (Q, buf_Q);
endmodule
