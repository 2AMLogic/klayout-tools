// A 4-bit ripple-carry adder -- the gate fixture for issue #875's
// technology-mapping acceptance gate (`klt equiv` between a
// `klt.synth.generic-netlist/1` and the mapped netlist `native/techmap`
// produced from it).
//
// Purely combinational (no clock), so it is in scope for `klt equiv`'s
// Phase 0 MVP (a design containing flip-flops/latches/memories is rejected
// outright -- see docs/cli/equiv.md's "Scope" section). This is the same
// design tests/test_synthesize_equiv_gate.py (issue #808, the Phase 1
// RTL-vs-generic-netlist gate this stage's gate mirrors) already uses, so
// both gates are demonstrated on the same small, hand-checkable circuit.
module adder4 (
    input  wire [3:0] a,
    input  wire [3:0] b,
    input  wire       cin,
    output wire [3:0] sum,
    output wire       cout
);
  assign {cout, sum} = a + b + cin;
endmodule
