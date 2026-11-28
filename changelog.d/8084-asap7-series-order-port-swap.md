- **`klt lvs` names the ASAP7 RVT port-swap limitation in-band** (issue #8084):
  11 more RVT masters (`AND4x1`, `AO21x1`, `AO21x2`, `AOI32xp33`, `FAx1`,
  `OAI31xp33`, `OAI31xp67`, `OAI322xp33`, `OAI32xp33`, `OAI33xp33`,
  `XOR2x2`; suffix `_ASAP7_75t_R`) join `KNOWN_UNSUPPORTED_ASAP7_CELLS` as
  `"series-order-port-swap"`, bringing it to 28. They match their CDL
  standalone only because the comparer swaps two commutative ports, and
  mismatch once the pins are anchored or a block wires them to asymmetric
  nets; a mismatching FinFET compare involving one now adds the
  `topology.known_limitation` warning for it, still without changing
  `status`.
