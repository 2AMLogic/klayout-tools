- **`klt pdk find` and `klt sim` say which bundle placed a PDK tree and where
  it sits** (#8007). A tree under a version-manager store reported only
  `source: "search root: ~/.ciel"` and `layout: "open_pdks"` — a directory
  name and the asset layout, neither of which says what ran there — while the
  tree's `.pdk-store.json` recorded a bundle digest, revision, sync tool and
  sync time that never reached a report. `klt pdk find` (JSON and text) and
  `provenance.pdk` now carry `install`, the nearest marker's
  `{sha256, version, via, synced}` or `null` when there is no usable evidence
  (so the placing tool reads as unknown rather than implied), and
  `store_layout`, the layout the tree was found in (`manager_family`,
  `legacy_manager` or `flat`, measured from the store an enabled symlink leads
  into; `null` when it cannot be established). A batch worker reports its own
  evidence in `environment.remote.worker_environment.pdk` and in each
  `fleet[]` member, so two hosts at one revision show their respective
  recorded bundles; an older worker's capture leaves both `null`. A present
  `SOURCES` file still supplies `version`, the revision-stamp comparison is
  unchanged, and both fields are excluded from `--rerun` drift like `source`.
  Additive; no `schema_version` bump. See `docs/cli/pdk.md` "Install
  provenance".
