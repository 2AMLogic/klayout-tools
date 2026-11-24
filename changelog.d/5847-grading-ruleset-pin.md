- **Added** (#5847): `klt signoff --manifest/--fleet` renders — and the same
  modes' `--check` — accept `--require-grading-ruleset sha256:<hex>`: the
  command refuses (exit 1, the error envelope on stderr, zero bytes on
  stdout, no evidence command run) unless the running build's
  `grading_ruleset_id` equals the given id, checked before the manifest is
  read. On a match the output is byte-identical to the same run without the
  flag. Print the id with `--describe-grader` or
  `klt version --format json`; refused with `--attest`,
  `--describe-grader` and plain envelope aggregation, or for a value that is
  not `sha256:` + 64 hex digits. No `schema_version` bump.
