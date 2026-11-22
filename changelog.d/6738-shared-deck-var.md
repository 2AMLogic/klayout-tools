- **`klt drc`/`klt lvs`/`klt extract` now parse `--deck-var` with one shared
  parser** (#6738, 2026-11-18): `klt drc` and `klt lvs` each kept a private
  copy of the `NAME=VALUE` parser `klt extract` already shared, and the copies
  had drifted (`klt drc` accepted a blank name, so `--deck-var =v` forwarded a
  nameless `-rd =v` global to KLayout). All three verbs now go through
  `cli/_parsing.parse_deck_options`. **Visible changes, all inside `--deck-var`
  input validation:** `klt drc --deck-var =v` is now an error (exit 1), as it
  already was for `klt lvs`/`klt extract`; whitespace around the name **and
  value** is now stripped by `klt drc`/`klt lvs` (`--deck-var "feol = true"`
  used to send the name `feol ` and the value ` true` verbatim; a value that
  genuinely needs leading or trailing whitespace can no longer be passed, as
  was already so for `klt extract`); and the error text for a malformed entry is now
  `--deck-var entry 'X' is not KEY=VALUE -- e.g. --deck-var poly_res=2k`
  (was `invalid --deck-var 'X': expected 'name=value' ...`) for all three. The
  error envelope's shape and exit code (1) do not change, and valid entries
  (including a repeated name, last-one-wins, and a value containing `=`) parse
  as before. Docs: `docs/cli/drc.md` and `docs/cli/lvs.md`'s `--deck-var`
  paragraphs.
