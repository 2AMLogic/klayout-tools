* row_mix_gf180 -- reference netlist for the generic mixed-device row example
* (issue #3969). Authored from docs/design/custom-transistor-layout-study.md
* section 4.1; hand-written, independent of generate.py and of klt extract.
*
* inv1: A -> n1 | transmission gate n1 -> n2 (S/SB) | NOR3(n2, B, C) -> ny
* | inv2: ny -> Y | one off dummy gate per row (gate tied to its own supply).
.SUBCKT row_mix_gf180 A B C S SB VDD VSS Y
* inverter 1
MN1 n1 A VSS VSS nfet L=0.28U W=0.8U
MP1 n1 A VDD VDD pfet L=0.28U W=1.6U
* transmission gate
MN2 n2 S n1 VSS nfet L=0.28U W=0.8U
MP2 n2 SB n1 VDD pfet L=0.28U W=1.6U
* NOR3
MN3a ny n2 VSS VSS nfet L=0.28U W=0.8U
MN3b ny B VSS VSS nfet L=0.28U W=0.8U
MN3c ny C VSS VSS nfet L=0.28U W=0.8U
MP3a p1 n2 VDD VDD pfet L=0.28U W=1.6U
MP3b p2 B p1 VDD pfet L=0.28U W=1.6U
MP3c ny C p2 VDD pfet L=0.28U W=1.6U
* inverter 2
MN4 Y ny VSS VSS nfet L=0.28U W=0.8U
MP4 Y ny VDD VDD pfet L=0.28U W=1.6U
* dummy gates (off)
MN5 Y VSS VSS VSS nfet L=0.28U W=0.8U
MP5 Y VDD VDD VDD pfet L=0.28U W=1.6U
.ENDS row_mix_gf180
