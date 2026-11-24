- **Item 9 verifies the pinned klt version** (#6758, 2026-11-19).
  `klt signoff --attest 9` and `--check` compare a declared `pins.tools.klt`
  with the running build, including development suffixes. A stale pin exits
  3 and reports both versions. A running build with an unknown identity
  records the comparison as unverifiable. Re-attest older envelopes that
  declare the pin to record the new `tool_version` check.
