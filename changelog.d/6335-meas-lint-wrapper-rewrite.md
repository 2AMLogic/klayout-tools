- **The `.meas` signal-position refusal's suggested card now replaces an
  existing `{...}` / `'...'` / `par("...")` wrapper instead of nesting
  `par('...')` inside it** (#6335, 2026-11-18): `klt sim` refuses these cards
  (none of the three works in ngspice) and still does, but the rewrite it
  offered kept the author's wrapper -- `{v(a)-v(b)}` suggested
  `{par('v(a)-v(b)')}`, `'v(a)-v(b)'` suggested `'par('v(a)-v(b)')'`, and
  `par("v(a)-v(b)")` suggested `par('par("v(a)-v(b)")')` -- each a card that
  aborts the deck. All three now suggest `par('v(a)-v(b)')`, with spaces
  inside the wrapper dropped. The quoted expression is the bare one, and in a
  `.control` block the `let` helper line and the `expr` measurement no longer
  carry the `par("...")` wrapper text. Message text only: the set of refused
  cards is unchanged, and no JSON field or `schema_version` changes.
