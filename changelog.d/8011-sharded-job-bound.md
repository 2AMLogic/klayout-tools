- **`klt sim` sizes a sharded batch request's whole-job bound by its largest
  shard** (#8011, 2026-11-27): the pre-submit ceiling assessment treated
  every request as one job (`options.timeout_s x units + 120`), so a request
  whose `remote.hosts` already shards it into jobs that each fit was still
  warned about -- and under `batch.job_ceiling_check: "enforce"` refused --
  for the unsharded bound. Three corners at `options.timeout_s: 1800` with
  `remote.hosts: 3` are three 1,920 s jobs, yet `--dry-run` and the
  `--batch-set` preflight reported a 5,520 s bound against the 3,600 s
  ceiling. `--dry-run`, the `--batch-set` preflight and `--submit-only` now
  assess the largest shard, `ceil(units / remote.hosts)` units, by the
  resolved host count (`--hosts` included): that request draws no warning and
  passes an `"enforce"` preflight, while `remote.hosts: 2` reports the real
  two-unit bound (3,720 s) with the three-host recommendation. The
  `job_ceiling` assessment adds `job_unit_count` (the units behind
  `job_bound_s`; `unit_count` stays the request's total) and, for a sharded
  request, `split` / `effective_hosts` with the number of jobs it runs as.
  `remote.hosts: 1` requests, a stated `batch.job_timeout_s`, the opt-in
  auto-split and the blocking submit are unchanged.
