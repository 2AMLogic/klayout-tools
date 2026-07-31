# `klt kb`

Query `kb/`, the flat-files knowledge base of published circuit designs
(topology, sizing strategy, layout idioms) the LLM reasoning module draws on
— see [`kb/README.md`](../../kb/README.md) for the schema summary, sourcing
rules, and how to add an entry. Runs entirely against the local repo
checkout; no network access, no index, no embeddings (stdlib-simple substring
matching, per `kb/README.md`'s flat-files design).

```
klt kb list                    [--format text|json]
klt kb show <id>                [--format text|json]
klt kb search <query>           [--format text|json]
klt kb validate                 [--format text|json]
```

- `list` — id, title, spec_class for every entry (id-sorted).
- `show <id>` — the full entry for `kb/entries/<id>.json`.
- `search <query>` — case-insensitive keyword match over `title`, `topology`,
  `spec_class`, `layout_idioms`, and `notes`.
- `validate` — every entry parses as JSON, validates against
  `kb/schema/entry.schema.json`, has `id` matching its filename stem, and —
  when the entry sets `artifacts` — that any `artifacts.netlist`/
  `artifacts.layout` path it references actually exists on disk (resolved
  relative to the repository root). This is the single implementation
  behind both the CI gate and `tests/test_kb.py`'s schema-conformance
  coverage.

Every subcommand emits through the shared envelope
([`docs/json-contract.md`](../json-contract.md): `schema_version`, error
shape, exit codes) — `--format json` is the API, `--format text` a courtesy
rendering.

## `klt kb list`

```json
{
  "schema_version": 1,
  "count": 3,
  "entries": [
    { "id": "inverter-based-comparator", "title": "...", "spec_class": "..." },
    { "id": "sky130-bandgap-reference", "title": "...", "spec_class": "..." },
    { "id": "sky130-spiral-inductor", "title": "...", "spec_class": "..." }
  ]
}
```

An empty `kb/entries/` is success (exit `0`), not an error.

## `klt kb show <id>`

```json
{
  "schema_version": 1,
  "entry": {
    "id": "sky130-bandgap-reference",
    "title": "...",
    "topology": "...",
    "spec_class": "...",
    "pdk_portability": { "primary_pdk": "sky130", "notes": "..." },
    "sizing_approach": "...",
    "layout_idioms": ["...", "..."],
    "source": { "citation": "...", "url": "...", "license_or_openness": "..." },
    "notes": "...",
    "artifacts": { "netlist": "examples/...", "layout": "path/to.gds" }
  }
}
```

`entry` is the exact contents of `kb/entries/<id>.json`, unmodified — see
[`kb/README.md`](../../kb/README.md#schema) for the field reference. An
unknown `<id>` is an error (exit `1`), not an empty/`null` result.

## `klt kb search <query>`

Same shape as `list`, plus the `query` that was searched:

```json
{
  "schema_version": 1,
  "query": "inductor",
  "count": 1,
  "entries": [
    { "id": "sky130-spiral-inductor", "title": "...", "spec_class": "..." }
  ]
}
```

Matching is a plain case-insensitive substring test against `title`,
`topology`, `spec_class`, each string in `layout_idioms`, and `notes` — an
entry with `layout_idioms: ["guard rings"]` matches a `--format text`
`search guard` or `search rings` query. `null`/absent optional fields are
skipped, never a match. No result is success (exit `0`), not an error.

## `klt kb validate`

```json
{
  "schema_version": 1,
  "valid": true,
  "entry_count": 3,
  "entries": [
    { "id": "inverter-based-comparator", "valid": true, "errors": [] },
    { "id": "sky130-bandgap-reference", "valid": true, "errors": [] },
    { "id": "sky130-spiral-inductor", "valid": true, "errors": [] }
  ]
}
```

- `valid` — `true` only if every entry's `valid` is `true`.
- `entries[].errors` — structured, human-readable messages for that entry:
  JSON-Schema validation errors (`<json-pointer path>: <message>`, or bare
  `<message>` when the error is at the document root), an `id` that doesn't
  match the filename stem, "invalid JSON" for a file that doesn't parse, or
  `artifacts/<netlist|layout>: referenced path does not exist: <path>` when
  the entry sets `artifacts` but the referenced file is missing (paths are
  resolved relative to the repository root, e.g. `artifacts.netlist:
  "examples/kb/<id>/testbench.spice"`). Empty when `valid` is `true`.

A malformed `kb/entries/*.json` file or a schema mismatch produces a
per-entry `valid: false` with a populated `errors` array — it does **not**
raise the application-level error path (a missing `kb/entries/` directory or
a missing/unreadable `kb/schema/entry.schema.json` still does; see "Exit
codes" below).

**Wired into CI**: the `test` job runs `klt kb validate --format json` after
`pytest`, so a malformed entry breaks the build the same way a real
invocation would surface it. Run it locally the same way:

```
klt kb validate --format json
```

## Exit codes

| Exit code | Meaning |
| --------- | ------- |
| `0` | `list`/`show`/`search`: success (including an empty result). `validate`: every entry is valid. |
| `1` | Environment problem — missing `kb/entries/`, missing/unreadable `kb/schema/entry.schema.json`, or (`show`) an unknown `<id>`. Documented error shape on stderr, per `docs/json-contract.md`. |
| `2` | Usage error (missing required argument, bad `--format` value, no subcommand) — from argparse. |
| `3` | `validate` only: ran successfully but found one or more invalid entries. The full report (including which entries failed and why) is still written to stdout, per the documented success shape above — this is a validation *finding*, not a tool failure. |

On the `1`-path error, a concise message is written to **stderr** and nothing
is written to stdout — no Python traceback:

```json
{ "schema_version": 1, "error": { "command": "kb show", "message": "kb entry not found: 'no-such-id'" } }
```
