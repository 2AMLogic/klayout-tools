- **Fixed** (#6742, 2026-11-21). `klt extract --abstract-cells` connects
  routes that reach different pads of the same abstracted pin. Its warnings
  identify joined nets and any conflicting layout or DEF names that kept
  route trees separate.
