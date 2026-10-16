// operator_probe.v -- H01 synthesizable operator probe (issue #20).
//
// ONE Mark I operator datapath, RTL-exact to the frozen N02 integer model
// (src/gf180_dx7/model/operator.py::mki_sin, contract gf180-dx7-integer-v1,
// spec/numeric-profile-v1.json operator_core; docs/N02-OPERATOR-MODEL.md
// section 2). The pinned GPL engine file (EngineMkI.cpp) is the semantic
// specification only; no bytes are copied (NUM-001). The ROMs below are
// $readmemh-loaded from reference/tables/*.hex -- the SAME bytes the Python
// model pins by SHA-256 in reference/tables/manifest.json (sinlog
// 990c19e9..., sinexp e515a71a...; generator tools/gen_tables.py --check).
//
// Per-evaluation arithmetic (N02 model, one eval = one mki_sin call):
//     phi       = phase[27:12]                          (uint16 truncation)
//     expVal    = sinLog(phi) + env (mod 2^16)          (log-domain level add)
//     isSigned  = expVal[15]; expVal &= 0x7FFF
//     result    = 4096 + sinExpTable[(expVal & 0x3FF) ^ 0x3FF]
//     result  >>= expVal >> 10
//     out       = isSigned ? (-result - 1) << 13 : result << 13   (int32)
// followed by the NUM-010 output conversion:
//     val       = out >>> 4
//     clip_val  = val < -2^24 ? +32768 (pinned quirk, 17-bit two's comp)
//               : val >= 2^24 ? +32767 : val >>> 9
// and the 32-bit wrapping phase advance phase <= phase + freq (NUM-003).
//
// STRUCTURE (the ladder_dp_n.v family pattern, single-state-set case): one
// datapath, all math in one sequential core, one evaluation accepted per
// clock (throughput 1 eval/clk, latency 3), state explicitly registered:
//     stage 1  phi fold + sinlog ROM read, env capture        (s1_*)
//     stage 2  log-domain add + sign + sinexp ROM read, shift (s2_*)
//     stage 3  4096-bias, >>shift, sign reconstruct, >>4/clip (out_*)
// Handshake: en=1 accepts one evaluation; its result appears with
// out_valid exactly 3 cycles later, in acceptance order. busy = en |
// in-flight stages; wait for !busy before ld_phase. ld_phase (used while
// en=0) loads the phase register from phase_in with priority over the
// advance, mirroring the harness's directed corner loads.
//
// OBSERVABILITY IS LOAD-BEARING: every state bit feeds the output ports or
// the registered XOR reduction state_obs, so synthesis cannot remove state.
// Negative controls (issue #20):
//   -DMUTATE_ADDRWIDTH        sinlog table address truncated to 9 bits; the
//                             equivalence harness MUST fail (resolution).
//   -DH01_STRIP_OBSERVABILITY all outputs tied off; yosys deletes every
//                             state flop and the flop-count gate MUST FAIL
//                             (the "outputs optimized away" control).
//
// This file alone claims no P&R, no timing closure, no fit, and no
// original-DX7 fidelity: bit-exactness to the frozen model is the bar, and
// docs/H01-OPERATOR-PROBE.md records exactly what was measured.
`default_nettype none

`ifndef H01_SINLOG_HEX
`define H01_SINLOG_HEX "reference/tables/sinlog_table.hex"
`endif
`ifndef H01_SINEXP_HEX
`define H01_SINEXP_HEX "reference/tables/sinexp_table.hex"
`endif

module operator_probe (
    input  wire        clk,
    input  wire        rst,         // synchronous, active high
    // eval stream: en=1 for one cycle accepts one operator evaluation
    input  wire        en,
    input  wire [31:0] freq,        // per-sample phase increment (NUM-003)
    input  wire [15:0] env,         // 14-bit frame gain, uint16 domain (NUM-004)
    // phase register load (use while en=0; priority over the advance)
    input  wire        ld_phase,
    input  wire [31:0] phase_in,
    // result (for the eval accepted 3 edges earlier)
    output wire [31:0] out_raw,     // mkiSin int32 result
    output wire [16:0] clip_val,    // NUM-010 clip domain, 17-bit two's comp
    output wire        out_valid,
    output wire        busy,        // eval accepted or in flight
    // registered XOR reduction of every state bit (observability)
    output wire        state_obs
);

    // ------------------------------------------------------------------
    // State
    // ------------------------------------------------------------------
    reg [31:0] phase_q;   // 32-bit wrapping phase accumulator (NUM-003)
    // stage 1: quarter-wave fold result (sinlog ROM output, sign included)
    reg [15:0] s1_log;
    reg [15:0] s1_env;
    reg        s1_valid;
    // stage 2: sinexp ROM output + shift amount + sign flag
    reg [15:0] s2_exp;
    reg [4:0]  s2_shift;  // expVal[14:10] after sign clear: 0..31
    reg        s2_sign;
    reg        s2_valid;
    // stage 3: output registers
    reg [31:0] out_q;
    reg [16:0] clip_q;
    reg        out_valid_q;
    // observability reduction register
    reg        obs_q;

    // ------------------------------------------------------------------
    // ROMs: the pinned N02 table bytes (manifest-hashed), 1024 x 16 each
    // ------------------------------------------------------------------
    reg [15:0] sinlog_rom [0:1023];
    reg [15:0] sinexp_rom [0:1023];
    initial begin
        $readmemh(`H01_SINLOG_HEX, sinlog_rom);
        $readmemh(`H01_SINEXP_HEX, sinexp_rom);
    end

    // ------------------------------------------------------------------
    // Stage 1 combinational: phi fold + sinlog ROM read
    // phi = phase[27:12]; mirror index when phi[10]; sign when phi[11]
    // (EngineMkI.cpp:64-78 semantics mirrored in the N02 model)
    // ------------------------------------------------------------------
    wire [15:0] phi = phase_q[27:12];
`ifdef MUTATE_ADDRWIDTH
    // NEGATIVE CONTROL: table address width mutated 10 -> 9 bits. Entries
    // alias across quadrants; the equivalence harness must fail on this
    // build (tools/h01_compare.py --mutate). Never synthesize this build
    // into any area claim.
    wire [9:0] sl_addr = {1'b0, phi[8:0] ^ {9{phi[10]}}};
`else
    wire [9:0] sl_addr = phi[9:0] ^ {10{phi[10]}};
`endif
    wire [15:0] sl_data = sinlog_rom[sl_addr];
    wire [15:0] sl_signed = sl_data | (phi[11] ? 16'h8000 : 16'h0000);

    // ------------------------------------------------------------------
    // Stage 2 combinational: log-domain level add + exp ROM read
    // expVal = (sinLog + env) mod 2^16; sign bit split; exp index =
    // (expVal & 0x3FF) ^ 0x3FF; shift = expVal >> 10
    // ------------------------------------------------------------------
    wire [15:0] exp_val16 = s1_log + s1_env;
    wire        ex_sign   = exp_val16[15];
    wire [14:0] exp_val15 = exp_val16[14:0];
    wire [9:0]  ex_addr   = ~exp_val15[9:0];   // ^ 0x3FF
    wire [4:0]  ex_shift  = exp_val15[14:10];
    wire [15:0] ex_data   = sinexp_rom[ex_addr];

    // ------------------------------------------------------------------
    // Stage 3 combinational: bias, shift, sign reconstruct, NUM-010 clip
    // result = 4096 + expOut (max 4096 + 4090 < 2^13); out = signed ?
    // (~result << 13) : (result << 13)  -- (-result - 1) == ~result mod
    // 2^32, so the negative side is the same shift of the complement.
    // ------------------------------------------------------------------
    wire [12:0] result13 = 13'd4096 + {1'b0, s2_exp};
    wire [12:0] result_sh = result13 >> s2_shift;
    wire [31:0] res32    = {19'b0, result_sh};
    wire [31:0] out_pos  = res32 << 13;
    wire [31:0] out_neg  = (~res32) << 13;
    wire [31:0] op_out   = s2_sign ? out_neg : out_pos;
    // NUM-010: val = out >>> 4; clip at +/- 2^24; else val >>> 9. The
    // negative-overflow quirk maps to +32768 (17-bit two's comp 17'h08000).
    wire signed [31:0] val32 = $signed(op_out) >>> 4;
    wire        clip_lo = $signed(val32) < -(1 << 24);
    wire        clip_hi = $signed(val32) >= (1 << 24);
    wire [16:0] clip_norm = val32 >>> 9;  // fits 17 bits for |val| < 2^24

    // ------------------------------------------------------------------
    // Sequential logic
    // ------------------------------------------------------------------
    always @(posedge clk) begin
        if (rst) begin
            phase_q    <= 32'h0000_0000;
            s1_valid   <= 1'b0;
            s2_valid   <= 1'b0;
            out_valid_q <= 1'b0;
        end else begin
            // phase register: load has priority; else advance when accepted
            if (ld_phase)
                phase_q <= phase_in;
            else if (en)
                phase_q <= phase_q + freq;   // natural 2^32 wrap

            // stage 1 -> 2
            s1_log   <= sl_signed;
            s1_env   <= env;
            s1_valid <= en;
            // stage 2 -> 3
            s2_exp   <= ex_data;
            s2_shift <= ex_shift;
            s2_sign  <= ex_sign;
            s2_valid <= s1_valid;
            // stage 3 -> outputs
            out_q      <= op_out;
            clip_q     <= clip_lo ? 17'd32768
                        : clip_hi ? 17'd32767
                        : clip_norm;
            out_valid_q <= s2_valid;
        end
    end

`ifdef H01_STRIP_OBSERVABILITY
    // NEGATIVE CONTROL: no output observable, no reduction -- synthesis
    // must (and does) delete every state flop; the flop-count gate then
    // FAILS. This build exists only to prove the gate has resolution.
    assign out_raw   = 32'h0;
    assign clip_val  = 17'h0;
    assign out_valid = 1'b0;
    assign busy      = 1'b0;
    assign state_obs = 1'b0;
`else
    assign out_raw   = out_q;
    assign clip_val  = clip_q;
    assign out_valid = out_valid_q;
    assign busy      = en | s1_valid | s2_valid | out_valid_q;

    wire obs_next = (^phase_q) ^ (^s1_log) ^ (^s1_env)
                  ^ (^s2_exp) ^ (^{s2_shift, s2_sign})
                  ^ (^{out_q, clip_q})
                  ^ s1_valid ^ s2_valid ^ out_valid_q;
    always @(posedge clk) begin
        if (rst)
            obs_q <= 1'b0;
        else
            obs_q <= obs_next;
    end
    assign state_obs = obs_q;
`endif

endmodule

`default_nettype wire
