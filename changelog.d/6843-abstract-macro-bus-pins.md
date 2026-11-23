- **`klt extract --abstract-cells` no longer duplicates a hard macro's bus
  pins when the LEF and the GDS labels spell the bit index differently**
  (#6843, 2026-11-21): a macro's LEF declares an indexed pin `DATA[0]`
  while its own GDS label text spells the same bit `DATA<0>`, and the LEF
  merge (`_merge_lef_pins`) compared names literally — so the LEF spelling
  was appended as a *second* pin whose PORT rectangle then wired wherever it
  happened to land. On the SG13CMOS5L protocol emulator's SRAM macro
  (`RM_IHPSG13_1P_256x16_c2_bm_bist`) that duplicated all 96 indexed pins
  (205 emitted pins vs the macro's 109) and tied every copy to `VPWR`,
  taking as-routed LVS from 0 errors at `f979a965` to 227. The merge now
  compares through a per-bit alias key — bracket style (`<>` vs `[]`),
  leading zeros, and stacked dimensions (`A[1][0]` ≡ `A<1><0>`) normalize,
  distinct indices stay distinct — so the label's own pin name and access
  points win and the LEF spelling is never appended. Distinct indices and
  genuinely LEF-only ports (including #3749's substrate pins) merge exactly
  as before.
