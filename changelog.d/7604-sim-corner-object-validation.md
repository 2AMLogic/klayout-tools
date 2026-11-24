- **`klt sim` reports a field-specific JSON error when a corner request supplies a
  non-object `options` or `monte_carlo`** (#7604, 2026-11-24). A non-null
  malformed value now raises `SimError` naming the field and its received type
  before measurement templates expand, CLI overrides apply or a backend is
  prepared, so the CLI exits 1 with the shared error envelope instead of
  leaking an uncaught `AttributeError`. Null and omitted values keep their
  existing behavior, and a valid Monte Carlo object still receives the existing
  sample-count checks.
