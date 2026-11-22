- **`klt sim`: sub-milli `corners.supply_v` values no longer collapse into
  one corner** (#5870, 2026-11-17): a supply rail value finer than three
  decimals used to be spelled into the `corner_id` with `.3f`, so any two
  grid points differing only below 1e-3 resolved to one `corner_id`, shared
  one artifact directory and one `--resume` key, and one simulation was
  silently mis-attributed to every colliding point. The supply label is now
  lossless — `.3f` when that spelling round-trips (every existing label,
  and every example in `docs/`, is byte-identical), the shortest round-trip
  decimal otherwise, the rule `corners.params` set in #3035. The crossed
  expansion now also refuses two points that resolve to one `corner_id`
  (mirroring `corners.units`; a `corners.variants` override collapsing two
  rungs to one ID is refused the same way, and `klt corner-matrix` reports
  such a request as invalid instead of diffing it), and on `remote`/`batch`
  a grid containing such a value must be answered by a runner that echoes
  the dispatched IDs — a legacy-`.3f` runner response fails with error code
  `supply_precision_unsupported_worker` instead of returning duplicated
  measurement sets.
