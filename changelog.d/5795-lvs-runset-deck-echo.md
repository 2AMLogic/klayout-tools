- **`klt lvs --engine klayout-runset` now echoes the caller's own
  `--deck-file` form, and `environment.klayout_binary` records a basename**
  (#5795, 2026-11-17): a *relative* `--deck-file` used to come back
  absolutized in both `deck_file` and `provenance.runset.deck_file` — the
  only path echoes in that envelope that discarded the caller's form — so a
  report built from repo-relative inputs still failed
  `klt env-provenance lint-envelope` unless the caller also knew to declare
  `--allow-prefix`. Both echoes now preserve the caller-given relative string
  (resolution is unchanged: the flag still resolves against the current
  directory, and `--check`/`--check --rerun` of such a report must run from
  that same directory — the anchor caveat the relative `layout`/`reference`
  echoes already carry). An absolute `--deck-file`, or a `deck_file` set in
  the request document, echoes byte-identically to before.
  `environment.klayout_binary` now records the binary's basename (e.g.
  `"klayout"`) in default mode — the form portable mode already chose in
  #3991 — so the portable-only special case is gone; the version stays in
  `environment.engine_version` and
  `provenance.runset.klayout_application_version`, and an override remains
  visible in the echoed `options`. A default-mode runset report built from
  repo-relative inputs now lints clean with no declarations, and
  `lint-envelope`'s recommendation names `request.portable_paths: true` as
  the first-choice remedy for a `klt`-produced envelope. No field is added,
  removed or retyped; no `schema_version` bump.
