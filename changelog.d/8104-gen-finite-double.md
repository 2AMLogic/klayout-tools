- **Fixed** (#8104, `klt gen`; no JSON field changes): a `double` generator
  parameter (`params.<name>`) too large for a float (e.g. `10**400`) raised an
  uncaught `OverflowError` instead of the contract's `GenError` envelope, and
  `NaN`/`Infinity` were accepted and handed to the generator as-is. Both now
  fail with the existing `generator '<name>': params.<name> must be a number`
  error and exit code 1. `klt gen --pdk-pcell` shares the check and gets the
  same fix.
