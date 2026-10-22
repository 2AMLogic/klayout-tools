# `klt evidence`

The writer side of the `evidence-record/1` convention defined in
[`../design/sim-evidence-discipline-spike.md`](../design/sim-evidence-discipline-spike.md)
(issue #4044). `klt sim` / `klt pex` stay stateless; this verb consumes a
**finished** report, so it works the same for `--backend batch` runs.

```
klt evidence record REPORT --request REQUEST.json --block B --scope S
    [--subset-reason R] [--supersedes PATH ...] [--root DIR]
    [--extensions FILE.json] [--input PATH ...] [--format text|json]
```

## What it writes

Under `<root>/evidence/sim/<block>/<scope>/`:

- `<id>.json` — the wrapper, where `<id>` is `<YYYYMMDD>-<HHMMSS>-<short-sha>`
  (UTC time; 7-char git short SHA of `--root`'s `HEAD`).
- `<id>.d/<corner>/corner.cir` and `log` — per-corner deck and engine log,
  copied from `corners[].artifacts.deck` / `.log`.

Wrapper fields: `schema`, `recorded_at`, `block`, `scope`, `request_path`
(repo-relative), `request_sha256`, `subset_reason`, `pdk_pin`
(`name`, `expected_deck_content_hash`, from `result.provenance`),
`extraction_pin` (pex reports only), `supersedes`, `git`
(`commit`, `dirty_inputs`, `inputs`), `environment`
(`env_provenance.environment_provenance()`), `artifacts`, `extensions`
(opaque `--extensions` object) and `result` — the report, unmodified.

## Guarantees

- **Never overwrites** — an existing record exits 1 with
  `error.code: "record_exists"`; there is no `--force`.
- **Git required** — a `--root` that is not a git work tree with a commit is
  `error.code: "not_a_git_repo"`. `dirty_inputs` is true when the request
  (or any `--input`) is modified, staged or untracked.
- **Honest snapshot** — if any corner lacks `artifacts.deck` (the report was
  run without `options.keep_artifacts`), `artifacts.netlist_snapshot` is
  `{"status": "unavailable", "reason": ...}`; the source netlist is never
  substituted.
- **No leaks** — wrapper metadata (everything but `result`) is scanned with
  the `klt env-provenance` rules; a hit exits 1 with
  `error.code: "provenance_leak"` and writes nothing.

## Output

`--format json`: `{schema_version: 1, status: "recorded", record_id,
record_path, git_commit, dirty_inputs, netlist_snapshot, supersedes}`.
Errors use the standard envelope (`docs/json-contract.md`); exit codes are
`0` written, `1` error, `2` usage.

The schema, id format and `validate_record_shape()` live in
`klayout_tools.evidence_record`, shared with the record linter (#4043).
