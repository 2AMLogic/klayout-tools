# `klt deck`

Identify klayout-tools' built-in DRC/LVS rule decks (`sky130`, `gf180mcu`,
`sg13g2`): report the content hash *this* build ships for a deck (`hash`),
look up which release shipped a given revision (`resolve`, by content hash
or by `(name, version)`, against klayout-tools' own generated release
history, issue #623), report this install's own deck content hash,
structural device-class coverage, and release status directly, with no
input layout needed (`info`, issue #1209), list the rule values a deck
enforces (`rules`, issue #2308), or list the drawn-layer predicate set each
of a deck's device classes is recognised by (`devices`, issue #2365).

```
klt deck hash    --deck <name>                              [--format text|json]
klt deck resolve --content-hash <sha256:hex> [--deck <name>] [--format text|json]
klt deck resolve --deck <name> --version <X.Y.Z>            [--format text|json]
klt deck info    [--deck <name>]                             [--format text|json]
klt deck rules   --deck <name> [--rule <id>]                 [--format text|json]
klt deck devices --deck <name> [--class <name>]              [--format text|json]
```

The five are complements: `hash` answers "which deck revision will this
build use?" (issue #1202), `resolve` answers "which release shipped that
revision?" (issue #623), `info` answers "what does this install
actually recognise, right now, with no input layout at all?" (issue #1209),
`rules` answers "what numbers does this deck actually enforce?" (issue
#2308) — the pre-layout question `klt drc`, which needs a stream, cannot
answer — and `devices` answers "what must a layout **draw** for one of those
device classes to be recognised?" (issue #2365).

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
  predates diode support entirely. It says what the deck **can find**; it
  does not say what a layout must **draw** for one to be found — that is
  [`klt deck devices`](#klt-deck-devices) below.
- `released` / `release` — the same tri-state signal as
  `provenance.deck.released` (`true`/`false`/`null`; see above), plus, when
  `true`, the `{git_tag, git_commit, package_version}` of the release that
  shipped this exact hash (the same shape `klt deck resolve` returns).

Omitting `--deck` reports every registered deck in one call — useful for a
"what does this install actually ship" sanity check right after `pip
install klayout-tools` (or a from-source build), before running any
extraction at all. An unrecognised `--deck` name is a clean error (exit 1),
matching `klt extract`'s own "unknown deck" message.

## `klt deck rules`

Report the **numbers a deck enforces** — every registered rule's id,
description, check kind, drawn layers, and distance threshold in micrometres
— with **no layout file and no check run** (issue #2308).

```
klt deck rules --deck sky130                          # every rule in the deck
klt deck rules --deck sky130 --rule poly.width.1      # one rule, by exact id
klt deck rules --deck sky130 --format json
```

`klt drc` reports *violations* and therefore needs a stream; `klt deck info`
reports a deck's identity and device coverage but not its rule table. Neither
answers the pre-layout question — area budgeting, device pitch, whether a
proposed segmentation is drawable at all — that has to be settled *before* any
GDS exists. Answering it used to mean transcribing a constant out of a deck
comment by hand, producing a copy that silently stops tracking the deck the
moment the deck moves, and a citation ("0.15 µm, `poly.1a`") that no reviewer
could re-check mechanically.

```json
{
  "schema_version": 1,
  "deck": "sky130",
  "content_hash": "sha256:a1d90e066e822d0a8f36e2d4f4969b83df4360692f689b5815be9c3f151f7c8e",
  "nominal_dbu_um": 0.001,
  "rules": [
    {
      "id": "poly.width.1",
      "description": "minimum poly width",
      "check": "width",
      "layers": ["poly.drawing"],
      "scope": "poly",
      "value_um": 0.15,
      "value_dbu": 150,
      "limits": {},
      "provenance": {
        "source_repo": "fossi-foundation/open-pdks",
        "source_path": "sky130/klayout/sky130.lydrc",
        "rule_id": "poly.1a",
        "commit": "c6d73a35f524070e85faff4a6a9eef49553ebc2b"
      }
    }
  ]
}
```

- `content_hash` — this install's own deck module hash, the same value
  `klt deck info`/`provenance.deck.content_hash` report. Cite it alongside a
  constant and the citation becomes re-checkable: the next PDK bump changes
  the hash, so a stale transcription is detectable instead of invisible.
- `nominal_dbu_um` — the deck's `NOMINAL_DBU_UM`, the factor `value_dbu` was
  converted by.
- `id` — **this deck's** rule id (`poly.width.1`), following klt's own
  `<layer>.<check>.<n>` convention. The *upstream PDK's* rule number
  (`poly.1a`, `DF.6_LV`) is reported separately as `provenance.rule_id` — the
  two namespaces are deliberately distinct (see
  [`drc.md`](drc.md)'s note that rule ids are a stable public contract).
- `value_um` / `value_dbu` — the rule's distance threshold, in micrometres and
  in the deck's nominal database units. Both are `null` for the check kinds
  that do not use a distance threshold (`area`, `density`, `antenna`) rather
  than reporting the unused `0` those rules author as a placeholder — "this
  rule publishes no distance" is a machine-readable answer, not a fabricated
  `0.0`.
- `limits` — the kind-specific numerics for exactly those kinds:
  `area_min_um2`/`area_max_um2`, `density_window_um`/`density_min`/
  `density_max`, or `antenna_ratio_max`. Always present; `{}` for a plain
  distance check, whose one number is `value_um`. Unset bounds are `null`.
- `layers` — the drawn layer name(s) the rule reads, in `layer`/`other_layer`
  order (`"poly.drawing"`; the raw `"<layer>/<datatype>"` pair when the deck
  publishes no name for it).
- `scope` — the DRM section or rule-id family this rule implements
  (`coverage.deck_scope`'s per-rule source), or `null` when unscoped.
- `provenance` — the rule's structured upstream citation, or `null` when the
  rule carries no structured provenance yet (its inline comment in the deck
  module remains the only record). `commit` is `""` when the deck pins no
  upstream commit for that citation.

Rules are reported in the deck's own declaration order (stable, not sorted —
it groups rules by layer the way the deck module itself reads).

An unknown `--deck` is a clean error (exit 1) through the standard envelope,
matching `klt deck info`'s. So is an unknown `--rule` id — deliberately an
error rather than an empty `rules` list, since a silent empty result reads as
"this deck has no such constraint", a materially different claim from "you
asked for a rule id that does not exist":

```json
{
  "schema_version": 1,
  "error": {
    "command": "deck rules",
    "message": "deck 'sky130' has no rule with id 'CO.7' (59 rules registered; run `klt deck rules --deck sky130` to list them)"
  }
}
```

Note the deck tables are klt's own curated transcriptions, not the PDK's
executable deck — coverage is the set of rules klt implements, not the full
DRM. `provenance` is what ties each value back to the upstream source it came
from; `klt drc`'s `coverage.deck_scope` answers the complementary "what
sections does this deck claim" question.

## `klt deck devices`

Report **what a layout has to draw** for each of an extraction deck's device
classes to be recognised — its device-recognition marker layer plus every
per-terminal `requires`/`excludes` predicate, with layer/datatype numbers —
with **no layout file and no extraction run** (issue #2365).

```
klt deck devices --deck gf180mcu                              # every class
klt deck devices --deck gf180mcu --class diode_pd2nw_06v0     # one class
klt deck devices --deck gf180mcu --format json
```

`klt deck info`'s `device_classes` says a deck *can* recognise a
`diode_pd2nw_06v0`. It does not say that doing so requires a `diode_mk`
(115/5) marker over the junction **and** `Pplus` (31/0) + `Dualgate` (55/0)
over its anode — and a layout missing any single member of that set extracts
as `device_count: 0`, which reads exactly like "this layout legitimately
contains no diodes". Before this command the only way to find out which
member was missing was to read `decks/gf180mcu.py`'s `DiodeDevice` entry
field by field, which two separate passes on a real design had to do
(2AMLogic/gf180-drone-fc, its issues #24 and #37).

```json
{
  "schema_version": 1,
  "deck": "gf180mcu",
  "content_hash": "sha256:95c2eb91…",
  "device_classes": [
    {
      "name": "diode_pd2nw_06v0",
      "kind": "diode",
      "marker": { "layer": 115, "datatype": 5, "name": "diode_mk" },
      "marker_gated": true,
      "predicate_gated": true,
      "terminals": [
        {
          "name": "anode",
          "layer": { "layer": 22, "datatype": 0, "name": "Comp" },
          "requires": [
            { "layer": 31, "datatype": 0, "name": "Pplus" },
            { "layer": 55, "datatype": 0, "name": "Dualgate" }
          ],
          "excludes": [ { "layer": 12, "datatype": 0, "name": "DNWELL" } ]
        },
        {
          "name": "cathode",
          "layer": { "layer": 21, "datatype": 0, "name": "Nwell" },
          "requires": [],
          "excludes": [ { "layer": 12, "datatype": 0, "name": "DNWELL" } ]
        }
      ],
      "required_layers": [ … ],
      "excluded_layers": [ … ],
      "provenance": {
        "source_repo": "google/globalfoundries-pdk-libs-gf180mcu_fd_pv",
        "source_path": "libs.tech/klayout/lvs/rule_decks/diode_extraction.lvs",
        "rule_id": "gf180mcu_fd_pr__diode_pd2nw_06v0",
        "commit": "c6d73a35f524070e85faff4a6a9eef49553ebc2b"
      }
    }
  ]
}
```

- `deck` / `content_hash` — the deck name as given, and this install's own
  deck module hash (the same value `klt deck info`/`klt deck rules`/
  `provenance.deck.content_hash` report). Cite it alongside a transcribed
  layer number and the citation stays re-checkable on the next PDK bump.
- `name` — the `devices[].class` label this class extracts as (the same
  strings `klt deck info`'s `device_classes` lists).
- `kind` — the recognition family: `mos`, `bipolar`, `capacitor`,
  `mom_capacitor`, `resistor`, `diode`.
- `marker` — the class's device-recognition marker layer, or `null` for a
  markerless (drawn-geometry-only) class.
- `marker_gated` / `predicate_gated` — booleans, so "which classes need
  something beyond their own conductor/diffusion geometry" is answerable by
  filtering rather than by reading prose. **Marker- and predicate-gated
  classes are the minority**, which is exactly what makes them expensive:
  every other family a deck recognises is driven off drawn geometry alone,
  so nothing about a layout suggests one family has a prerequisite the
  others do not. `predicate_gated` is `true` when any terminal declares a
  non-empty `requires` — note this is honestly `true` for `pfet` (a PMOS is
  not recognised outside `nwell`) and `false` for `nfet`.
- `terminals` — one entry per recognised terminal:
  `{name, layer, requires, excludes}`. `layer` is `null` for a terminal
  formed by the substrate rather than by a drawn mask (gf180mcu's
  `diode_nd2ps_06v0` anode). Every layer in `requires` must **also** cover
  the region; every layer in `excludes` is subtracted from it.
- `required_layers` / `excluded_layers` — the flattened union in
  first-mention order (marker first, then each terminal's own layer and its
  `requires`). `required_layers` is the "what am I missing?" list — every
  one of these has to be drawn over the same geometry, and missing any one
  of them fails identically and silently.
- `provenance` — the entry's structured upstream PDK-LVS citation, or `null`
  when it carries none yet (the same shape `klt deck rules` reports).
- Every layer is `{"layer": <int>, "datatype": <int>, "name": <str|null>}` —
  the raw GDS numbering a layout is drawn in, plus the deck's published name
  for the pair when it publishes one (`null`, never a fabricated name, when
  it does not).

`klt extract` reports the same fact from the other end: a junction-diode
region that matches every predicate of a class except one or more members of
this set now produces a `warnings[]` entry naming what is missing, instead of
a silent `device_count: 0` — see
[`extract.md`](extract.md)'s "Marker-gated and predicate-gated device
classes" section.

An unknown `--deck` is a clean error (exit 1) through the standard envelope.
So is an unknown `--class` — deliberately an error rather than an empty
`device_classes` list, since a silent empty result reads as "that class has no
drawn-layer requirements", a materially different claim from "you asked for a
class this deck does not declare":

```json
{
  "schema_version": 1,
  "error": {
    "command": "deck devices",
    "message": "deck 'gf180mcu' declares no device class named 'diode_pw2nd' (declared: nfet, pfet, bjt, …; run `klt deck devices --deck gf180mcu` to list them)"
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
