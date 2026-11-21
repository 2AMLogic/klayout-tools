- **`klt sta --check REPORT` / `--rerun`** (#5841, 2026-11-17): a committed
  `klt sta --format json` report can now be re-verified against its request, as
  `klt place-and-route` and `klt synthesize` already allowed (#2224). Cheap mode
  re-hashes the geometry source and liberty(ies) against the report's
  `provenance` digests (naming the moved corner on a multi-corner report);
  `--rerun` re-runs the analysis and diffs verdict fields, excluding version
  echoes, `engine_log` and the non-reproducible `sdf.content_hash`. Exit `0`
  match, `3` drifted. Additive: the `sta` report and its `schema_version` are
  unchanged.
