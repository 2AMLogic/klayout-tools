- **`klt signoff --attest --check` names a cross-release `provenance.input`
  shape mismatch instead of leaving it inside anonymous drift** (#5829,
  2026-11-17): item 1 moved from a single `provenance.input.path` binding
  (0.8.0) to the `paths` input-set binding (0.9.x, #4764) under an unchanged
  envelope `schema_version`, so `--attest 1 --check` across the two releases
  reported a `drifted` a consumer could not tell apart from real evidence
  drift. When the committed and freshly re-attested envelopes bind
  `provenance.input` with different key sets, the response now carries an
  additive top-level `shape_mismatch` object (`field`, `keys_committed`,
  `keys_fresh`, `note`) naming both key sets and stating the remediation:
  regenerate the envelope (re-run `--attest` with `-o` under the current klt)
  and re-commit it — there is no converter, because recomputing the combined
  set digest needs the input files. The field is absent when both sides bind
  the same keys (content-only drift, items 9/10), `status` stays `drifted`,
  exit stays `3`, and the `drift[]` list is unchanged; no `schema_version`
  bump. `--format text` prints one `shape_mismatch:` line when present.
  Documented in [`docs/cli/signoff.md`](docs/cli/signoff.md)'s
  "`--attest --check`" section.
