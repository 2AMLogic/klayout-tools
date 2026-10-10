// SPDX-License-Identifier: MIT
// Observability negative-control fixture for issue #2746 (design spike,
// docs/design/synthesize-observability-spike.md). Original RTL, MIT (this repo).
//
// State inventory (28 storage bits total):
//   acc_q            8 bits  functional (drives `acc`)
//   probe_q          8 bits  observation-only (drives only `dbg_probe`)
//   shared_q         4 bits  functional AND observed (drives `parity` and `dbg_shared`)
//   u_func.probe_q   4 bits  functional (drives `delayed`), same module as u_hist
//   u_hist.probe_q   4 bits  observation-only (drives only `dbg_hist`)
//
// The name `probe_q` deliberately appears in three places (obs_core and two
// instances of probe_unit) so name-selector ambiguity is observable.

module probe_unit (
    input  wire       clk,
    input  wire [3:0] d,
    output wire [3:0] q
);
    reg [3:0] probe_q;
    always @(posedge clk) probe_q <= d;
    assign q = probe_q;
endmodule

module obs_core (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       en,
    input  wire [7:0] din,
    // functional outputs
    output wire [7:0] acc,
    output wire       parity,
    output wire [3:0] delayed,
    // observation outputs
    output wire [7:0] dbg_probe,
    output wire [3:0] dbg_shared,
    output wire [3:0] dbg_hist
);
    reg [7:0] acc_q;
    reg [7:0] probe_q;
    reg [3:0] shared_q;

    always @(posedge clk or negedge rst_n)
        if (!rst_n)  acc_q <= 8'd0;
        else if (en) acc_q <= acc_q + din;

    always @(posedge clk) probe_q <= acc_q ^ din;

    always @(posedge clk or negedge rst_n)
        if (!rst_n) shared_q <= 4'd0;
        else        shared_q <= din[3:0] ^ din[7:4];

    assign acc        = acc_q;
    assign parity     = ^shared_q;
    assign dbg_probe  = probe_q;
    assign dbg_shared = shared_q;

    probe_unit u_func (.clk(clk), .d(din[7:4]),   .q(delayed));
    probe_unit u_hist (.clk(clk), .d(acc_q[3:0]), .q(dbg_hist));
endmodule
