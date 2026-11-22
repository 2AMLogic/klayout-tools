- `klt extract --abstract-cells --abstract-cell-lef` warns when the extracted
  substrate global connects only to LEF-merged substrate pins. The warning
  identifies the net and counts affected instances and cell types. A verified
  physical tie can be disclosed with `hints.layout_short_nets` for LVS, or
  drawn and labelled for extraction (#5632).
