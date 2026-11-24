- **`klt place-and-route --from-def <routed.def>` runs only the DEF→GDS
  merge on a committed routed stream** (issue #7721, 2026-11-24): a new
  local entry point for the merge-time repair family
  (`min_area_repair`, `via_pad_trim` with `tip_check`/`router_fill_scan`,
  `power_stack_repair`) that `klt drc` re-grades never invoke and
  `--resume` cannot reach without the stage `.odb` checkpoints and
  `*.commit.json` records a committed result set does not carry. It
  resolves the LEF/GDS/macro views from the request (same resolution as a
  placement run, issue #4204 default-variant substitution included;
  `--pdk`/`--pdk-root` honored), runs the existing merger unchanged with
  no OpenROAD process, and writes `<stage_root>/<top>_from_def.gds` — its
  own name, so the committed `<top>.gds` being compared against is never
  overwritten, and a populated `outputs.dir` is allowed. Only
  `hdl_toplevel` and `pdk.cell_library` are required in the request; the
  original run's request works unchanged. The response reports the
  merge's own fields (`layer_map`, `def_net_names`, `min_area_repair`,
  `via_pad_trim`, `power_stack_repair`, `pin_conductor`,
  `gds_dbu_um`) plus an additive `from_def` block (the supplied DEF's
  path and `def_content_hash`, the resolved `tech_lef`/`cell_lef`/
  `cell_gds`/`macro_lefs`/`macro_gds`); routing, stage timing and ODB
  metrics are absent — they exist only when a placement run produced
  them. Refused by name: `--check`/`--rerun`/`--resume`/`--backend`
  combinations and a `post_route_spef` request (needs the route stage's
  OpenSTA checkpoint). **Additive — `schema_version` unchanged:** a new
  flag and a new response shape for it; every existing invocation and
  payload is untouched.
