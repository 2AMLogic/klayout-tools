# `klt signoff`

Two modes, one verb:

1. **Envelope aggregation** (the original mode, issue #309) — combine one or
   more `klt drc`/`klt lvs`/`klt extract`/`klt sim` JSON envelopes into a
   single pass/fail signoff verdict — the mechanical piece that
   [`.claude/skills/design-signoff/SKILL.md`](../../.claude/skills/design-signoff/SKILL.md)
   hand-assembled before this verb existed.
2. **Tier-verdict report** (`--manifest`, issue #722 — Phase 0 of epic #706)
   — render the full T1-T4 evidence-tier item skeleton, mechanically parsed
   from [`../design-evidence-tiers.md`](../design-evidence-tiers.md), and
   grade each item against a block manifest's declared kind and per-item
   evidence locations. See "Tier-verdict report" below.

```
klt signoff <file>... [--format text|json]
klt signoff --manifest <manifest-file> [--format text|json]
```

- `<file>...` — one or more paths to `klt drc`/`klt lvs`/`klt extract`/`klt
  sim` JSON envelope files (`--format json` output from any of those four
  verbs), in the order they should appear in `checks[]`. Any entry may be
  `-`, which reads one envelope from stdin (same convention as `klt
  report`/`klt lvs`). Mutually exclusive with `--manifest`.
- `--manifest` — path to a block manifest JSON file (or `-` for stdin);
  switches to the tier-verdict report mode instead of aggregating `<file>...`
  arguments. Mutually exclusive with `<file>...`.
- `--format` — `text` (default, a human-readable pass/fail summary) or
  `json` (this command's own JSON envelope, see below).

## What it does

`klt signoff` reads each `<file>` as a JSON object, classifies it by its own
structural shape (mirroring `klt report`'s envelope-kind detection — see
[`report.md`](report.md#envelope-kind-detection) — extended here to also
recognise `klt extract`'s and `klt sim`'s shapes), and combines them into
one verdict in two steps:

1. **Provenance consistency.** Every input's `provenance` block (issue
   #251, [`../json-contract.md`](../json-contract.md#shared-provenance-block))
   is compared: all checks that resolved a PDK must name the same
   `pdk.name`/`pdk.version`; all checks that populate `provenance.input`
   (`klt drc`/`klt extract`) must agree on `input.content_hash`; any two
   checks naming the *same* deck must agree on that deck's
   `content_hash`. If any of these disagree, `klt signoff` **refuses** to
   produce a pass/fail verdict at all (`status: "refused"`) — a "clean" DRC
   report against last week's layout combined with a "match" LVS report
   against today's is not a signoff, it is two unrelated facts, and a
   wrong-but-confident verdict is worse than a loud refusal. See
   "Provenance consistency" below for the exact comparison rules.
2. **Per-check pass/fail**, only once provenance is consistent: `klt drc`
   passes on `status: "clean"`, `klt lvs` on `status: "match"`, `klt sim` on
   `status: "pass"`. `klt extract` has no independent pass/fail — a present
   extract envelope is definitionally a successful extraction (`klt
   extract` either produces one or raises, which surfaces here as an
   `error`-kind check instead) — so it always counts as passed, but is
   still listed in `checks[]` so its `provenance` block participates in
   step 1 and its device/net counts are visible in the aggregated result.
   An `error`-kind entry (any of the four verbs' own `--format json`
   failure output, e.g. a captured `klt drc` run that hit a missing file)
   never passes.

Overall `status` is `"pass"` only if every check passed *and* provenance was
consistent; `"fail"` if provenance was consistent but at least one check did
not pass; `"refused"` if provenance was inconsistent (regardless of whether
the individual checks themselves passed — see the worked example below).

`klt signoff` never re-runs the underlying verb — like `klt report`, it is a
pure, additive transform of JSON envelopes that already exist on disk, so it
composes into a pipeline:

```
klt drc design.gds --deck sky130 --format json > drc.json
klt lvs request.json --format json > lvs.json
klt extract design.gds --deck sky130 --format json > extract.json
klt sim request.json --format json > sim.json
klt signoff drc.json lvs.json extract.json sim.json --format json
```

### Provenance consistency

| Field compared | Populated by (per `provenance` block) | Comparison scope |
| --- | --- | --- |
| `pdk.name` | Any check that resolved a PDK (`klt lvs`, `klt extract`, `klt sim`; `klt drc` resolves none) | All checks that populate it, together |
| `pdk.version` | Same as `pdk.name` | All checks that populate it, together |
| `input.content_hash` | `klt drc`, `klt extract` only | All checks that populate it, together |
| `deck[<name>].content_hash` | Any check naming a deck | Only checks naming the *same* deck `<name>` |

A check with no `provenance` block (an `error`-kind entry) or a `null`
`provenance` is excluded from every comparison — it already fails the check
itself, it does not also need to fail the provenance gate. See
[`../json-contract.md`](../json-contract.md#shared-provenance-block) for
which fields each verb populates and why some are `null` by design (e.g.
`klt lvs`'s `provenance.input`, which already covers its two netlist inputs
via its own `environment.layout_sha256`/`reference_sha256` fields).

### What this does not do yet

`klt signoff` combines what four `klt` verbs' own JSON already asserts. It
does **not** diff the aggregated result against a block's declared spec —
[`docs/design/design-pipeline.md`](../design/design-pipeline.md)'s S3 stage
(block specs) has no machine-readable schema yet (see that doc's §4 gap
map). The `design-signoff` skill still drives the full T1 qualification
report; envelope-aggregation mode is the mechanical building block
underneath the checklist items ("DRC clean", "LVS clean", "post-layout
verification") that this verb *can* check today, and refuses to guess at
the ones it can't (a spec-diff) rather than fabricate a verdict for them.

## Tier-verdict report (`--manifest`)

`klt signoff --manifest <file>` renders the **T1-T4 evidence-tier item
skeleton**, mechanically parsed from
[`../design-evidence-tiers.md`](../design-evidence-tiers.md) (never
duplicated in code, so the doc and this command can never drift), graded
against a caller-supplied **block manifest**:

```json
{
  "block": "my-block",
  "kind": "analog",
  "evidence": {
    "3": "drc.json",
    "4": {"file": "lvs.json", "content_hash": "sha256:<expected input hash>"}
  }
}
```

- `block` — optional, echoed back verbatim in the report.
- `kind` — required: `"analog"`, `"digital"`, or `"mixed-signal"` (see the
  doc's "Block kind" subsection). Selects which column of the doc's
  per-kind T1 items (1, 2, 5, 7) applies; a `"mixed-signal"` manifest
  renders **both** columns, once per partition.
- `evidence` — optional (default `{}`), a map from item id to either a bare
  file path (a `klt drc`/`lvs`/`extract`/`sim` `--format json` envelope, or
  `"-"` for stdin) or `{"file": ..., "content_hash": ...}` to also pin the
  check to an expected layout revision. Keys are `"<item id>"` for a
  kind-independent item (3, 4, 6, 8, 9, 10), or `"<item id>.<analog|
  digital>"` for a per-kind item — a `"mixed-signal"` manifest may still use
  the bare `"<item id>"` key for a kind-independent item to cite the same
  evidence in both partitions' rows, per the doc's mixed-signal guidance.

An item's `status` is `"met"` **only** when its `evidence` entry resolves to
a *readable* `klt` JSON envelope, classifiable as one of
`drc`/`lvs`/`extract`/`sim`, whose own check passed — and, if the evidence
entry pinned an expected `content_hash`, whose own
`provenance.input.content_hash` matches it (a mismatch means the check ran
against a *different* layout revision than the one being claimed: stale, so
it renders `"unmet"`, never a false pass). Every other case — no evidence
entry, a malformed entry, an unreadable/unparsable evidence file, an
unrecognised envelope shape, or a failing check — also renders `"unmet"`:
**this phase never infers a `"met"` verdict for an item with no runnable
check behind it.**

`T2`-`T4` render as single ladder-row items (per the doc's "The ladder"
table — only `T1` has an itemized checklist) and are always `"unmet"`: this
toolkit's closed loop targets T1, and T2+ require commercial tools/fab
access this repo has no mechanism to check.

**This phase (issue #722) is the item model, the doc parser, and the
interface only** — it does not run any check itself, only reads
pre-existing `klt` JSON envelopes named by the manifest. Wiring the actual
DRC/LVS/sim *gates* is a follow-on phase of epic #706.

### Tier-report JSON schema

```json
{
  "schema_version": 1,
  "block": "my-block",
  "kind": "analog",
  "tier": null,
  "t1_item_count": 10,
  "t1_met_count": 1,
  "source_doc": "docs/design-evidence-tiers.md",
  "items": [
    {
      "tier": "T1",
      "id": 3,
      "title": "DRC clean",
      "partition": null,
      "text": "latest `klt drc` JSON report: ...",
      "notes": [],
      "status": "met",
      "citation": {
        "file": "drc.json",
        "kind": "drc",
        "check_status": "clean",
        "content_hash": "sha256:...",
        "exit_status": 0
      }
    },
    {
      "tier": "T2",
      "id": null,
      "title": "T2 — signoff-validated",
      "partition": null,
      "text": "Validated on commercial signoff tools (T1, plus DRC/LVS signoff and simulation on commercial tools with the foundry's own decks)",
      "notes": [],
      "status": "unmet",
      "citation": null
    }
  ]
}
```

| Field           | Type              | Description                                                                          |
| --------------- | ------------------ | ---------------------------------------------------------------------------------------- |
| `schema_version`| integer             | Version of this report's own JSON shape (starts at `1`, independent of envelope-aggregation mode's `schema_version`). |
| `block`         | string \| null       | Echoed from the manifest's `block` field.                                                |
| `kind`          | string               | `"analog"`, `"digital"`, or `"mixed-signal"`, echoed from the manifest.                  |
| `tier`          | string \| null       | `"T1"` only if every rendered T1 item is `"met"`; otherwise `null` — no partial credit.  |
| `t1_item_count` | integer              | Number of rendered T1 items (10 for `analog`/`digital`, 20 for `mixed-signal`).          |
| `t1_met_count`  | integer              | Number of those items with `status: "met"`.                                              |
| `source_doc`    | string               | Always `"docs/design-evidence-tiers.md"` — where the item list was parsed from.          |
| `items`         | array\<object\>      | One entry per T1 checklist item (per partition, for `mixed-signal`), then one entry per T2-T4 ladder row. |

#### `items[]` entries

| Field       | Type              | Description                                                                          |
| ----------- | ------------------ | ---------------------------------------------------------------------------------------- |
| `tier`      | string              | `"T1"`, `"T2"`, `"T3"`, or `"T4"`.                                                        |
| `id`        | integer \| null     | The T1 checklist item number (1-10), or `null` for a T2-T4 ladder row.                   |
| `title`     | string              | The item's/tier's bold title from the doc.                                               |
| `partition` | string \| null      | `"analog"`/`"digital"` for a `mixed-signal` manifest's per-partition row, else `null`.    |
| `text`      | string \| null      | The item's body text (the matching column for a per-kind item, or the shared text).      |
| `notes`     | array\<string\>     | Additional kind-independent caveats the doc attaches to the item (e.g. item 5's spec-ratification note). |
| `status`    | string               | `"met"` or `"unmet"` — see above.                                                        |
| `citation`  | object \| null       | Present only when `status: "met"`: `{"file", "kind", "check_status", "content_hash", "exit_status"}`. |

## Envelope-aggregation JSON schema (the contract)

**JSON is the API.** Per the project's rules, **breaking (renaming,
removing, or retyping) a field is a breaking change**. New fields may be
added without breaking the contract. See
[`../json-contract.md`](../json-contract.md) for the envelope shared across
all `klt` commands (`schema_version`, error shape, exit codes).

```json
{
  "schema_version": 1,
  "status": "pass",
  "check_count": 2,
  "passed_count": 2,
  "failed_count": 0,
  "provenance_consistency": {
    "ok": true,
    "mismatches": []
  },
  "checks": [
    {
      "source": "drc.json",
      "kind": "drc",
      "status": "clean",
      "passed": true,
      "detail": {"file": "design.gds", "deck": "sky130", "violation_count": 0},
      "provenance": {"...": "the source envelope's own provenance block"}
    },
    {
      "source": "lvs.json",
      "kind": "lvs",
      "status": "match",
      "passed": true,
      "detail": {
        "layout": "design.spice", "reference": "golden.spice",
        "mismatch_count": 0,
        "counts": {"nets": {"...": "..."}, "devices": {"...": "..."}, "pins": {"...": "..."}}
      },
      "provenance": {"...": "the source envelope's own provenance block"}
    }
  ]
}
```

### Top-level fields

| Field                     | Type            | Description                                                                          |
| -------------------------- | --------------- | --------------------------------------------------------------------------------------- |
| `schema_version`           | integer          | Version of this command's own JSON shape (starts at `1`).                              |
| `status`                   | string           | `"pass"`, `"fail"`, or `"refused"` — see "What it does" above.                          |
| `check_count`               | integer          | Number of input files given on the command line (`len(<file>...)`).                    |
| `passed_count`              | integer          | Number of `checks[]` entries with `passed: true`.                                       |
| `failed_count`              | integer          | Number of `checks[]` entries with `passed: false`.                                      |
| `provenance_consistency`    | object           | `{"ok": bool, "mismatches": [...]}` — see below.                                        |
| `checks`                    | array\<object\>  | One entry per input file, in the order given.                                           |

### `provenance_consistency.mismatches[]` entries

| Field    | Type              | Description                                                                            |
| -------- | ------------------ | ------------------------------------------------------------------------------------------ |
| `field`  | string              | `"pdk.name"`, `"pdk.version"`, `"input.content_hash"`, or `"deck[<name>].content_hash"`.  |
| `values` | array\<object\>     | `{"source": <str>, "value": <str>}` for every check that populated this field, in file order. |

Empty (`ok: true`, `mismatches: []`) when every input's provenance agrees,
or no two inputs share a comparable field at all (e.g. a single-input run).

### `checks[]` entries

| Field         | Type              | Description                                                                          |
| ------------- | ------------------ | ---------------------------------------------------------------------------------------- |
| `source`      | string              | The input file path (or `"-"`) this check was read from, exactly as given.               |
| `kind`        | string              | `"drc"`, `"lvs"`, `"extract"`, `"sim"`, or `"error"` — see "What it does" above.          |
| `status`      | string \| null      | The source envelope's own `status` field, or `"error"` for an `error`-kind check.         |
| `passed`      | boolean             | Whether this check counts toward `passed_count`/`failed_count` — see "What it does".      |
| `detail`      | object              | A small, kind-specific excerpt of the source envelope (not the full `violations[]`/`mismatches[]`/`devices[]`/`corners[]` detail — read the original file for that). |
| `provenance`  | object \| null      | The source envelope's own `provenance` block, echoed verbatim (`null` for an `error`-kind check, which carries none). |

## Exit codes and errors

Envelope-aggregation mode:

| Exit code | Meaning                                                                 |
| --------- | ------------------------------------------------------------------------ |
| `0`       | `status: "pass"` — every check passed and provenance was consistent.     |
| `1`       | An input file was missing/unreadable/not valid JSON, was not a JSON object, did not match a recognized `klt drc`/`lvs`/`extract`/`sim`/error envelope shape, neither `<file>...` nor `--manifest` was given, or both were given together. |
| `2`       | Usage error (bad `--format` value) — from argparse.                      |
| `3`       | `status: "fail"` — provenance was consistent, but at least one check did not pass. |
| `4`       | `status: "refused"` — two or more inputs' provenance blocks disagree; no pass/fail verdict was produced. |

Tier-verdict report mode (`--manifest`):

| Exit code | Meaning                                                                 |
| --------- | ------------------------------------------------------------------------ |
| `0`       | `tier: "T1"` — every rendered T1 item is `"met"`.                        |
| `1`       | The manifest file was missing/unreadable/not valid JSON/not a JSON object, its `kind` was missing or invalid, its `evidence` field was not a JSON object, `docs/design-evidence-tiers.md` could not be parsed, or `--manifest` was combined with `<file>...`. |
| `2`       | Usage error (bad `--format` value) — from argparse.                      |
| `3`       | `tier: null` — ran successfully, but at least one T1 item is `"unmet"`.  |

On error, a concise message is written to **stderr** and nothing is written
to stdout. No Python traceback is printed.

- `--format text` (default): a plain-text line prefixed `klt signoff:`.
- `--format json`: the documented JSON error envelope (see
  [`../json-contract.md`](../json-contract.md)):

  ```json
  {
    "schema_version": 1,
    "error": {
      "command": "signoff",
      "message": "envelope 'bad.json' has an unrecognized shape (schema_version=1): not a klt drc/lvs/extract/sim success or error envelope -- klt signoff aggregates only those four verbs today (see docs/cli/signoff.md)"
    }
  }
  ```

## Worked example: refused on a stale pairing

```
$ klt drc design.gds --deck sky130 --format json > drc.json
$ klt extract design_v2.gds --deck sky130 --format json > extract.json   # a newer layout revision
$ klt signoff drc.json extract.json
status: refused
checks: 2/2 passed

provenance mismatches (refusing to aggregate):
  input.content_hash:
    drc.json: sha256:da6049448a5669dfb8f6a9af6e1394249b18cd451f42e9bcbb118bb69de4a3db
    extract.json: sha256:9f2c000000000000000000000000000000000000000000000000000000000

[PASS] drc      drc.json      status=clean
[PASS] extract  extract.json  status=extracted
```

Both individual checks pass — DRC is clean, extraction succeeded — but they
ran against two different layout revisions (`input.content_hash` disagrees),
so `klt signoff` refuses to report an overall `"pass"`.

## Worked example: tier-verdict report with one item met

```
$ klt drc design.gds --deck sky130 --format json > drc.json
$ cat manifest.json
{"block": "my-bandgap", "kind": "analog", "evidence": {"3": "drc.json"}}
$ klt signoff --manifest manifest.json
block: my-bandgap  kind: analog
tier: none
T1: 1/10 items met

[UNMET] T1 #1 Design sources
[UNMET] T1 #2 Layout
[MET  ] T1 #3 DRC clean
        cite: drc.json (kind=drc, status=clean, content_hash=sha256:..., exit_status=0)
[UNMET] T1 #4 LVS clean
...
[UNMET] T2 #- T2 — signoff-validated
[UNMET] T3 #- T3 — silicon-validated
[UNMET] T4 #- T4 — production-validated

source: docs/design-evidence-tiers.md
```

(`UNMET`/`MET` render in red/green respectively in a real terminal.) Only
item 3 has evidence in the manifest, so only item 3 is `"met"` — every
other item, including the whole T2-T4 ladder, renders `"unmet"` with no
fabricated citation. `klt signoff` exits `3` here (`tier: null`); it would
exit `0` only once every T1 item's manifest entry resolves to a passing,
fresh check.
