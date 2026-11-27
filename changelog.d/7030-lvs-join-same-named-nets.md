- **Inline LVS can connect same-named nets by label** (#7030, 2026-11-22).
  Set `layout.join_same_named_nets` to `true` to compare a layout using implicit
  top-level connections. The report records this assumption in a warning and
  replays it with `--check --rerun`. The default follows drawn connections.
