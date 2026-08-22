# `klt layout-metrics`

Aggregate `klt layers` / `klt cells` / `klt drc` output for a **block
directory** into a single normalized `layout.json` — the data contract the
klayout-tools.org gallery site (epic #13) is built on. It never recomputes
metrics ad hoc: it calls the exact same library functions that back `klt
layers`, `klt cells`, and `klt drc`.

```
klt layout-metrics <block> [--deck sky130|gf180mcu|sg13g2]
                    [--pdk sky130|gf180mcu|sg13g2] [--output PATH]
                    [--dry-run] [--format text|json]
```

- `<block>` — path to a block directory (e.g. `blocks/example-block`). See
  "Block directory layout" below.
- `--deck` — optional DRC deck to run for the `drc.violation_count` field
  (currently: `sky130`, `gf180mcu`, `sg13g2`). Omitted by default — DRC is
  opt-in per invocation since the deck cannot be inferred from the block
  directory.
- `--pdk` — optional PDK family this block targets, recorded verbatim as the
  `pdk` field (same vocabulary as `--deck`). Omitted by default — like the
  deck, a block directory carries nothing that identifies its PDK, and this
  command never guesses one. **Unlike `--deck`** (best-effort: an unknown
  name just omits `drc`), an unknown `--pdk` exits `1`: an explicit,
  caller-supplied identifier that is wrong would be written straight into
  the contract.
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
may be added without breaking the contract, so consumers (the site's gallery
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
  "pdk": "sky130",
  "layout_file": "layout.gds",
  "layer_count": 12,
  "cell_count": 34,
  "instance_count": 120,
  "drc": { "deck": "sky130", "status": "clean", "violation_count": 0 },
  "renders": { "metal1": "renders/metal1.png" },
  "signals": {
    "schema_version": 1,
    "engine": "ngspice",
    "engine_version": "46",
    "status": "pass",
    "corner_count": 15,
    "default_corner_id": "tt/1.800V/27C",
    "passed": 15,
    "failed": 0,
    "errored": 0,
    "measurements": [
      {
        "name": "tphl",
        "unit": "s",
        "status": "pass",
        "worst_case": { "corner_id": "ss/1.620V/125C", "value": 6.1e-11, "margin": null }
      }
    ],
    "corners": [
      {
        "corner_id": "tt/1.800V/27C",
        "waveform": "signals/tt_1.800V_27C.json",
        "...": "..."
      }
    ]
  },
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
| `pdk`              | string          | **Optional.** The PDK family this block targets, from `--pdk`. See below.                            |
| `layout_file`      | string          | **Optional.** The layout file used, relative to `<block>`. Omitted when `status` is `no_artifacts`. |
| `layer_count`      | integer         | **Optional.** From `klt layers`. Omitted when the layout could not be parsed.                       |
| `cell_count`       | integer         | **Optional.** From `klt cells`. Omitted when the layout could not be parsed.                        |
| `instance_count`   | integer         | **Optional.** Sum of every cell's `instances` from `klt cells` (total placement records).           |
| `drc`              | object          | **Optional.** Present only when `--deck` was supplied and the run succeeded. See below.             |
| `renders`          | object          | **Optional.** Present only when at least one PNG exists under `output/renders/`. Filename stem -> path relative to `output/`. |
| `signals`          | object          | **Optional.** Present only when `output/sim/signals.json` exists. See below.                        |
| `status`           | string          | One of `"ok"`, `"partial"`, `"no_artifacts"` — see below.                                           |

### `pdk` field

Introduced by issue #1285. One of `klt`'s own PDK-family names — currently
`"sky130"`, `"gf180mcu"`, `"sg13g2"` — the same vocabulary `drc.deck` uses.
Adding it required no `schema_version` bump: same additive, omit-absent
convention as `drc`/`renders`/`signals`, so a `layout.json` written before
this field existed stays valid.

**It is never inferred.** Nothing in a block directory says which PDK its
layout targets, so the field appears only when a caller passed `--pdk`. The
content pipelines behind the klayout-tools.org gallery supply it where they
genuinely know it:

- `scripts/bootstrap-gallery-blocks.py` knows the PDK exactly — it walks
  `tests/corpus/<pdk>/` — and attaches it to every corpus block it writes.
- `scripts/ingest-canary.py` takes an explicit `--pdk` (authoritative) and
  otherwise falls back to the conservative `<pdk>-<name>` slug guess that
  already labels canary renders (`scripts/_gallery_common.py`'s
  `infer_pdk`), omitting the field entirely when that guess fails — no
  field rather than a wrong one.

The gallery site (`site/src/pages/DetailPage.tsx`) prefers this field when
picking the embedded GDS viewer's `pdk=` identifier, and falls back to its
own slug-prefix heuristic (issue #1060) for blocks that carry no `pdk`.

### `drc` object

| Field              | Type    | Description                                    |
| ------------------- | ------- | ----------------------------------------------- |
| `deck`              | string  | The deck name passed via `--deck`.              |
| `status`            | string  | `"clean"` or `"violations"`, from `klt drc`.    |
| `violation_count`   | integer | Total violation count, from `klt drc`.          |

### `signals` object

Introduced by issue #99 (epic #90 phase 2, "Gallery signals pipeline") --
`.meas`-backed transistor-level measurements from a `klt sim` PVT sweep
(`docs/cli/sim.md`), attached verbatim from `output/sim/signals.json` when
that file exists. This command does not run `klt sim` itself or validate
the file's shape beyond "is it a JSON object" — whatever build-time step
wrote it (for the klayout-tools.org gallery's 7 standard cells,
`scripts/gallery_signals.py`, invoked by
`scripts/bootstrap-gallery-blocks.py`) owns the content's correctness.
Adding this field required no `schema_version` bump — same additive,
omit-absent convention as `drc`/`renders`.

| Field                        | Type    | Description                                                                                     |
| ----------------------------- | ------- | ------------------------------------------------------------------------------------------------- |
| `schema_version`              | integer | The embedded `klt sim` response's own schema version (currently `1`) -- independent of this document's top-level `schema_version`. |
| `engine` / `engine_version`   | string  | The SPICE engine used (`"ngspice"`) and its reported version.                                     |
| `status`                      | string  | Aggregate `"pass"` / `"fail"` / `"error"` across every corner -- see `docs/cli/sim.md`.            |
| `corner_count`/`passed`/`failed`/`errored` | integer | Corner counts by outcome.                                                            |
| `default_corner_id`           | string  | **Optional.** The `corner_id` a viewer should select by default (the gallery pipeline emits its nominal-process nominal-PVT corner). Consumed by the site's waveform viewer (issue #100); falls back to `corners[0]` when absent. |
| `measurements`                | array   | Per-measurement rollup (name, unit, limits if any, worst-case corner+value) -- see `docs/cli/sim.md`'s `measurements[]`. |
| `corners`                     | array   | One entry per expanded PVT corner -- see `docs/cli/sim.md`'s `corners[]`, with each entry's `artifacts` object replaced by an optional `waveform` (see below). |
| `device_substitution`         | object  | **Optional.** Present only when the source netlist required a documented device-model substitution (see `scripts/gallery_signals.py`'s module docstring) -- maps the substituted device names. |

A corner's optional `waveform` is a **path relative to the block's
`output/` directory** (e.g. `"signals/tt_1.800V_27C.json"`), the same
convention `renders` values use, pointing at that corner's waveform JSON
artifact in `klt sim`'s documented shape (`docs/cli/sim.md`). `klt sim`'s
own per-corner `artifacts` object is deliberately *not* carried into
`layout.json` — those are absolute paths into the run's work directory,
meaningless once committed. The gallery pipeline stages only its nominal
corners' waveforms; see `scripts/gallery_signals.py`'s "Waveform
artifacts" docstring section.

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

A fourth status value, `"in design — simulation evidence"`, is emitted only
by `scripts/ingest-canary.py` (issue #62), never by this command itself —
it marks a pre-layout block ingested from an external canary repo (real
simulation evidence, `spec_summary`/`signals`, no GDS-derived metrics yet).
See [`../../blocks/README.md`](../../blocks/README.md#canary-blocks-issue-62)
for that pipeline's full field-level documentation.

## Text format

```
$ klt layout-metrics blocks/example-block --deck sky130 --pdk sky130
slug: example-block
name: Example Block
status: ok
pdk: sky130
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
| `1`       | `<block>` does not exist or is not a directory, or `--pdk` names an unknown PDK family. |
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
