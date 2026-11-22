- **Layout plans select resistor-array material from the netlist class** (#6919, 2026-11-21).
  A sky130 `RES_XHIGH_PO` group now draws the `xhigh` resistor body when the plan
  omits both `flavor` and `metal_level`. The report records the inferred material
  and warns when devices grouped together call for different materials.
