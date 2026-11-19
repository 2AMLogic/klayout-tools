- **`klt functional-verification`'s `{path, scope}` envelopes no longer vary
  with the directory `klt` is invoked from** (#6762, 2026-11-19):
  `environment.sources[]`, `environment.{results_xml,test_log,build_log}`, the
  per-seed `environment.initial_state.runs[].results_xml` and mutation
  `mutation_testing.results[].log_path` resolved their `scope` against the
  repo containing the *process cwd*, so one request over the same inputs
  produced a different committed blob per invoking directory (`{"path": null,
  "scope": "external"}` instead of `{"path": "rtl/gcd.v", "scope": "repo"}`).
  They now resolve against the repo containing the *request* — its own
  directory, or `--base-dir` — the same anchor `docs/cli/functional-verification.md`
  already documents for every relative path in the request. **Reports can
  change** for a run whose cwd sat in a different repo than its request
  (`external` ↔ `repo`); a request outside every repo reports `external`
  throughout, as does a `-o/--outdir` outside the request's repo. `sha256`
  values are unchanged. No field is added or removed and no `schema_version`
  bumps. `klt em` and the OpenROAD engine logs keep their cwd anchor.
