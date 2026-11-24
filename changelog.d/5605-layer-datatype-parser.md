- **One acceptance rule for every `LAYER/DATATYPE` parser** (#5605,
  2026-11-15): the five hand-rolled `"<layer>/<datatype>"` string parsers
  disagreed on what they accept -- the four `split("/")`-based copies took
  signed numbers (`-68/20` parsed as layer -68, `+68/20` as layer 68) while
  `klt tap-distance`'s regex rejected them. They now all delegate to one
  public shared-core rule (`klayout_tools._paths.parse_layer_datatype`,
  exposed to CLI flags as `klayout_tools.cli._parsing.parse_layer_spec`),
  which accepts digits only and tolerates surrounding whitespace. This is a
  user-visible tightening on six shipped verbs. Three are CLI flags: `klt
  stats --window-layer`, `klt seam-check --layer` and `klt interface-check
  --pin-layer-a`/`--pin-layer-b` now reject `-68/20`, `68/-20` and `+68/20`
  with a clean exit-1 error instead of silently accepting a layer that can
  match no geometry. The other three are spec-file fields: the shared helper
  the `klt erc`, `klt mom` and `klt power` spec validators already called was
  itself one of the loose `split("/")` copies, so their layer fields
  (`stackup[].layer`/`label_layer`/`active_layer`, `vias[].layer`, the
  `ties[]` layer fields, `devices[].body_layer`, `mom`'s stackup `layer`)
  accepted a signed spec too and now report it as malformed. `klt
  tap-distance` behaviour is unchanged (its parsers were already strict,
  including `--net-spec` layer fields, whose error message now comes from
  the shared helper). No `schema_version` changes: payload shapes are
  untouched, only flag/spec *validation*.
