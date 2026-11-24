- **`klt sta` / `klt place-and-route`: surface the OpenSTA Liberty warnings
  that discard timing models (#7411, 2026-11-23)**: a Liberty whose timing
  table names an undefined `lu_table_template` makes OpenSTA warn
  (`STA-1253`) and drop the table, leaving the timing group model-less
  (`STA-1311`) and discarding it too — so the arc has no delay model and the
  reported slacks silently omit the paths it would have launched (for a
  sequential cell's CLK-to-Q group, typically every register-launched path),
  while the response still reads `status: "ok"` / `timing_status:
  "constrained"`. `klt sta` now carries an additive, always-present
  `liberty_warnings` block (`{count, by_code, sample, sample_limit,
  truncated}`) on every response and every `corners[]` entry, attributed to
  the corner whose own session emitted it; the verbatim sample quotes the
  deck by its report identity (#2879's rule), never an absolute host path.
  `klt place-and-route` appends one `warnings[]` entry naming the per-code
  counts when any stage's Liberty read discarded a model — deduplicated
  across stages (every stage script re-reads the same deck; a `--resume`d
  stage's lines are recovered from its retained `openroad-logs`), and
  byte-identical `warnings` on a clean deck. Only `STA-1253`/`STA-1311`
  count; both streams are scanned (OpenROAD builds differ in which the
  logger writes to). A warning, never a refusal: `status`, `timing_status`,
  exit codes and `signoff` grading are unchanged; no `schema_version` bump.
  See `docs/cli/sta.md` "Liberty model warnings" and
  `docs/cli/place-and-route.md` "Measured power delivery".
