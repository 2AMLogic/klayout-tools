- **`klt place-and-route` discloses whether `library.dont_use` was actually
  checked: `cell_exclusions.validated`** (#7388, 2026-11-23): a supplied
  `library.dont_use` is checked for zero-match patterns against the resolved
  Liberty's own cell names, but when that cell list could not be established
  (unreadable/unparseable Liberty, or one with no `cell (...)` groups) the
  check was skipped silently — unlike `klt synthesize`, whose
  always-present `cell_exclusions` block reports the same skip as
  `validated: null` "rather than passing off an unchecked list as a checked
  one". A caller mirroring one `dont_use` list between the two verbs (which
  `docs/cli/place-and-route.md`'s "Library cell exclusions" section invites)
  got a check-performed signal from one verb and none from the other. The
  block — still present only when `library.dont_use` is supplied, a
  deliberate presence divergence from synthesize's always-present block now
  recorded in `docs/json-contract.md` — gains the same tri-state
  `validated`: `true` when each requested pattern was matched against the
  cell names, `null` when the list could not be established and the check
  was skipped. Additive; no `schema_version` bump.
