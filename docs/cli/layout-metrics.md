# `klt layout-metrics`

Aggregate `klt layers` / `klt cells` / `klt drc` output for a **block
directory** into a single normalized `layout.json` — the data contract the
klayout-tools.org gallery site (epic #13) is built on. It never recomputes
metrics ad hoc: it calls the exact same library functions that back `klt
layers`, `klt cells`, and `klt drc`.

```
klt layout-metrics <block> [--deck sky130|gf180mcu] [--output PATH]
                    [--dry-run] [--format text|json]
```

- `<block>` — path to a block directory (e.g. `blocks/example-block`). See
  "Block directory layout" below.
- `--deck` — optional DRC deck to run for the `drc.violation_count` field
  (currently: `sky130`, `gf180mcu`). Omitted by default — DRC is opt-in per
  invocation since the deck cannot be inferred from the block directory.
- `--output` / `-o` — override the output path. Defaults to
  `<block>/output/layout.json`.
- `--dry-run` — print the computed `layout.json` without writing any file.
- `--format` — `text` (default, a human-readable summary) or `json`. Under
  `--format json`, stdout mirrors exactly what was written to disk (or would
  have been, under `--dry-run`) — a caller never needs to re-read the file.

The command runs fully headless via KLayout's batch database API
(`klayout.db`) — no GUI, no Qt — and is safe to run in CI.

## Block directory layout

A "block" is a directory containing:

- One GDSII/OASIS layout file at the top level. Preferred name is
  `layout.gds` or `layout.oas`; otherwise the single `*.gds`, `*.gds.gz`, or
  `*.oas` file found there is used. A block with no such file is reported
  with `status: "no_artifacts"` rather than raising.
- An optional `meta.json` with `name` and/or `description` string fields,
  used verbatim when present:

  ```json
  { "name": "Example Block", "description": "A short description." }
  ```

  `name` falls back to a title-cased version of the block directory name
  (the slug, e.g. `example-block` -> `Example Block`) when `meta.json` is
  absent or does not set it.
- An optional `output/renders/*.png` directory (written by `klt render`,
  #60) — every PNG found there is listed in `renders`, keyed by filename
  stem.

## JSON schema (the contract)

**JSON is the API.** Human-readable text output is a courtesy; the JSON
schema below is the stable contract. Per the project's rules, **breaking
(renaming, removing, or retyping) a field is a breaking change**. New fields
may be added without breaking the contract, so consumers (the Astro gallery
loader, #59) should ignore unknown fields. See
[`../json-contract.md`](../json-contract.md) for the envelope shared across
all `klt` commands (`schema_version`, error shape, exit codes).

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-31T12:00:00+00:00",
  "slug": "example-block",
  "name": "Example Block",
  "description": "A short description of the block.",
  "layout_file": "layout.gds",
  "layer_count": 12,
  "cell_count": 34,
  "instance_count": 120,
  "drc": { "deck": "sky130", "status": "clean", "violation_count": 0 },
  "renders": { "metal1": "renders/metal1.png" },
  "status": "ok"
}
```

### Top-level fields

| Field             | Type            | Description                                                                                       |
| ------------------ | --------------- | --------------------------------------------------------------------------------------------------- |
| `schema_version`   | integer         | Version of this command's JSON shape (starts at `1`).                                              |
| `generated_at`     | string          | ISO-8601 UTC timestamp of when this `layout.json` was produced.                                    |
| `slug`             | string          | The block directory's basename.                                                                     |
| `name`             | string          | Display name — from `meta.json`, else a title-cased fallback of `slug`.                             |
| `description`      | string          | **Optional.** From `meta.json`; omitted if not set.                                                 |
| `layout_file`      | string          | **Optional.** The layout file used, relative to `<block>`. Omitted when `status` is `no_artifacts`. |
| `layer_count`      | integer         | **Optional.** From `klt layers`. Omitted when the layout could not be parsed.                       |
| `cell_count`       | integer         | **Optional.** From `klt cells`. Omitted when the layout could not be parsed.                        |
| `instance_count`   | integer         | **Optional.** Sum of every cell's `instances` from `klt cells` (total placement records).           |
| `drc`              | object          | **Optional.** Present only when `--deck` was supplied and the run succeeded. See below.             |
| `renders`          | object          | **Optional.** Present only when at least one PNG exists under `output/renders/`. Filename stem -> path relative to `output/`. |
| `status`           | string          | One of `"ok"`, `"partial"`, `"no_artifacts"` — see below.                                           |

### `drc` object

| Field              | Type    | Description                                    |
| ------------------- | ------- | ----------------------------------------------- |
| `deck`              | string  | The deck name passed via `--deck`.              |
| `status`            | string  | `"clean"` or `"violations"`, from `klt drc`.    |
| `violation_count`   | integer | Total violation count, from `klt drc`.          |

### `status` values

- `"ok"` — a layout file was found and both `klt layers` and `klt cells`
  parsed it successfully.
- `"partial"` — a layout file was found but could not be fully parsed
  (unreadable or corrupt stream).
- `"no_artifacts"` — no layout file was found under the block directory.
  `layout_file`, `layer_count`, `cell_count`, `instance_count`, and `drc` are
  all omitted; `renders` may still be present.

`drc` is independent of `status`: a missing `--deck`, an unsupported deck
name, or a DRC engine error simply omits `drc` — it never changes `status`
or blocks the rest of the report.

## Text format

```
$ klt layout-metrics blocks/example-block --deck sky130
slug: example-block
name: Example Block
status: ok
layer_count: 12
cell_count: 34
instance_count: 120
drc: deck=sky130 status=clean violations=0
renders: metal1, poly
written: blocks/example-block/output/layout.json
```

Its exact layout is **not** part of the contract — parse the JSON instead.

## Exit codes and errors

| Exit code | Meaning                                                                 |
| --------- | ------------------------------------------------------------------------ |
| `0`       | Success — `layout.json` written (or printed, under `--dry-run`).         |
| `1`       | `<block>` does not exist or is not a directory.                          |
| `2`       | Usage error (missing argument, bad `--format` value) — from argparse.    |

A block that exists but lacks a layout file, DRC deck, or renders is **not**
an error — it exits `0` with `status: "no_artifacts"` or a report missing
the relevant optional fields, matching the graceful-degradation contract
`klayout-tools.org`'s loader (#59) expects.

On error, a concise message is written to **stderr** and nothing is written
to stdout. No Python traceback is printed.

- `--format text` (default): a plain-text line prefixed `klt layout-metrics:`.
- `--format json`: the documented JSON error envelope (see
  [`../json-contract.md`](../json-contract.md)):

  ```json
  {
    "schema_version": 1,
    "error": { "command": "layout-metrics", "message": "directory not found: blocks/missing" }
  }
  ```
