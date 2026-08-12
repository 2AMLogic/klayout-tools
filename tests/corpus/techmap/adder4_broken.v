// The seeded-mismatch source for issue #875's technology-mapping
// acceptance gate: the same 4-bit ripple-carry adder as `adder4.v`, with
// the carry-in dropped from the sum -- a real "forgot the carry-in" bug
// class, not an arbitrary corruption. Same mutation
// tests/test_synthesize_equiv_gate.py (issue #808) uses for the Phase 1
// gate's own seeded mismatch.
//
// Its *mapped* netlist (`adder4_broken_mapped.v`, produced by the real
// `native/techmap` mapper from this design's own generic netlist) is a
// structurally valid, real mapper output that is functionally wrong
// relative to `adder4_generic.json` -- exactly what the gate must catch.
// Hand-editing gate-level Verilog was deliberately avoided: it risks
// producing something Yosys refuses to parse instead of a real logical
// divergence.
module adder4 (
    input  wire [3:0] a,
    input  wire [3:0] b,
    input  wire       cin,
    output wire [3:0] sum,
    output wire       cout
);
  assign {cout, sum} = a + b;
endmodule
