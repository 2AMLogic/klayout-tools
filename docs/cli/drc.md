# `klt drc`

Run a headless DRC rule deck against a GDSII or OASIS layout stream and
report violations as structured data.

```
klt drc <file> --deck sky130|gf180mcu [--format text|json]
```

- `<file>` — path to a GDSII (`.gds`) or OASIS (`.oas`) file. KLayout
  auto-detects the stream format on read; the extension is not authoritative.
- `--deck` — required. The DRC deck to run. Currently: `sky130`, `gf180mcu`.
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

The `gf180mcu` deck is likewise a **curated starter subset**: 13 rules —
width, spacing, and enclosure checks across the `Poly2`, `Comp`
(diffusion/active), `Contact`, and `Metal1` layers, plus a first increment
of well/substrate-tap coverage (`Nwell` spacing and Nwell-tap enclosure) and
one bipolar (BJT)-specific device rule (`DRC_BJT` mark-layer separation) —
transcribed from the published GlobalFoundries 180nm MCU **Design Rule
Manual** ([`google/gf180mcu-pdk`](https://github.com/google/gf180mcu-pdk),
`docs/physical_verification/design_manual/`; Apache License 2.0),
specifically the "7.4 Nwell" (`NW.*`), "7.5 Comp" (`DF.*`), "7.7 Poly2"
(`PL.*`), "7.12 Contact" (`CO.*`), "7.13 Metaln" (`Mn.*`), and "10.7 DRC_BJT
Mark Layer" (`BJT.*`) sections. Unlike sky130 (transcribed from a live,
KLayout-runnable `.lydrc` script), the companion KLayout DRC-deck repo
([`google/globalfoundries-pdk-libs-gf180mcu_fd_pv`](https://github.com/google/globalfoundries-pdk-libs-gf180mcu_fd_pv))
does not yet open-source the core FEOL/BEOL width/space/enclosure checks as
executable rule-deck code, so `src/klayout_tools/decks/gf180mcu.py` instead
cites the DRM's own published rule ids (e.g. `"DF.1a"`, `"PL.1"`, `"CO.1"`,
`"Mn.1"`, `"NW.2a"`, `"DF.4d"`, `"BJT.3"`) and numeric values directly.

Seven of the thirteen gf180mcu rules approximate an official DRM rule in
some way — either a compound-layer context our single/two-layer check
primitives can't isolate (`comp.space.1`, `poly2.space.1`, `poly2.width.1`,
`nwell.enclosing.comp.1`), a bound our primitives don't support
(`contact.width.1`'s fixed-size square, approximated as a minimum only), or
context our engine has no data for at all — net-potential (`nwell.space.1`)
or device connectivity (`bjt.separation.comp.1`), both of which require
netlist/connectivity information the geometry-only check primitives don't
have. Each is called out explicitly in its rule's docstring in
`gf180mcu.py`; the threshold values used are always the real, unmodified
DRM values.

Coverage does **not** yet include: `Pplus`/`Nplus` implant-specific rules
(width/space/enclosure of the implant layers themselves), `LVPWELL` or
`DNWELL`, the remaining `BJT.*` rules (`BJT.1`/`BJT.2`, which key off
`DNWELL`), or 5V/6V high-voltage variants — left for follow-on issues.

Coverage is expected to grow incrementally in follow-on issues, for both
decks.

## Database units

Each deck's rule thresholds (`DrcRule.threshold_dbu`) are authored against a
**nominal** database unit — the dbu each deck's numeric values were
transcribed from the source rule table at (both `sky130` and `gf180mcu`
currently use `0.001` µm/dbu, i.e. a 1nm grid; see each deck module's
`NOMINAL_DBU_UM` constant in `src/klayout_tools/decks/`).

A layout stream's own database unit is whatever its author wrote it at, and
is not guaranteed to match a deck's nominal dbu — foundry PCell libraries,
GDS emitted by other tools, and older flows commonly use a coarser (e.g.
5nm) or finer (e.g. 0.5nm) grid. `klt drc` reads the layout's actual `dbu`
(the same value reported as `dbu_um` in the JSON output below) and scales
every threshold by `NOMINAL_DBU_UM / dbu` *before* it reaches a
`Region.*_check()` primitive, so a rule's physical meaning (e.g. "0.15um
minimum poly width") stays correct regardless of the stream's dbu. This
matters in both directions: an unconverted threshold interpreted against a
coarser dbu reads as physically *larger* (over-flagging clean geometry as a
violation), and against a finer dbu reads as physically *smaller* (silently
missing real violations) — the same geometry must produce the same verdict
regardless of `dbu_um`, and `klt drc` guarantees this by construction.

`bbox` and `polygon` coordinates in `violations[]` remain in the checked
layout's **own** database units (as documented below) — only the rule
*thresholds* are converted internally; report consumers that need physical
coordinates multiply by the report's own `dbu_um`, exactly as for any other
`klt` command's dbu-denominated output.

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
| `deck`            | string                   | The deck name used (`"sky130"` or `"gf180mcu"`).                         |
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
| `layer`       | string               | The deck's own layer name (e.g. sky130's `"poly.drawing"` or gf180mcu's `"Poly2"`) if the deck names the layer, else `"<layer>/<datatype>"`. |
| `cell`        | string               | Name of the top cell the violation was found under.                          |
| `bbox`        | object (dbu ints)    | `{"left", "bottom", "right", "top"}`, in database units.                     |
| `polygon`     | array\<[x,y]\> \| null | Vertices in database units, or `null` if the check produced a degenerate edge pair that could not be converted to a polygon. |

`violations` is sorted by
`(rule, cell, bbox.left, bbox.bottom, bbox.right, bbox.top)` so repeated runs
against the same input produce identical, diff-clean output. The full bbox is
part of the key (not just the lower-left corner) so violations sharing a corner
are still totally ordered — output stays canonical across platforms and KLayout
builds regardless of the engine's internal shape-enumeration order.

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
  { "schema_version": 1, "error": { "command": "drc", "message": "unknown deck 'nope' (available: gf180mcu, sky130)" } }
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
