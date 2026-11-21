- **`klt drc --check --deck-file` on a curated report is now an error** (#6070,
  2026-11-17): `--check` silently ignored `--deck-file` for a single-engine
  curated report while `--check --rerun` rejected it, so the same input had two
  contracts. `--check` now exits `1` with the standard error envelope
  (`--deck-file cannot be combined with --check on a curated report`), matching
  `--rerun`. A `klayout`-engine report and an `--engine both` report (the
  override still reaches the native side only) behave as before. No JSON field
  changed.
