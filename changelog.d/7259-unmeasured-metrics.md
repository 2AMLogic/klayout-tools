- **`klt signoff --attest 8` accepts `unmeasured_metrics`, a declared metric gap** (#7259, 2026-11-22).
  A digital inventory's `corners`/`metrics` block was all-or-nothing per
  pair: one `metric` check per corner x required metric, a missing binding a
  failed check -- so a characterization that measured some but not all of
  `area`/`fmax`/`power` had to choose between a red attestation that bound
  its measured values and a green metrics-free one that bound none. The
  inventory may now list `[{"corner", "metric", "reason"}]` for each required
  metric nobody measured. The pair's slot in `attestation.checks[]` holds a
  passing `unmeasured_metric` check (role `digital`, carrying `corner`,
  `metric` and `reason`, no `value`) in place of the failed `metric` check,
  so the attestation passes with the measured subset bound and every gap
  visible -- item 9's `unbenched_rows` is the precedent. Text output renders
  `[PASS] digital ss fmax: unmeasured (<reason>)`. A declaration excuses only
  the pair it names: the same metric missing at another corner, an invalid
  bound value, and citation or freshness failures still fail. Refused (exit
  `1`) as a malformed inventory: a non-list, an entry that is not exactly
  `{corner, metric, reason}`, an unknown corner or metric, a blank reason, a
  duplicate pair, a pair that `metrics` also binds, and the field on an
  inventory without `corners`/`metrics`. Inventories that omit the field
  produce byte-identical envelopes. Additive; no `schema_version` bump --
  earlier builds refuse `unmeasured_metrics` as an unknown field.
