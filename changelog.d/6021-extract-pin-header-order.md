- **`klt extract --pins` hoists a header token on a whole-token match too**
  (#6021, 2026-11-17): #5534 made the top `.SUBCKT` header ordering match
  per component label, but that rule alone cannot match a declared name
  that itself contains `|` -- a caller may declare a two-label net by its
  joined spelling (`a|b`), and the header token's *parts* (`a`, `b`) are
  neither equal to it, so the declared pin silently stayed in its old
  position. The pre-#5534 whole-token comparison is restored alongside the
  per-label one, making the match a true superset: pipe-free headers
  behave exactly as before, since the two comparisons coincide there.
  `docs/cli/extract.md` also qualifies its "fully determined by `--pins`"
  wording for the #2000 duplicate-label carve-out: a declared name
  matching 2+ physically disconnected same-label nets keeps all of them
  promoted, hoisted in their existing header order, so `pin_count` can
  exceed the declared set's size. No JSON field or `schema_version`
  change.
