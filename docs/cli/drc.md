# `klt drc`

Run a headless DRC rule deck against a GDSII or OASIS layout stream and
report violations as structured data.

```
klt drc <file> --deck sky130 [--format text|json]
```

- `<file>` — path to a GDSII (`.gds`) or OASIS (`.oas`) file. KLayout
  auto-detects the stream format on read; the extension is not authoritative.
- `--deck` — required. The DRC deck to run. Currently: `sky130`.
- `--format` — `text` (default, a human-readable summary) or `json`.

## Engine

`klt drc` runs fully headless via the pip `klayout` package's native
`klayout.db.Region` check primitives (`width_check`, `space_check`,
`separation_check`, `enclosing_check`, `enclosed_check`, `notch_check`,
`overlap_check`) — the same C++ polygon-processing engine that backs
KLayout's higher-level DRC-DSL scripts, invoked directly instead of through
the script runner. There is **no dependency on the standalone `klayout`
application binary or its `.drc`/`.lydrc` script runner** — only
`pip install klayout` (already this repo's sole runtime dependency), so the
command runs anywhere that already runs in CI.

A "deck" is our own declarative rule table (`DrcRule`: rule id, layer,
check kind, threshold, optional second layer) that drives those check
primitives — not the official sky130 `.lydrc` script executed verbatim. See
"Coverage" below for what that means for rule fidelity.

## Coverage

The `sky130` deck is a **curated starter subset**, not the full sky130
design rule manual (which spans hundreds of rules). It currently covers 10
rules — width, spacing, and enclosure checks across the `poly`, `diff`,
`li1`, `met1`, `licon1`, and `mcon` layers — transcribed directly from the
official community sky130 KLayout DRC deck
([`fossi-foundation/open-pdks`](https://github.com/fossi-foundation/open-pdks),
`sky130/klayout/sky130.lydrc` and `sky130.lyt`; GPLv3). Each rule in
`src/klayout_tools/decks/sky130.py` cites the exact source rule id (e.g.
`"poly.1a"`) and comment it was transcribed from.

Two of the ten rules approximate an official rule defined on a *compound*
layer expression (a boolean union of two mask layers, e.g. `diff.or(tap)`)
as a check against a single drawn layer, because the native `Region` check
primitives check one layer, or one layer against one other layer, at a
time — they do not evaluate arbitrary layer expressions the way the DRC-DSL
script runner does. This is called out explicitly in each such rule's
docstring; the threshold *values* used are always the real, unmodified
source values.

Coverage is expected to grow incrementally in follow-on issues. A
`gf180mcu` deck is tracked separately (issue #15) and will register as a
sibling `DrcRule` list in `src/klayout_tools/decks/gf180mcu.py`, without
requiring changes to this adapter interface.

## Limitation: whole-layout, flattened

Each rule is checked against the **whole layout**, flattened per top cell
(via `Cell.begin_shapes_rec`) — there is no `--top <cell>` filter to scope
the check to a single cell in this version. If a layout has multiple top
cells, each is checked independently and violations report the top cell
they were found under.

## JSON schema (the contract)

**JSON is the API.** Human-readable text output is a courtesy; the JSON
schema below is the stable contract. Per the project's rules, **breaking
(renaming, removing, or retyping) a field is a breaking change**, and rule
`id` values are part of that contract — a rule id is never renumbered or
repurposed once shipped. New fields may be added without breaking the
contract, so consumers should ignore unknown fields. See
[`docs/json-contract.md`](../json-contract.md) for the envelope shared across
all `klt` commands (`schema_version`, error shape, exit codes).

```json
{
  "schema_version": 1,
  "file": "design.gds",
  "deck": "sky130",
  "dbu_um": 0.001,
  "status": "clean",
  "violation_count": 0,
  "rule_counts": {},
  "violations": []
}
```

On a run with findings:

```json
{
  "schema_version": 1,
  "file": "design.gds",
  "deck": "sky130",
  "dbu_um": 0.001,
  "status": "violations",
  "violation_count": 1,
  "rule_counts": { "poly.width.1": 1 },
  "violations": [
    {
      "rule": "poly.width.1",
      "description": "minimum poly width",
      "check": "width",
      "layer": "poly.drawing",
      "cell": "TOP",
      "bbox": { "left": 0, "bottom": 0, "right": 100, "top": 2000 },
      "polygon": [[0, 0], [0, 2000], [100, 2000], [100, 0]]
    }
  ]
}
```

### Top-level fields

| Field             | Type                     | Description                                                             |
| ----------------- | ------------------------ | ------------------------------------------------------------------------ |
| `schema_version`  | integer                  | Version of this command's JSON shape (starts at `1`; per-command).       |
| `file`            | string                   | The input path exactly as provided on the command line.                  |
| `deck`            | string                   | The deck name used (`"sky130"`).                                         |
| `dbu_um`          | number (float)           | Database unit in micrometres, same semantics as `klt layers`.            |
| `status`          | `"clean"` \| `"violations"` | Never `"error"` — a failed run does not emit this envelope at all (see Exit codes). |
| `violation_count` | integer                  | `len(violations)`.                                                       |
| `rule_counts`     | object\<string, int\>    | Per-rule-id violation counts; keys sorted for determinism.               |
| `violations`      | array\<object\>          | One entry per violating geometry, see below.                             |

### `violations[]` entries

| Field         | Type                | Description                                                                 |
| ------------- | ------------------- | ----------------------------------------------------------------------------- |
| `rule`        | string               | Stable rule id (e.g. `"poly.width.1"`) — never renumbered once shipped.       |
| `description` | string               | Human-readable rule description.                                              |
| `check`       | string               | The check kind (`"width"`, `"space"`, `"enclosing"`, etc.).                   |
| `layer`       | string               | `"name.purpose"` (e.g. `"poly.drawing"`) if the deck names the layer, else `"<layer>/<datatype>"`. |
| `cell`        | string               | Name of the top cell the violation was found under.                          |
| `bbox`        | object (dbu ints)    | `{"left", "bottom", "right", "top"}`, in database units.                     |
| `polygon`     | array\<[x,y]\> \| null | Vertices in database units, or `null` if the check produced a degenerate edge pair that could not be converted to a polygon. |

`violations` is sorted by `(rule, cell, bbox.left, bbox.bottom)` so repeated
runs against the same input produce identical, diff-clean output.

## Exit codes

| Code | Meaning                                                     |
| ---- | ------------------------------------------------------------ |
| `0`  | Ran clean — no violations.                                   |
| `1`  | Failed to run — bad file, unknown `--deck`, or engine error. |
| `2`  | Usage error (missing argument, bad `--format` value) — from argparse. |
| `3`  | Ran successfully, violations found.                          |

`2` is deliberately **not** reused for "violations found" even though some
DRC tools use a 2-way error/warning split on that code: `2` is already
spoken for by argparse in every other `klt` subcommand, and a script
depending on `klt drc`'s exit code must be able to tell "you typed something
wrong" apart from "the deck ran and found problems" apart from "the tool
itself failed."

On error (exit 1), a concise message is written to **stderr** and nothing is
written to stdout. No Python traceback is printed — including for an unknown
`--deck` name.

- `--format text` (default): a plain-text line prefixed `klt drc:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "drc", "message": "unknown deck 'nope' (available: sky130)" } }
  ```

Note that exit code `3` is a per-command extension of the shared exit-code
table: unlike `0`/`1`/`2`, it is specific to `klt drc` and means the deck ran
successfully and the documented success payload *is* on stdout.

## Worked example

See `examples/drc/`: `generate.py` builds `example.gds` (a poly bar
narrower than the minimum width, and a diff shape under-enclosing a licon1
contact — two seeded violations — plus one clean, wide met1 shape), and
`example.drc.json` is the exact expected output of:

```
klt drc examples/drc/example.gds --deck sky130 --format json
```
