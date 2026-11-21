module sky130_fd_sc_hd__and2_1 (X, A, B, VPWR, VGND, VPB, VNB);
  output X; input A, B, VPWR, VGND, VPB, VNB;
  and and0 (X, A, B);
endmodule
module sky130_fd_sc_hd__or2_1 (X, A, B, VPWR, VGND, VPB, VNB);
  output X; input A, B, VPWR, VGND, VPB, VNB;
  or or0 (X, A, B);
endmodule
module sky130_fd_sc_hd__buf_1 (X, A, VPWR, VGND, VPB, VNB);
  output X; input A, VPWR, VGND, VPB, VNB;
  buf buf0 (X, A);
endmodule
module sky130_fd_sc_hd__xor2_1 (X, A, B, VPWR, VGND, VPB, VNB);
  output X; input A, B, VPWR, VGND, VPB, VNB;
  xor xor0 (X, A, B);
endmodule
module sky130_fd_sc_hd__dfxtp_1 (Q, CLK, D, VPWR, VGND, VPB, VNB);
  output Q; input CLK, D, VPWR, VGND, VPB, VNB;
  wire buf_Q;
  sky130_fd_sc_hd__udp_dff$P_pp$PG$N dff0 (buf_Q, D, CLK, , VPWR, VGND);
  buf buf0 (Q, buf_Q);
endmodule
