- **Added** (#5817, 2026-11-19): every `klt signoff --attest` command check that actually
  launched -- passing, failing and timed-out alike -- now carries an additive
  `duration_s` number (observed wall time in seconds, one decimal) in the
  live `--format json` result, and `--format text` names it on the check's
  line (`... (exit 0) in 99.6s`), so a check drifting toward its declared
  `timeout_s` budget is visible before the timeout fires. Live-only, exactly
  like `output_tail` (#4698): never written to an `--output` envelope, never
  in the `--check` diff, so the committed bytes and their content hash stay
  reproducible across runs; a command that never launched
  (`error: "launch_failed"`) and a skipped or carried `local_only` command
  omit the field. Additive, so no `schema_version` bump.
