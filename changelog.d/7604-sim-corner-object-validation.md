`klt sim` reports a field-specific JSON error when a corner request supplies a
non-object value for `options` or `monte_carlo`. Null and omitted values keep
their existing behavior.
