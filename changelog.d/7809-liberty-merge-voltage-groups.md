- **A Liberty list-merge now unions optional `input_voltage` / `output_voltage`
  groups that only some cell-group files declare** (#7809, 2026-11-24). A
  corner that lists a release's split cell-group files no longer refuses over
  `input_voltage(default_VDD_VSS_input)` / `output_voltage(default_VDD_VSS_output)`
  being present in some files and missing in another (the ASAP7 RVT `ff`
  corner's SIMPLE file), so `klt sta` can run it. Files that each declare a
  group still must agree on its body and name, and a file without the group
  whose cells reference its name still refuses. See `docs/cli/pdk.md`.
