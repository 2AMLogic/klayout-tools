# `klt deck`

Identify klayout-tools' built-in DRC/LVS rule decks (`sky130`, `gf180mcu`,
`sg13g2`): report the content hash *this* build ships for a deck (`hash`),
look up which release shipped a given revision (`resolve`, by content hash
or by `(name, version)`, against klayout-tools' own generated release
history, issue #623), or report this install's own deck content hash,
structural device-class coverage, and release status directly, with no
input layout needed (`info`, issue #1209).

```
klt deck hash    --deck <name>                              [--format text|json]
klt deck resolve --content-hash <sha256:hex> [--deck <name>] [--format text|json]
klt deck resolve --deck <name> --version <X.Y.Z>            [--format text|json]
klt deck info    [--deck <name>]                             [--format text|json]
```

The three are complements: `hash` answers "which deck revision will this
build use?" (issue #1202), `resolve` answers "which release shipped that
revision?" (issue #623), and `info` answers "what does this install
actually recognise, right now, with no input layout at all?" (issue #1209).

## `klt deck hash`

Report the `provenance.deck.content_hash` this build resolves for a built-in
deck — with **no layout file and no check run** (issue #1202).

```
klt deck hash --deck sky130 [--format text|json]
```

The content hash, not the package version, is what pins a run's rule set. But
obtaining it used to require *running a check*, and therefore having some
layout around to run it against: gating on the deck before doing any work
meant a throwaway `klt drc` used as a version probe, and was impossible
outright for a consumer with no layout handy (a CI preflight, a container
smoke test). This answers the same question directly, in one cheap call.

The value is computed by the same code path a real run records
(`klayout_tools._provenance`'s deck block), so it cannot drift from what
`klt drc --deck sky130 <layout>` would report in its
`provenance.deck.content_hash`.

```json
{
  "schema_version": 1,
  "deck": "sky130",
  "content_hash": "sha256:2e78949d63f03012c505528158948a250e18c2c21c8710c85a23a8243649f4d0",
  "released": false
}
```

- `deck` / `content_hash` — the deck name and its `sha256:`-prefixed content
  hash, named exactly as `provenance.deck` names them so a consumer comparing
  against a committed report compares like with like.
- `released` — the same tri-state signal `provenance.deck.released` carries
  (`true`/`false`/`null`; see [`../json-contract.md`](../json-contract.md)),
  from the generated release-history table. The *hash* never depends on that
  table: a missing or malformed table yields `released: null`, not an error.

An unknown deck name is a clean error (exit 1) through the standard envelope,
and names what is available:

```json
{
  "schema_version": 1,
  "error": {
    "command": "deck hash",
    "message": "unknown deck 'nope' (available: gf180mcu, sg13g2, sky130)"
  }
}
```

This is a question about **this build**, not about release history — pair it
with `klt deck resolve --content-hash <hash>` below to turn the answer into
the release that shipped it, and with [`klt version`](version.md) to identify
the tool build itself.

## `klt deck resolve`

Answers "which klayout-tools git tag/PyPI version shipped *this exact* deck
revision" — the tool-level piece that lets a committed report's pinned
`provenance.deck.content_hash` ([`docs/json-contract.md`](../json-contract.md))
be turned back into "what do I install to reproduce this", without
hand-bisecting this repo's own git history when more than one `klt` build is
installed locally.

**`klt --version` alone does not guarantee two installs run the same rule
set** — the version string identifies the *tool* build, not the DRC/LVS deck
content it ships with a rebuild of that same version (e.g. a local editable
install with an uncommitted deck edit). To confirm two runs actually used
byte-identical rules, compare their `content_hash` — from each run's `klt
drc`/`klt extract` JSON output, or straight from `klt deck hash --deck
<name>` above with nothing to run it against — then use `klt deck resolve
--content-hash <hash>` on the differing hash to identify which release (if
any) each install corresponds to. (Since issue #1202, `klt --version` does at
least distinguish a release from a post-tag build of the same version: see
[`version.md`](version.md).)

- `--content-hash sha256:<hex>` — resolve a deck content hash, e.g. as
  reported by `klt drc --deck sky130`'s `provenance.deck.content_hash`.
  Optionally narrow with `--deck` if the hash happens to collide across deck
  names (never observed in practice, but not structurally impossible).
- `--deck <name> --version <X.Y.Z>` — resolve a deck name + klayout-tools
  package version directly (both required together).

Exactly one query shape per invocation: give either `--content-hash` (with
an optional `--deck` narrower), or `--deck` **and** `--version` together.
Neither, or only one of `--deck`/`--version`, is a documented error (exit 1).

**This same lookup now also runs automatically, at generation time** (issue
#1193): every command that emits a shared `provenance.deck` block
(`klt drc`, `klt extract`, `klt lvs`, and others) includes a `released`
field alongside `content_hash` — `false` when the deck in use is not one
`klt deck resolve` can name a release for, `null` when the table itself
can't be checked. See `docs/json-contract.md`'s "Shared `provenance` block"
section. Running `klt deck resolve --content-hash <hash>` by hand is still
useful to find out *which* release a hash belongs to once `released: true`
confirms one exists.

**Both `klt deck resolve` and `provenance.deck` require a hash you already
have in hand** — normally obtained by running some verb against an actual
input layout first and reading `provenance.deck.content_hash` out of its
JSON report. `klt deck info` (below) closes that gap: it reports this
*install's own* deck hash and structural device-class coverage directly,
with no input layout needed at all — see issue #1209, where two
`klayout-tools==0.2.0` installs (PyPI vs. a from-source checkout) shipped
different `gf180mcu` deck content silently, because nothing surfaced the
difference short of a live extraction diff.

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

## `klt deck resolve` output

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

## `klt deck info`

```
klt deck info                 # every registered deck (sky130, gf180mcu, sg13g2)
klt deck info --deck gf180mcu # one deck only
```

```json
{
  "schema_version": 1,
  "decks": [
    {
      "deck": "gf180mcu",
      "content_hash": "sha256:79e71a1e7d84be3cfc82e4c70afbdf7b743ac1f361fb8e981f57831014d2e8b0",
      "device_classes": [
        "nfet", "pfet", "bjt", "cap_mim_2f0_m4m5_noshield", "resistor",
        "diode_nd2ps_06v0", "diode_pd2nw_06v0"
      ],
      "released": false,
      "release": null
    }
  ]
}
```

- `deck` — the deck name.
- `content_hash` — this install's own `sha256:`-prefixed deck module hash
  (the same computation and shape as `provenance.deck.content_hash`).
- `device_classes` — the device-class roles this deck is *structurally
  capable* of recognising (`ExtractionDeck.device_classes`) — independent of
  whether a given layout actually contains any device of that class. This is
  the field that would have caught issue #1209 directly: comparing this
  list's contents (not just its `content_hash`) is what distinguishes a deck
  that recognises `diode_nd2ps_06v0`/`diode_pd2nw_06v0` from one that
  predates diode support entirely.
- `released` / `release` — the same tri-state signal as
  `provenance.deck.released` (`true`/`false`/`null`; see above), plus, when
  `true`, the `{git_tag, git_commit, package_version}` of the release that
  shipped this exact hash (the same shape `klt deck resolve` returns).

Omitting `--deck` reports every registered deck in one call — useful for a
"what does this install actually ship" sanity check right after `pip
install klayout-tools` (or a from-source build), before running any
extraction at all. An unrecognised `--deck` name is a clean error (exit 1),
matching `klt extract`'s own "unknown deck" message.

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
