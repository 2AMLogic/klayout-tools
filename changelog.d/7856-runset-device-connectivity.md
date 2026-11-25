- `klt lvs` with `engine: "klayout-runset"` reports `device.connectivity`
  for a mismatched device pair with identical primary parameters. For example,
  shorting a MOS gate to its drain now identifies a terminal connectivity
  error with both sets of dimensions in the report. (#7856)
