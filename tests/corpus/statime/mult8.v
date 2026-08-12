// A minimal 8x8 combinational multiplier -- this spike's (#809) second
// corpus design, chosen to complement `gcd.v` (sequential, register-to-
// register paths) with a purely combinational, register-free design
// (input-port-to-output-port paths only), the same pairing
// `docs/design/synthesize-qor-improvements-survey.md` section 1.3 used for
// its own A/B measurement ("Design B -- mult8").
//
// No handshake, no clock: `p` is a pure combinational function of `a`/`b`.
module mult8 (
    input  wire [7:0]  a,
    input  wire [7:0]  b,
    output wire [15:0] p
);
    assign p = a * b;
endmodule
