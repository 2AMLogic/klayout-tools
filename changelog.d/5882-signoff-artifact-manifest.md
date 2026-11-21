- **`klt signoff` accepts repeatable `--artifact-manifest PATH` with `--manifest` or `--fleet`** (#5882, 2026-11-19).
  If signoff cannot fetch or verify the bytes for a cited `artifact:<key>`, it
  grades the item `unmet` with reason `artifact_unavailable`. The report records
  the artifact URI in `detail.artifact` and the diagnostic in `detail.problem`.
  The new reason is additive within the existing `schema_version`. Grading is
  unchanged when both the flag and an `artifact:` citation are absent.
