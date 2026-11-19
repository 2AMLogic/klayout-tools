- **`klt signoff --manifest` renders a per-spec-row table** (#5691,
  2026-11-17): a block manifest citing many `klt sim`/`klt yield` envelopes
  against a declared row set had no verb that produced the one artifact a
  pre-silicon reader wants — each spec row's limit, worst value, binding
  corner, verdict, yield estimate and the cited file behind each number — so
  every consuming repo wrote its own aggregator and re-derived the same joins
  (binding instance by *relative* margin, row attribution, cross-campaign
  reduce). When the manifest declares a top-level `spec_rows` map, the tier
  report now gains one additive top-level `spec_rows[]` block: one record per
  declared row, in declaration order, with `state` (`graded`/`gap`/
  `no_evidence`/`unattributable`), `worst` and `yield` quoted byte-equal off
  the cited envelopes, the binding measurement picked by minimum **relative**
  margin (`margin / |active bound|`; absolute-margin fallback with
  `relative_margin: null` when the bound is `0` or `limits` is absent;
  `error > inconclusive > fail > insufficient_evidence > pass` precedence),
  declared gaps carried as explicit no-evidence records, disagreement in
  limits listed under `limit_disagreement[]` instead of reconciled, and
  `sources[]` naming the file and measurement behind every number.
  `--format text` renders the same records as a table. A manifest that
  declares nothing omits the key entirely — no item verdict, `tier`,
  `row_coverage` or grading rule moves, and `--check` reports no drift on an
  upgrade alone. Additive: no `schema_version` bump.
