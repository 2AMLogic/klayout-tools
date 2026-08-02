# `klt report`

Render one or more `klt` JSON envelope files into a single combined
human-readable (or GitHub Flavored Markdown) report — the missing piece
between "a `klt` verb wrote a JSON envelope" and "a human, or a GitHub
Actions step summary, can read it." Downstream block repos (and this
repo's own CI) point `klt report`'s output at `$GITHUB_STEP_SUMMARY` so
every PR gets a reviewable violation/metrics report with zero local
tooling — see the Tiny Tapeout `tt-gds-action` prior art cited in #250/#267.

```
klt report <file>... [--format text|json|github-summary]
```

- `<file>...` — one or more paths to `klt` JSON envelope files, in the
  order they should appear in the report. Any entry may be `-`, which reads
  one envelope from stdin (JSON on stdin, same as every other `klt` verb
  that accepts `-`, e.g. `klt lvs`).
- `--format` — `text` (default, a human-readable plain-text rendering),
  `json` (this command's own JSON envelope, see below), or
  `github-summary` (GitHub Flavored Markdown suitable for
  `$GITHUB_STEP_SUMMARY`).

Unlike every other `klt` verb, `--format` has a **third** choice,
`github-summary`, in addition to the usual `text`/`json` — see "Why a third
format" below.

## What it does

`klt report` reads each `<file>` as a JSON object and renders it into one
report **section**, in the order given, then concatenates the sections into
one combined report. It never re-runs the underlying check itself — it is
a pure, additive transform of JSON envelopes that already exist on disk
(per CLAUDE.md's "JSON is the contract; human-readable output is a
courtesy" rule), so it composes naturally into a pipeline like:

```
klt drc design.gds --deck sky130 --format json > drc.json
klt layout-metrics blocks/example-block --format json > metrics.json
klt report drc.json metrics.json --format github-summary >> "$GITHUB_STEP_SUMMARY"
```

### Envelope-kind detection

`klt report` never hardcodes which verb produced a given envelope — there
is no `"command"` field on a success envelope to key off of (see
[`../json-contract.md`](../json-contract.md)). Instead, each file's kind is
detected from its own JSON structure:

| Kind             | Detected by                                                  | Rendered as                                            |
| ---------------- | ------------------------------------------------------------- | -------------------------------------------------------- |
| `drc`            | a top-level `violations` array (`klt drc`'s shape)             | status + a Rule/Cell/Layer/BBox/Description table         |
| `lvs`            | a top-level `mismatches` array (`klt lvs`'s shape)              | status + a Category/Severity/Side/Description table       |
| `layout-metrics` | top-level `slug` + `name` + `status` together (`klt layout-metrics`'s `layout.json` shape) | a Metric/Value key-metrics table, plus DRC/Renders/Signals summary lines when present |
| `error`          | a top-level `error` object with a `message` (any verb's own `--format json` error output, per `docs/json-contract.md`) | the failed command name and message               |

A file matching none of these — including one with no `schema_version` at
all — is a **hard error** (see "Errors" below), not an empty or silently
skipped section: a typo'd path or a non-`klt` JSON file should fail loudly,
not produce a quietly-incomplete report.

### Omit-absent, never null

Within a `layout-metrics`-kind section, only the fields actually present in
the source envelope are rendered — a block reported with `status:
"no_artifacts"` (which omits `layer_count`/`cell_count`/etc., see
[`layout-metrics.md`](layout-metrics.md)) renders a shorter metrics table,
never a table with literal `null`/`undefined` cells. This mirrors the
gallery site's own "omit-absent" convention
(`site/src/data/types.ts`).

### Empty findings render a clear state, not an empty table

A `drc`/`lvs`-kind envelope with zero violations/mismatches renders "No
violations found." / "No mismatches found." instead of a table with a
header row and no data rows.

## Why a third format

`--format text`/`--format json` alone don't cover this command's actual
job: the whole point of `klt report` is to produce a *rendering* meant to
be pasted somewhere else (a GitHub Actions step summary), and that
rendering's markup (GitHub Flavored Markdown tables, `##` headings) is
neither the plain, no-markup `text` courtesy rendering every other verb's
console output uses, nor the machine-readable `json` contract. `--format
github-summary` names that third, equally first-class rendering target
explicitly rather than overloading `--format text` to sometimes mean
"plain text" and sometimes mean "markdown" depending on how it's piped.

## JSON schema (the contract)

**JSON is the API.** Per the project's rules, **breaking (renaming,
removing, or retyping) a field is a breaking change**. New fields may be
added without breaking the contract. See
[`../json-contract.md`](../json-contract.md) for the envelope shared across
all `klt` commands (`schema_version`, error shape, exit codes). This verb
is a renderer, but it does not get an exemption from that contract for its
own `--format json` output — the rendered `markdown` is included verbatim
so a caller can still pipe straight to `$GITHUB_STEP_SUMMARY` from JSON
output, alongside a structured `sections` summary for a caller that would
rather not parse markdown.

```json
{
  "schema_version": 1,
  "envelope_count": 2,
  "sections": [
    {
      "kind": "drc",
      "source": "drc.json",
      "title": "DRC Report: design.gds",
      "status": "violations",
      "item_count": 1
    },
    {
      "kind": "layout-metrics",
      "source": "metrics.json",
      "title": "Layout Metrics: example-block",
      "status": "ok",
      "item_count": null
    }
  ],
  "markdown": "## DRC Report: design.gds\n**Status:** ❌ violations\n...",
  "text": "DRC Report: design.gds\n----------------------\nStatus: violations\n..."
}
```

### Top-level fields

| Field            | Type            | Description                                                                          |
| ----------------- | --------------- | --------------------------------------------------------------------------------------- |
| `schema_version`  | integer          | Version of this command's own JSON shape (starts at `1`).                              |
| `envelope_count`  | integer          | Number of input files given on the command line (`len(<file>...)`).                    |
| `sections`        | array\<object\>  | One entry per input file, in the order given — a structured summary, see below.        |
| `markdown`        | string           | The full combined report as GitHub Flavored Markdown — identical to `--format github-summary`'s stdout. |
| `text`            | string           | The full combined report as plain text — identical to `--format text`'s stdout.        |

### `sections[]` entries

| Field         | Type              | Description                                                                          |
| ------------- | ------------------ | ---------------------------------------------------------------------------------------- |
| `kind`        | string              | `"drc"`, `"lvs"`, `"layout-metrics"`, or `"error"` — see "Envelope-kind detection" above. |
| `source`      | string              | The input file path (or `"-"`) this section was rendered from, exactly as given.         |
| `title`       | string              | The section's rendered heading text (without markdown's `## ` prefix).                    |
| `status`      | string \| null      | The source envelope's own `status` field (`drc`/`lvs`/`layout-metrics` kinds), `"error"` for an `error`-kind section, or `null` if the source envelope had none. |
| `item_count`  | integer \| null     | Violation/mismatch count for `drc`/`lvs` kinds; `null` for `layout-metrics`/`error` kinds (a key-metrics or error section has no "item count"). |

`sections[]` never embeds the underlying `violations[]`/`mismatches[]`
arrays themselves — a consumer that wants that raw structured detail should
read the original envelope file directly; `sections[]` is a summary index
into the rendered `markdown`/`text`, not a re-export of every source
envelope's full contract.

## Exit codes and errors

| Exit code | Meaning                                                                 |
| --------- | ------------------------------------------------------------------------ |
| `0`       | Report rendered successfully.                                            |
| `1`       | An input file was missing/unreadable/not valid JSON, was not a JSON object, or did not match a recognized envelope shape. |
| `2`       | Usage error (no `<file>` given, bad `--format` value) — from argparse.    |

Unlike `klt drc`/`klt lvs`, there is no additional exit code for "rendered
successfully but the underlying report shows violations/mismatches" — the
rendered envelopes' own pass/fail verdicts are *content* this command
reports, not a verdict this command's own exit code re-derives. A CI step
that must fail the build on a DRC violation should gate on `klt drc`'s own
exit code (`3`) earlier in the pipeline, not on `klt report`'s.

On error, a concise message is written to **stderr** and nothing is written
to stdout. No Python traceback is printed.

- `--format text` (default) and `--format github-summary`: a plain-text
  line prefixed `klt report:`.
- `--format json`: the documented JSON error envelope (see
  [`../json-contract.md`](../json-contract.md)):

  ```json
  {
    "schema_version": 1,
    "error": {
      "command": "report",
      "message": "envelope file 'bad.json' has an unrecognized shape (schema_version=1): no violations[], mismatches[], or layout-metrics-style (slug/name/status) fields found"
    }
  }
  ```

## Worked example

```
$ klt drc examples/drc/example.gds --deck sky130 --format json > /tmp/drc.json
$ klt report /tmp/drc.json --format github-summary
## DRC Report: examples/drc/example.gds
**Status:** ❌ violations
- Deck: sky130
- File: examples/drc/example.gds

| Rule | Cell | Layer | BBox | Description |
| --- | --- | --- | --- | --- |
| poly.width.1 | TOP | poly.drawing | (0,0)-(100,2000) | minimum poly width |
```
