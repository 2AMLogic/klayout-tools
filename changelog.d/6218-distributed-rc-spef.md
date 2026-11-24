- **Fixed** (#6218, `klt extract --distributed-rc --spef`): SPEF export now
  writes the reported ladder's segment resistances and per-node ground
  capacitances instead of raising `KeyError: 'resistance_ohm'` on a
  distributed terminal. Lumped SPEF output and the JSON report shape are
  unchanged; no `schema_version` bump.
