- **`klt functional-verification` accepts OpenSTA SDF paths with no delay operands** (#7481).
  The Icarus annotation copy omits entries such as `(IOPATH CLK Q)`.
  The SDF receipt reports partial annotation and records their count and
  affected cell arcs under `iopath_empty_delay`.
