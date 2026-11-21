- **Changed** (#5704, 2026-11-20, `klt gen res_array` on sky130A `flavor`
  `high`/`xhigh`; drawn-geometry change, no JSON field changes,
  `schema_version` unchanged): the contacted end heads are now drawn
  **2.16 µm** long along the current-flow axis instead of the
  enclosure-derived 0.39 µm, meeting open_pdks `sky130A.tech`'s
  precision-resistor contact-extension rule (`extend xpc/a xhrpoly,uhrpoly
  2160`, "poly contact extends poly resistor by < 2.16um", `licon.1c +
  li.5`) that the PDK's own Magic DRC reported once per head under both
  `drc(fast)` (its default style) and `drc(full)`; verified live
  (2026-11-17, Magic 8.3.683, open_pdks `c6d73a35f524`) that the reproducer
  cell reports `No errors found.` in both styles after the change. The
  2.16 µm value is the foundry's own (`sky130A.tcl`'s `res_to_endcont
  1.985` + `end_contact_size/2` 0.095 + `poly_surround` 0.08), resolved
  from a new per-(PDK, flavor) table with no new request parameter, so a
  unit's total length grows 2 × (2.16 − 0.39) = **3.54 µm** per
  `high`/`xhigh` unit and the unit pitch moves accordingly. The recognised
  body segment (`poly.res` span, so the extracted `L`/`R`), the 0.19 ×
  2.0 µm licon slots and the 2.2 µm pad height are unchanged; every other
  family/flavour (sky130 `generic`, gf180mcu, sg13g2) and every
  `metal_level` request draws identical geometry to before.
