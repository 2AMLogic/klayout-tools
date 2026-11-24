- **`klt sim --op-lint` refuses a malformed `measurements` through the error
  envelope** (#6590, 2026-11-18): the default `klt sim` mode already rejected a
  non-array `measurements` or a non-object entry (#6245), but `--op-lint` read
  the same request without that check. A numeric `measurements` (`5`, `1.5`,
  `true`) escaped as a `TypeError` traceback, and `null`, a string, `{}`,
  `false`, `0` or a non-object entry were silently dropped, so one document was
  accepted by one mode and refused by the other. `--op-lint` now shape-checks a
  supplied top-level `measurements` with the shared validator before it starts
  ngspice: exit `1`, empty stdout, and the shared error envelope on stderr
  naming `request.measurements` or `measurements[<i>]` and the Python type. An
  absent key, `[]` and valid measurement objects behave as before, and
  `analysis` stays optional. No JSON field of a successful report changed;
  step-scoped measurements keep their existing lenient scan (#6412).
