- **`klt sim --backend batch`: attest the models a per-section-corner-library
  request executed** (#5457, 2026-11-13): a request that selects its corners
  with `corners.process[].sections[].lib` (#2522) declares no `models.lib`, so
  #5321's `runner_models_lib_sha256` / `models_lib_comparison` pair was `null`
  and `not_comparable` by construction — the one request shape the docs
  recommend for off-host portability could never attest what it ran against.
  `environment.remote` (and each `fleet[]` member) now also carries
  `runner_corner_section_libs` (`[{name, sha256}]` lifted from the runner's own
  `environment.corner_section_libs`, `null` when unreported) and, when either
  side reported per-section libraries, `corner_section_libs_comparison`
  (`match` / `mismatch` with the unmatched `names` / `not_comparable` with a
  `reason`), compared by `(name, sha256)` pair membership so a shared basename
  or a `--batch-set` shard subset is not a mismatch. `models_lib_comparison`
  gains the `no_models_lib` reason, which previously read as the fixable-by-
  updating-the-image `runner_hash_unavailable`. Text output prints a
  `corner_section_libs:` line next to `models_lib:`. Missing data is never a
  proven mismatch and klt never fails a run on the verdict. Additive; no
  `schema_version` bump. See `docs/cli/sim.md` "Worker-observed environment".
