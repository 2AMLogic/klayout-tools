- **`klt signoff` grades a `sta` envelope's own `status`; opt-in
  `require_routed_geometry`** (issue #7711, 2026-11-24): item 5 graded a cited
  `klt sta` envelope `met` from per-corner timing alone, so an envelope
  declaring `status` other than `"ok"` (which `klt sta` never emits; only a
  hand-edited or foreign envelope can) still counted as passing. It now grades
  `unmet`/`check_failed` with `detail.sta_status`, in `--manifest` and
  aggregation mode. `geometry_source` is deliberately **not** part of the
  default predicate (item 5 is the pre-layout timing item, and `klt sta`
  characterises pre-route DEFs), so existing `met` verdicts on
  `placement_estimate`/`netlist_estimate` envelopes are unchanged; an evidence
  entry may now set `"require_routed_geometry": true` to grade `unmet`
  (`check_failed`, `detail.sta_geometry_not_routed`) unless it is literal
  `"routed"`. Off by default; a non-boolean value is `invalid_evidence`. Every
  `sta` citation additively carries `detail.geometry_source`. No
  `schema_version` change; `grading_ruleset_id` changes with the grading code.
  See `docs/cli/signoff.md`.
