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

> **Deliberate deviation from #4044:** the issue asks for `result` as
> "byte-identical embedded JSON". The wrapper embeds the report as a parsed
> object, so `result` is *equal when parsed*, not byte-for-byte identical to the
> report file (key order and whitespace may differ). No value is altered.

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

## `klt evidence lint`

The sweep half (issue #4043, survey gap G1): audits every record under
`<root>/evidence/sim/` so chip repos no longer fork their own
`evidence_lint.py`. Toolchain-free and CI-runnable.

```
klt evidence lint [ROOT] [--base REF] [--schema FILE.json]
    [--no-append-only] [--format text|json]
```

| Check | Finding codes |
|---|---|
| Shape (`validate_record_shape`), filename is a record id | `record_unreadable`, `record_shape`, `record_id_mismatch` |
| Append-only: `git diff --name-status REF...HEAD` — any status but `A` under the evidence dir (`HEAD` pointer files exempt). `--base` defaults to `origin/main` | `append_only_violation` |
| Supersession: targets exist, share `<block>/<scope>`, no forks, no cycles, a second record for a scope must supersede; `HEAD` file names an unsuperseded record | `supersedes_missing_target`, `supersedes_scope_mismatch`, `supersession_fork`, `supersession_cycle`, `supersedes_omitted`, `head_mismatch` |
| Sidecar: files named by `artifacts.corners[]` exist; an optional `deck_sha256` / `log_sha256` must match | `sidecar_missing`, `sidecar_hash_mismatch` |
| Verdict: `result.status` is re-derived from the embedded corners with `klt sim`'s own grading rule (`sim._grade_corners`); pex records are not re-derived | `verdict_mismatch` |
| Repo schema (`--schema`) | `schema_violation` |

`--schema FILE.json` is flat and declarative (no code hooks):

```json
{"required_fields": {"rate": "string"},
 "extension_keys": ["rate", "lane"],
 "corner_id_pattern": "^(tt|ss|ff)_\\d+v\\d+_\\d+C$"}
```

`required_fields` names extra top-level wrapper fields and their JSON type;
`extension_keys` restricts the keys of `extensions`; `corner_id_pattern` is
searched against every `result.corners[].corner_id`. Without `--schema` only
the built-in checks run.

Output (`--format json`): `{schema_version: 1, status: "clean"|"findings",
records_checked, base, findings: [{code, record, message}]}`. Exit `0` clean,
`1` findings, or an error envelope (`not_a_git_repo`, `bad_base`,
`bad_schema`), `2` usage. A non-git `--root` needs `--no-append-only`.

**Not covered (follow-up):** `mismatch_lint`-style Monte Carlo evidence
directories keyed by their own SHA-256 manifest are covered only to the extent
they are `evidence-record/1` sidecars (the `*_sha256` check above); a dedicated
manifest check is a follow-up to be filed at `loom:triage`.
