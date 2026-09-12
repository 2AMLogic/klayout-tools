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
klt kb search [<query>] [--where <figure><op><value>]... [--pdk <pdk>] [--format text|json]
klt kb validate                 [--recheck-measured] [--format text|json]
```

- `list` — id, title, spec_class for every entry (id-sorted).
- `show <id>` — the full entry for `kb/entries/<id>.json`.
- `search [<query>]` — case-insensitive keyword match over `title`,
  `topology`, `spec_class`, `layout_idioms`, and `notes`, optionally narrowed
  by numeric filters over an entry's `measured.figures` (`--where`, repeatable,
  AND'd together) and/or an exact `measured.pdk` match (`--pdk`). `<query>` is
  optional — omit it to filter purely on `--where`/`--pdk`. See "Numeric
  filters" below.
- `validate` — every entry parses as JSON, validates against
  `kb/schema/entry.schema.json`, has `id` matching its filename stem, and —
  when the entry sets `artifacts` or `measured` — that any
  `artifacts.netlist`/`artifacts.layout` path, or any
  `measured.figures[].testbench` path, it references actually exists on disk
  (resolved relative to the repository root; `artifacts.notes` and
  `measured.notes` are prose and are never treated as a path). This is the
  single implementation behind both the CI gate and `tests/test_kb.py`'s
  schema-conformance coverage. `--recheck-measured` additionally re-runs
  every distinct `measured.figures[].testbench` via `klt sim` and fails if a
  recorded figure has drifted — see "Recheck measured figures
  (`--recheck-measured`)" below.

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
    "artifacts": {
      "netlist": "examples/...",
      "layout": "path/to.gds",
      "notes": "..."
    },
    "measured": {
      "pdk": "sky130",
      "corner": "tt, 1.8V, 27C",
      "supply_v": 1.8,
      "notes": "...",
      "figures": [
        { "name": "av_db", "value": 55, "unit": "dB", "analysis": "ac", "testbench": "examples/..." }
      ]
    }
  }
}
```

`entry` is the exact contents of `kb/entries/<id>.json`, unmodified — see
[`kb/README.md`](../../kb/README.md#schema) for the field reference. An
unknown `<id>` is an error (exit `1`), not an empty/`null` result.

## `klt kb search [<query>]`

Same shape as `list`, plus the `query`/`where`/`pdk` filters that were applied:

```json
{
  "schema_version": 1,
  "query": "inductor",
  "where": [],
  "pdk": null,
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
skipped, never a match. `<query>` may be omitted (an empty string never
excludes an entry) to search purely on `--where`/`--pdk`. No result is
success (exit `0`), not an error.

### Numeric filters (`--where`, `--pdk`)

`--where <figure><op><value>` filters on an entry's `measured.figures`, e.g.
`--where av_db>=40`. `op` is one of `>=`, `<=`, `==`, `!=`, `>`, `<` (checked
longest-first, so `>=`/`<=` are never misparsed as `>`/`<`). Repeat `--where`
for multiple conditions — every one must match (AND), against possibly
different figures. `--pdk <pdk>` is a separate, case-insensitive exact match
against `measured.pdk` (e.g. `--pdk sky130`) — not a `--where` condition,
since `pdk` is a string, not a number.

An entry with no `measured` block, or missing the named figure, never
matches a `--where`/`--pdk` filter — it is excluded, not an error. A
malformed `--where` expression (no recognised operator, empty figure name,
or non-numeric value) is an application error (exit `1`), same as any other
`KbError`:

```
$ klt kb search --where av_db --format json
{ "schema_version": 1, "error": { "command": "kb search", "message": "--where 'av_db': expected '<figure_name><op><value>' with op one of >=, <=, ==, !=, >, <" } }
```

This is the preferred **first step** for KB-assisted topology selection
(`.claude/skills/design-topology-selection/SKILL.md`) whenever the S3 block
spec has numeric electrical targets: filter on numbers before falling back
to keyword search or a full `kb list` scan, since a numeric match is a much
stronger fit signal than a title/topology keyword hit.

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
  "examples/kb/<id>/testbench.spice"`), or `artifacts/<netlist|layout>: must
  be a repository-relative path without '..' segments: <path>` for an
  absolute path or one escaping the repository — a link only the author's
  machine can follow is not a verification link. The same two error shapes
  apply to `measured.figures[].testbench`, reported as
  `measured/figures/<index>/testbench: ...` — one entry per figure whose
  path is missing or escapes the repository. Empty when `valid` is `true`.

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

### Recheck measured figures (`--recheck-measured`)

Plain `klt kb validate` only checks that `measured.figures[].testbench`
*exists on disk* — it never re-runs the testbench, so a recorded figure can
silently drift out of sync with what the testbench actually produces (e.g.
someone edits a netlist's component values without re-running `klt sim` and
updating the KB entry). `--recheck-measured` closes that gap: for every
entry with a `measured` block, it re-runs each distinct
`measured.figures[].testbench` via `klt sim` (`klayout_tools.sim.run_sim`,
the same engine `klt sim` itself uses) and compares the fresh result against
the recorded `figures[].value`, adding an error (so `valid` is `false`) when:

- the fresh `klt sim` run itself fails (a `SimError` — bad request, missing
  PDK/model library, engine crash) — reported as `measured/figures/<index>/
  <name>: klt sim <testbench> failed to reproduce this testbench: <reason>`.
- the recorded value has drifted beyond a **fixed 1% relative tolerance**
  from the closest value the fresh run produces — reported as
  `measured/figures/<index>/<name>: recorded value <value> has drifted
  <pct> from the closest value klt sim <testbench> produces now (tolerance
  1%)`. Exact floating-point equality is not the bar: re-running ngspice on
  a different machine/version can perturb the last few significant digits
  without the underlying circuit having drifted at all.

A figure's recorded value need not be the literal, same-named measurement
value a testbench's `request.json` computes — e.g. `vref_min_v`/`vref_max_v`
are the min/max of one measurement across a corner sweep, and a `freq_hz`
figure recorded alongside a `period_s` one is its reciprocal, not a distinct
measurement. Rather than require every `measured` figure to name the exact
measurement/aggregation that produced it, a figure is considered reproduced
when its recorded value is within tolerance of *any* individual per-corner
measurement value, any measurement's rolled-up worst-case value, or the
reciprocal of either, from that fresh run — this recheck cannot invent a
false match after a real regression (nothing in the fresh run would land
near the stale value or its reciprocal), but it does mean a newly authored
figure must be reproducible by one of those forms or it will never recheck
clean.

A testbench referenced by more than one figure (or by more than one entry)
is only simulated once per `klt kb validate --recheck-measured` invocation.
This is expensive — it actually runs `klt sim` (ngspice, plus a full PDK
fetch for any testbench with real device models) for every measured
entry — so it is **not** run by plain `klt kb validate`, and is not part of
the `test` job's every-PR CI gate above. It is the check the nightly
`kb-drift-canary` GitHub Actions workflow
(`.github/workflows/kb-drift-canary.yml`) runs instead, on a `schedule` +
`workflow_dispatch` trigger only — mirroring `equiv-canary.yml`'s precedent
for an expensive, non-blocking-on-PRs simulation gate. A drifted figure
fails that job (surfacing as a red nightly check) but is never
auto-corrected — a human/agent should investigate whether the drift reflects
a deliberate netlist change with a forgotten `measured` update, a silicon
model update, or a real regression, and update `kb/entries/<id>.json`
accordingly.

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
