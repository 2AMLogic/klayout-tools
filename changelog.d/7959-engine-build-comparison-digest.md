- **`klt sim --backend batch`: `engine_build_comparison` also says whether the worker ran the client's binary** (#7959).
  The comparison's `status` is the `--version` banner result, so a worker
  built differently from the submitting client reported
  `{"status": "match"}` while `environment.engine_build.binary_sha256` and
  `environment.client_engine_build.binary_sha256` disagreed, and a consumer
  binding a result to the executing binary had to diff the two hashes
  itself. `environment.remote.worker_environment.engine_build_comparison`
  now also carries `digest` (`"match"` / `"mismatch"`) whenever both digests
  are valid: `{"status": "match", "digest": "mismatch"}` is the same ngspice
  version built differently. `digest` is independent of `status` (a missing
  banner still yields `not_comparable` beside it) and is omitted when either
  side has no valid hash — an older worker capture, a hash that failed — so
  those reports are unchanged. Each `fleet[]` member compares its own
  worker's digest. Additive only: no `schema_version` bump, and the
  `batch_convergence_all_failed` hint still quotes `status`.
