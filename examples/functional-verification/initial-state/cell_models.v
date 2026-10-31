// Two sky130-shaped sequential cells with real UDP state (a cell-model UDP's
// state cannot be written through VPI -- hence options.initial_state).
primitive sky130_fd_sc_hd__udp_dff$PR_pp$PG$N (Q, D, CLK, RESET, NOTIFIER, VPWR, VGND);
  output Q; reg Q; input D, CLK, RESET, NOTIFIER, VPWR, VGND;
  table
    ? ? 1 ? ? ? : ? : 0;
    0 r 0 ? ? ? : ? : 0;
    1 r 0 ? ? ? : ? : 1;
    x r 0 ? ? ? : ? : x;
    ? n 0 ? ? ? : ? : -;
    * ? 0 ? ? ? : ? : -;
  endtable
endprimitive

primitive sky130_fd_sc_hd__udp_dff$P_pp$PG$N (Q, D, CLK, NOTIFIER, VPWR, VGND);
  output Q; reg Q; input D, CLK, NOTIFIER, VPWR, VGND;
  table
    0 r ? ? ? : ? : 0;
    1 r ? ? ? : ? : 1;
    x r ? ? ? : ? : x;
    ? n ? ? ? : ? : -;
    * ? ? ? ? : ? : -;
  endtable
endprimitive

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
