# `klt signoff`

Aggregate one or more `klt drc`/`klt lvs`/`klt extract`/`klt sim` JSON
envelopes into a single pass/fail signoff verdict — the mechanical piece
tracked by issue #309 that
[`.claude/skills/design-signoff/SKILL.md`](../../.claude/skills/design-signoff/SKILL.md)
hand-assembled before this verb existed.

```
klt signoff <file>... [--format text|json]
```

- `<file>...` — one or more paths to `klt drc`/`klt lvs`/`klt extract`/`klt
  sim` JSON envelope files (`--format json` output from any of those four
  verbs), in the order they should appear in `checks[]`. Any entry may be
  `-`, which reads one envelope from stdin (same convention as `klt
  report`/`klt lvs`).
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
map), so there is nothing to diff against mechanically today — and it does
not walk the full T1 checklist in
[`../design-evidence-tiers.md`](../design-evidence-tiers.md) (design-source
commit hygiene, README/license/CI checks, testbench-shipped checks — items
that have no `klt` verb JSON to read at all). The `design-signoff` skill
still drives the full T1 qualification report; `klt signoff` is the
mechanical building block underneath the checklist items ("DRC clean",
"LVS clean", "post-layout verification") that this verb *can* check today,
and refuses to guess at the ones it can't (a spec-diff, or a
design-hygiene check) rather than fabricate a verdict for them.

## JSON schema (the contract)

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

| Exit code | Meaning                                                                 |
| --------- | ------------------------------------------------------------------------ |
| `0`       | `status: "pass"` — every check passed and provenance was consistent.     |
| `1`       | An input file was missing/unreadable/not valid JSON, was not a JSON object, or did not match a recognized `klt drc`/`lvs`/`extract`/`sim`/error envelope shape. |
| `2`       | Usage error (no `<file>` given, bad `--format` value) — from argparse.    |
| `3`       | `status: "fail"` — provenance was consistent, but at least one check did not pass. |
| `4`       | `status: "refused"` — two or more inputs' provenance blocks disagree; no pass/fail verdict was produced. |

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
