# `klt deck`

Look up which klayout-tools release shipped a given DRC/LVS rule deck
(`sky130`, `gf180mcu`) revision, by content hash or by `(name, version)`,
against klayout-tools' own generated release history (issue #623).

```
klt deck resolve --content-hash <sha256:hex> [--deck <name>] [--format text|json]
klt deck resolve --deck <name> --version <X.Y.Z>            [--format text|json]
```

Answers "which klayout-tools git tag/PyPI version shipped *this exact* deck
revision" — the tool-level piece that lets a committed report's pinned
`provenance.deck.content_hash` ([`docs/json-contract.md`](../json-contract.md))
be turned back into "what do I install to reproduce this", without
hand-bisecting this repo's own git history when more than one `klt` build is
installed locally.

- `--content-hash sha256:<hex>` — resolve a deck content hash, e.g. as
  reported by `klt drc --deck sky130`'s `provenance.deck.content_hash`.
  Optionally narrow with `--deck` if the hash happens to collide across deck
  names (never observed in practice, but not structurally impossible).
- `--deck <name> --version <X.Y.Z>` — resolve a deck name + klayout-tools
  package version directly (both required together).

Exactly one query shape per invocation: give either `--content-hash` (with
an optional `--deck` narrower), or `--deck` **and** `--version` together.
Neither, or only one of `--deck`/`--version`, is a documented error (exit 1).

## Resolve-only, not fetch-or-build

`klt deck resolve` looks the query up in a **generated table** — it never
clones, checks out, or builds a historical klayout-tools revision in-process.
Once you have the reported `git_tag`/`package_version`, reproduce against it
yourself:

```bash
pip install "klayout-tools==0.1.0"
# or:
git checkout v0.1.0 && pip install -e .
```

This is a deliberate scope limit (issue #623's "Suggested capability"): an
in-process fetch-and-build mechanism would need the invoking environment to
already have (or fetch) the full klayout-tools git history — which
reintroduces, just automated, the exact friction this command exists to
remove for a PyPI/wheel install that has no such history available at all.

## `klt deck resolve`

```json
{
  "schema_version": 1,
  "query": {
    "content_hash": "sha256:3bf7dada5e1dc46d36411c6149a227599f41580edfeb78c00060b5756424f3d3",
    "deck": null,
    "version": null
  },
  "deck": "sky130",
  "content_hash": "sha256:3bf7dada5e1dc46d36411c6149a227599f41580edfeb78c00060b5756424f3d3",
  "git_tag": "v0.2.0",
  "git_commit": "c8e4f8cd77a563cc6f612877f48c1148a556b25c",
  "package_version": "0.2.0"
}
```

- `query` — echoes exactly what was given (`content_hash`/`deck`/`version`,
  each `null` when not part of the query), so a caller doesn't have to keep
  its own request around to interpret the result.
- `deck` / `content_hash` — the matched deck name and its exact
  `sha256:`-prefixed content hash (the same shape and computation as
  `provenance.deck.content_hash`).
- `git_tag` / `git_commit` — the klayout-tools release that shipped this
  exact deck revision.
- `package_version` — the PyPI version string for that same release (i.e.
  `pip install klayout-tools==<package_version>`).

**When more than one release shipped byte-identical deck content** (the deck
simply didn't change between two releases), resolving by `--content-hash`
reports the **newest** matching release — so resolving the
currently-installed build's own deck hash always reports back that same
currently-running version, never a stale earlier release that happened to
ship the same bytes first. Resolving by `--deck`/`--version` is always an
exact, unambiguous lookup regardless.

### Not found

A hash that predates the table's start, was never released, or a
`--deck`/`--version` combination that never shipped, is a clean error (exit
1) via the standard error envelope — never a silent empty/null result:

```json
{
  "schema_version": 1,
  "error": {
    "command": "deck resolve",
    "message": "no known release ships deck 'sky130' at version '99.0.0' (known deck history covers v0.1.0..v0.2.0; known decks: gf180mcu, sky130)"
  }
}
```

## The generated history table

`src/klayout_tools/decks/_history.json` is a lookup table — one entry per
`(deck, release)` pair, covering every `v*` git tag — mapping
`{deck, content_hash, git_tag, git_commit, package_version}`. It is
**generated, never hand-maintained**: `scripts/generate_deck_history.py`
walks this repo's own git tag history and rebuilds it from scratch, hashing
each `decks/*.py` module exactly the way
`klayout_tools._provenance.sha256_file` hashes it at runtime. Recording one
entry per release (not only when a deck's hash changes) is what makes both
query shapes exact:

- `--deck`/`--version` is a direct dict lookup for any real release — no
  "nearest earlier version" fallback logic at query time.
- `--content-hash` picking the *newest* match (see above) reports the
  currently-running version even when the deck hasn't changed recently.

### Regenerating

Run this once a release has been tagged and pushed (the tag must already
exist in git for the generator to see it), then commit the regenerated file
in a follow-up commit:

```bash
python scripts/generate_deck_history.py
git add src/klayout_tools/decks/_history.json
git commit -m "chore(decks): regenerate deck history table for vX.Y.Z"
```

Idempotent — re-running it against unchanged tag history reproduces
byte-identical output, so it is safe to run speculatively.

Coverage is **released** revisions only: an unreleased dev checkout's deck
hash will not resolve until it has shipped in a tagged release — expected,
per the not-found shape above, not a bug.
