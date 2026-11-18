- **`klt gen` draws the sky130 low-Vt marker** (#5607, 2026-11-17).
  `mos_array` and `diff_pair` draw `lvtn.drawing` (125/44) alone over the
  device footprint when `params.voltage_flavor="lvt"` on `sky130`, for both
  `flavor="nfet"` and `"pfet"`. `klt extract --deck sky130` classifies the
  resulting devices as `sky130_fd_pr__nfet_01v8_lvt` and
  `sky130_fd_pr__pfet_01v8_lvt`, respectively. Previously this value resolved
  to no layer and produced a `drc_hints.notes` entry. No JSON fields changed.
