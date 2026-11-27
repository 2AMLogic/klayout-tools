- **`klt gen mos_array --mos_cap` on gf180mcu now draws DRC-clean MOS
  capacitors (`DF.1c`)** (#6170, 2026-11-17). The `MOS_CAP_MK` marker covered
  the array's whole `voltage_flavor` margin box, which reached the 0.42 um
  guard-ring `COMP` and made every array report four ring-wide
  `comp.width.moscap.1` violations; it now covers only the unit devices (the
  device `COMP` itself was already >= 1 um at `w_um` >= 1.0). Because the
  channel is `w_um` wide, no S/D geometry can pass a 1 um width rule below
  that, so `mos_cap` now rejects `w_um` < 1.0 with a `GenError` naming
  `comp.width.moscap.1` (previously 0.22 um LV / 0.30 um MV were accepted and
  emitted a violating device). Non-`mos_cap` output is unchanged. No JSON field
  added or removed; `schema_version` unchanged.
