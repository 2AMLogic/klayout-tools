- **`via_pad_trim.tip_check` reports per-hit marker boxes** (#7405, 2026-11-23):
  each `rules[]` entry on ASAP7 now carries `markers` — one per hit, the
  edge pair's bounding box in microns in top-cell coordinates (`box_um`)
  with that pair's own `via_pad`/`router_fill` flags, sorted by box and
  capped at 200 per rule (`markers_truncated` when the cap cut the list) —
  beside the unchanged full counts, so a residual can be attributed without
  rebuilding the checks with `klayout.db`. Additive; no `schema_version`
  bump.
