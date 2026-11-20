- **`klt functional-verification` uses the request's base directory to select the
  repo for its `{path, scope}` envelopes** (#6762, 2026-11-19).
  `environment.sources[]`, `environment.{results_xml,test_log,build_log}`,
  per-seed `environment.initial_state.runs[].results_xml` and mutation
  `mutation_testing.results[].log_path` previously used the repo containing
  the invoking directory. They now use the repo containing the request file's
  directory by default. For stdin and inline JSON without `--base-dir`, the
  invoking directory is the base for relative request paths, so envelopes can
  differ between working directories. A fixed absolute `--base-dir` pins the
  classification repo for every request form. A relative `-o/--outdir` still
  follows the invoking directory, so output paths and scopes can change even
  with a fixed base. Present paths outside the selected repo have scope
  `external`, as do all present paths if the base lies outside every repo.
  The schema version, fields and digest calculation are unchanged.
  `klt em` and the OpenROAD engine logs keep their cwd anchor.
