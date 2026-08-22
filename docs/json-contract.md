# JSON output contract

Every `klt <verb>` command supports `--format text|json` (default: `text`).
**JSON is the API; text is a courtesy rendering of the same data** (CLAUDE.md).
This document defines the shared envelope every command emits through, so
verbs conform from day one rather than being harmonized after the fact.

All commands emit through `src/klayout_tools/cli/output.py`'s
`emit_success()` / `emit_error()` helpers — no `*_cmd.py` module hand-rolls
`json.dump`/`json.dumps`.

## Design: additive envelope, not a wrapping envelope

Each command's JSON payload stays **flat** at the top level (e.g. `klt
layers`'s `file`, `dbu_um`, `layer_count`, `layers` fields), rather than being
nested under a new key like `{"result": {...}}`. The only envelope-level
addition is a `schema_version` field alongside the command's existing fields.
This keeps the contract additive: an already-shipped command's documented
fields are never renamed, removed, or nested as part of adopting this
contract.

## Success shape

```json
{
  "schema_version": 1,
  "...": "the command's own top-level fields, unchanged"
}
```

- `schema_version` (integer) — starts at `1`. **Versioned per command**, not
  globally: `klt layers` and `klt cells` evolve independently, so a
  breaking change to one command's JSON shape does not force a version bump
  on another. A command bumps its own `schema_version` only when it makes a
  non-additive (breaking) change to its own payload; adding new fields does
  not require a bump.
- All other top-level fields are defined by the individual command's own
  documentation (e.g. `docs/cli/layers.md`).
- Written to **stdout only**, as indented JSON with a trailing newline.

### Pre-1.0 caveat: value sets within an unchanged shape can grow

The `schema_version`/`klt --version` policy above covers the *shape* of a
payload (which top-level fields exist), not the full *set of values* a field
can take on. Before `klt` reaches `1.0`, an additive change can introduce a
new enum-like value — most notably a new `mismatches[].category` (and
therefore a new `category_counts` key) for `klt lvs`, or a new `violations[]`
rule id for `klt drc` — without a `schema_version` bump and without changing
the reported `klt --version` string. Such a change is a deliberate, additive
behavior improvement (e.g. a new class of finding the tool did not previously
surface), not schema drift, but it does mean two runs of the identical `klt`
binary and identical `0.1.0` version string are **not** guaranteed to report
the same category set for the same input across time, or across two builds
snapshotted on different days. `CHANGELOG.md` is the source of truth for
*which* categories/rule ids exist as of a given date — check it first before
assuming a category change is a bug.

If a downstream project needs to pin exact reproducibility (e.g. golden
acceptance data keyed on `category_counts`), key that pin off the shared
`provenance` block's `deck` (`sha256:` content hash) and `klayout_version`
fields below — not `klt --version` — until `klt` reaches `1.0` and this
caveat is retired.

This caveat is narrow: it covers a field's *value set* growing (a new enum
member appearing), not what an existing, unchanged-type field's value
*means*. Redefining the semantics of an already-shipped field — e.g. `klt
precheck`'s `layer_whitelist[].shapes` moving from "summed once per cell
definition" to "weighted by placement multiplicity across the full cell
hierarchy" (issue #452) — is a breaking change and does earn a
`schema_version` bump on that command, even though the field's name and
JSON type (`integer`) never changed. See `docs/cli/precheck.md` for that
concrete precedent.

## Shared `provenance` block

Verbs whose verdict depends on the exact tool build, PDK release, and rule
deck — currently `drc`, `lvs`, `extract`, `sim`, `size`, and `precheck` — emit a
shared top-level `provenance` block so a "clean"/"pass"/"match" result is
auditable and reproducible later. Two runs made against different deck
revisions or PDK releases are otherwise indistinguishable in the output, so a
signoff claim can't be checked or reproduced. This block is **additive** (see
above): adopting it required no `schema_version` bump on any verb.

```json
"provenance": {
  "klt_version": "0.4.2",
  "klayout_version": "0.29.8",
  "pdk": {"name": "sky130A", "source": "volare", "version": "<stamp>"},
  "deck": {"name": "sky130", "content_hash": "sha256:<hex>", "released": true},
  "input": {"content_hash": "sha256:<hex>"}
}
```

- `klt_version` — the running `klt` package version
  (`klayout_tools.__version__`). This is the plain, static package version —
  deliberately *not* the build-identity string (issue #1202), which a
  post-tag source build reports as `X.Y.Z+g<sha>`; that one is available from
  `klt version --format json` as `version`, alongside `git_commit` and the
  tri-state `is_release`. Record it beside a report when "which build
  produced this?" has to remain answerable later; `klt_version` alone cannot
  tell a release from a source build made after that release's tag. See
  `docs/cli/version.md`.
- `klayout_version` — the KLayout Python engine build (`klayout.__version__`),
  or `null` if unresolvable.
- `pdk` — the resolved PDK, as `{name, source, version}` (`name` is the
  variant, `source` is how it was found, `version` is the `SOURCES` stamp), or
  `null` when the run resolved no PDK (e.g. `klt drc`, which resolves none, or
  a `lvs` compare, which is topological).
- `deck` — the rule (or model) deck the run used, as
  `{name, content_hash, released}`. `content_hash` is a `sha256:`-prefixed
  hex digest of the deck file actually used, so "clean against *this exact*
  rule set" is a checkable claim. The whole block is `null` when no deck was
  involved (e.g. `lvs` against a pre-extracted netlist). `klt extract`
  additionally carries an `options` key (issue #595) when `--deck-option`
  selected a non-default flavour of a shared-geometry device family (e.g.
  gf180mcu's `{"poly_res": "2k"}`) — omitted entirely when no such option was
  given, so the block is otherwise unchanged. See `docs/cli/extract.md`'s
  "Selecting a shared-geometry resistor flavour". `klt drc --engine klayout`
  carries the same `options` key (issue #1306) when one or more `--deck-var
  NAME=VALUE` flags were passed, recording the extra `-rd` script globals
  that were threaded into the deck for that run (e.g. `{"feol": "true"}`) —
  likewise omitted entirely when no `--deck-var` was given. See
  `docs/cli/drc.md`.
  A pinned `content_hash` can be turned back into the klayout-tools git
  tag/PyPI version that shipped it with `klt deck resolve --content-hash
  <hash>` (issue #623) — a resolve-only lookup against a generated
  hash/version history table, not an in-process fetch of the historical
  deck; see `docs/cli/deck.md`. Note that `klt_version`/`klt --version`
  alone is *not* sufficient to confirm two runs used the same rule set (a
  rebuild of the same version can carry a different deck) — `content_hash`
  is the field that actually pins the rule set. The same hash can be obtained
  with no layout file and no check run at all from `klt deck hash --deck
  <name>` (issue #1202), whose payload is
  `{schema_version, deck, content_hash, released}` — the same `content_hash`
  and `released` values this block carries, so a consumer can gate on the
  deck a build will use *before* doing any work.
  - `released` (issue #1193) is that same `klt deck resolve` lookup run
    automatically, at generation time, so a "clean" report doesn't silently
    accumulate evidence against a deck revision no release ever ships. A
    tri-state signal, not a plain boolean: `true` when `content_hash` matches
    a released `klayout-tools` version; `false` when the deck history table
    loaded fine but confirms no release ships this hash (an unreleased dev
    checkout, an uncommitted deck edit, or a deck added after the last tag);
    `null` when the question is unanswerable — `content_hash` itself is
    `null`, or the generated history table
    (`src/klayout_tools/decks/_history.json`) is missing, unreadable, or
    malformed. `null` is deliberately distinct from `false`: a broken or
    absent history table must never be reported as a confirmed
    "this deck is unreleased" claim. `false` is a **non-fatal** signal — it
    never fails the run; it only makes an otherwise-invisible reproducibility
    gap visible in the output. No warning (`released: true`) is emitted for
    an ordinary run against a deck unchanged since its last release.
    Both `content_hash` and `released` require a run against an actual input
    layout to see; `klt deck info` (issue #1209) reports this install's own
    deck `content_hash` plus its structural device-class coverage
    (`ExtractionDeck.device_classes`) directly, with no input layout needed —
    see `docs/cli/deck.md`.
- `input` — the input layout stream the run was made against, as
  `{content_hash}` (same shape as `deck`). `content_hash` is a
  `sha256:`-prefixed hex digest of the file, so a stale committed report is a
  one-line diff against a freshly computed hash instead of being
  byte-identical to a current run. Populated by `drc` and `extract`; `null`
  when a verb has no single input layout to pin this way, including `lvs`,
  which already covers its two inputs via its own
  `environment.layout_sha256`/`reference_sha256` fields (predates this block
  and is intentionally not folded into it).

Fields that can't be resolved are `null` per the envelope convention — never
silently fabricated. The block is built once in
`src/klayout_tools/_provenance.py`; each verb's `docs/cli/<verb>.md` notes only
which of `pdk`/`deck`/`input` it populates.

**Verifying a committed report still reproduces.** A committed
`--format json` report (e.g. cited as `klt signoff` manifest evidence) is
only trustworthy for as long as its `provenance.deck.content_hash`/
`provenance.input.content_hash` still match the current deck/input — a
caller previously had to hand-roll that diff themselves. `klt drc`, `klt
lvs`, and `klt extract` (issue #1149, closing the gap the first two verbs'
issue #1106 left in the third) all support `--check <report.json>` (cheap:
re-hash and compare, no engine re-run) and `--check <report.json> --rerun`
(full: re-run and diff every verdict-bearing field, excluding
`klt_version`/`klayout_version`/`pdk.version`), reporting `status: "match"`
or `"drifted"` — see each verb's own `docs/cli/<verb>.md`, "`--check`
/ `--rerun`". This is what surfaces a curated deck rebuild that silently
changed device-recognition behavior (e.g. gf180mcu's substrate/well-tap
derivation) underneath a previously-committed report: since a deck is a
plain Python module, any byte change to it — cosmetic or
device-recognition-affecting — changes `content_hash`, and `--check`/
`--rerun` are what turn that into a caller-visible signal instead of a
silent mismatch.

## Error shape

Under `--format json`, errors are also JSON — not a plain-text line — written
to **stderr**, with **stdout left empty**. This means a caller never needs to
inspect stdout content to tell success from failure under `--format json`;
the exit code alone is authoritative.

```json
{
  "schema_version": 1,
  "error": {
    "command": "layers",
    "message": "file not found: missing.gds"
  }
}
```

- `error.command` — the subcommand name (e.g. `"layers"`).
- `error.message` — a concise, human-readable description of what went wrong.
  No Python traceback is ever emitted.

Under `--format text` (the default), errors remain the pre-existing
plain-text stderr line: `klt <command>: <message>`. Text is a courtesy
rendering, not the contract, so this shape is not versioned.

## Exit codes

| Exit code | Meaning                                                                                     |
| --------- | -------------------------------------------------------------------------------------------- |
| `0`       | Success. The documented success payload was written to stdout.                               |
| `1`       | Application-level error (e.g. missing/unreadable file). Documented error shape on stderr.     |
| `2`       | Usage error (missing required argument, invalid `--format` choice, etc.) — raised by argparse before a command's handler runs. |

Codes `0`/`1`/`2` mean the same thing for every verb. A command may define
**additional** codes above `2` for outcomes that are neither success nor
tool failure, documented in its own `docs/cli/<verb>.md` — e.g. `klt drc`
exits `3` when the deck ran successfully but found violations (a successful
run, so the documented success payload is still on stdout). Extensions never
redefine `0`/`1`/`2`.

**Carve-out:** exit code `2` and its accompanying stderr output are argparse's
own behavior, produced before any subcommand's `run()` executes. They are
deliberately out of scope for the shared `output.py` helper — argparse always
writes plain text for usage errors, in both `--format text` and `--format
json` modes, since format-specific handling would require parsing the
arguments before the parser itself has rejected them.

## `--format text` vs `--format json`

- `text` (default) — a human-readable rendering. Its exact layout is **not**
  part of the contract and may change between releases without notice.
- `json` — the stable API. Breaking a JSON field (renaming, removing, or
  retyping it) is a breaking change per CLAUDE.md; adding a field is not.

## Adding a new command

1. The library function backing the command returns a plain dict payload
   including `schema_version` (see `layers_report()` in
   `src/klayout_tools/layers.py` for the pattern) — not the CLI layer, so the
   version travels with the payload wherever it's reused (e.g. a future MCP
   server, per `docs/ARCHITECTURE.md`).
2. The `*_cmd.py` module's `run()` calls `output.emit_success(payload,
   args.format, text_renderer)` on success and `return output.emit_error(name,
   message, args.format)` on the documented error path.
3. Document the command's fields in `docs/cli/<verb>.md`, including its
   `schema_version`.

See `src/klayout_tools/cli/layers_cmd.py` and `docs/cli/layers.md` for a
worked example.
