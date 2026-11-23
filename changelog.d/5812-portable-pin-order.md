- **`klt lvs` records PDK-rooted `provenance.pin_order_sources[].path` as
  `"$PDK_ROOT/<relative>"` by default** (#5812, 2026-11-17): the default report
  echoed the absolute install path of each library pin-order file, so a
  committed report leaked a home-directory path that `klt env-provenance scan`
  flags. Files under the resolved PDK root now use the same token
  `portable_paths: true` already produces; `reference.pin_order_files` entries
  outside the PDK root stay absolute. The value changes, the type does not: no
  `schema_version` bump, no new fields. This is a narrow exception to #3798's
  byte-identical-by-default rule for this one field. Chip repos whose committed
  LVS records now fail `klt lvs --check` should re-mint them, not hand-edit.
