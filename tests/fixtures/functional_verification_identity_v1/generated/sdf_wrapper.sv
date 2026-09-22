// Generated SDF wrapper shim standing in for a KLT-created build input
// (issue #2097: root name "generated" is reserved for KLT-created inputs).
//
// This committed variant annotates via a *relative* SDF path so the golden
// manifest digest stays machine-independent; the relocation tests build
// their own shims embedding absolute paths, whose raw-byte identity is
// expected (and disclosed) to change on relocation.
`timescale 1ns/1ps
module sdf_wrapper_tb_counter;
    initial begin
        $sdf_annotate("tb_counter_typ.sdf", dut);
    end
endmodule
