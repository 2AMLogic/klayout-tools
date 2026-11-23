- **`klt signoff` accepts a `structural` disposition on `spec_rows[]`
  rows** (#6985, 2026-11-22): a row whose claim is a design constant the
  bench itself wires in, or a structural property checked by inspecting a
  deck or layout, is not a measured quantity -- no producible envelope
  can ever carry a covering measurement, so such a row could only be
  asserted through some envelope's `rows` (a false claim) or declared a
  `gap`, and a gap reads identically whether the row is unevidenced or
  evidenced by a real structural check the grader cannot see. The new
  object form `{"id", "structural": {"claim", "artifact":
  {"file", "content_hash"}}}` makes the row representable and distinct:
  the artifact is freshness-pinned by content hash and checked at grading
  time (`structural_artifact_unreadable` / `structural_artifact_stale`
  when missing or changed), the row is listed under
  `detail.structural_rows` — never in `declared_gaps`,
  `justified_gaps`, `uncovered_rows` or `row_sources` — and rendered as
  a `structural` state in the `spec_rows[]` table with its claim and
  artifact hash. The hash pin proves the artifact is unchanged, not that
  the property holds. A citation naming a structural row is
  `invalid_evidence`, as is a malformed object or `structural` beside
  `gap`. The roll-up is deliberately unchanged: a declared structural
  row keeps the item `unmet` / `partial_coverage` exactly like a
  justified gap (whether it may ever satisfy item 5 is a separate
  operator-level ruling in the #5858 family). Item 6 rows share the
  reader and grading branch. Additive: no `schema_version` bump; every
  new report field is omitted when unused, so a manifest declaring no
  structural row renders byte-identically.
