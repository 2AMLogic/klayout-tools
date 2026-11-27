- **`klt sta` `violating_endpoints` (#7418)**: the request gains an optional
  `constraints.max_violating_endpoints` positive-integer knob, and when it is
  set the response (single-corner and every `corners[]` entry) gains
  `violating_endpoints` — the bounded, worst-first list of violating
  endpoints `{check_type, engine_check_role, startpoint, endpoint, slack_ns,
  path_group}` per side that `setup_violation_count` / `hold_violation_count`
  only count, with `setup_truncated` / `hold_truncated` separating "all of
  them" from "first N". Rows are unique per endpoint per path group (the same
  population the counts measure), so untruncated the list length equals the
  count; a disagreement is reported as `status: "inconsistent"` with a
  reason rather than a silently short list, and a failed engine query as
  `status: "unavailable"` with empty lists. The emitter iterates
  `sta::path_group_names` querying cap+1 rows per group (live-verified
  against OpenROAD 26Q3-1510: `-slack_max` is inclusive at the boundary, so
  a strict slack < 0 filter keeps zero-slack endpoints out;
  `-group_path_count` caps per path group; `-path_group` is list-valued, so
  multi-word group names must be `[list]`-quoted or the filter silently
  disables — the capture fixture records all three). **Additive —
  `schema_version` unchanged:** an omitted knob emits no Tcl and the
  response is byte-identical.
