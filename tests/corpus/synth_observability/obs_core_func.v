// SPDX-License-Identifier: MIT
// Explicit wrapper/top variant for the issue #2746 spike: the caller states
// the observation-free interface in RTL by instantiating the unmodified
// obs_core and leaving every dbg_* output unconnected. Synthesize with
// hdl_toplevel = obs_core_func and sources = [obs_core.v, obs_core_func.v].
module obs_core_func (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       en,
    input  wire [7:0] din,
    output wire [7:0] acc,
    output wire       parity,
    output wire [3:0] delayed
);
    obs_core u_core (
        .clk(clk), .rst_n(rst_n), .en(en), .din(din),
        .acc(acc), .parity(parity), .delayed(delayed),
        .dbg_probe(), .dbg_shared(), .dbg_hist()
    );
endmodule
