# `klt eval`

Score one candidate against a declared gate + objective — the primitive an
optimization/hill-climbing loop needs to compare two candidates without
fanning out four subprocesses and hand-rolling the scoring itself (issue
#387).

```
klt eval <descriptor> [--candidate KEY=VALUE ...] [--format text|json]
```

Today, an agent driving a sizing/layout optimization loop has `klt drc`,
`klt lvs`, `klt sim`, and `klt layout-metrics` — each headless, each JSON, but
each with its own exit-code vocabulary (`drc` 0/1/2/3, `sim` 0/1/2/3/4, `lvs`
0/1/2/3, `layout-metrics` 0/1/2 with no pass/fail concept of its own). Asking
"is this candidate better than the last one?" means reconciling all four by
hand, every time, in every block repo. `klt eval` is pure orchestration over
those same four verbs' own library entry points — it never re-implements
DRC/LVS/sim/metrics logic — reconciled into **one envelope**: a hard
`valid` gate plus a single scalar `objective` with a declared polarity.

- `<descriptor>` — a descriptor document (see "Descriptor" below), in any of
  three forms, mirroring `klt lvs`/`klt sim`'s request-argument convention:
  - a **path** to a descriptor JSON file, e.g. `klt eval eval.json`.
  - `-` to read the descriptor JSON document from **stdin**.
  - an **inline JSON object** string, e.g.
    `klt eval '{"checks": {...}, "gates": [...], "objective": {...}}'`. An
    existing file always wins first.
  - **Relative paths inside the descriptor** (a check's `file`/`request`/
    `block` argument) resolve against the **descriptor file's own
    directory** for the path form, but against the **current working
    directory** for the stdin/inline-JSON forms — same rule as `klt lvs`/
    `klt sim`.
- `--candidate KEY=VALUE` — fill a `{KEY}` placeholder in any check's
  argument values with `VALUE` (repeatable). Lets an optimizer loop swap
  just the candidate file between iterations without rewriting the whole
  descriptor, e.g. `--candidate layout=candidate-042.gds`. A placeholder
  with no matching `--candidate` value is a broken invocation (exit 1), never
  a silently-empty substitution.
- `--format` — `text` (default, a human-readable summary) or `json`.

## Descriptor

A descriptor names which checks exist, which of them gate validity, and
which single metric is the objective. **Which checks constitute the gate,
and what the objective is, are declared here — never hardcoded in `klt
eval` itself.**

```json
{
  "checks": {
    "drc": {"file": "{layout}", "deck": "sky130"},
    "lvs": {"request": "lvs_request.json"},
    "sim": {"request": "sim_request.json"},
    "layout-metrics": {
      "block": ".",
      "threshold": {"metric": "cell_count", "max": 500}
    }
  },
  "gates": ["drc", "lvs", "sim"],
  "objective": {
    "check": "layout-metrics",
    "metric": "cell_count",
    "polarity": "minimize"
  }
}
```

- `checks` — an object keyed by check name. Each name must be one this
  version of `klt eval` knows how to run (currently: `drc`, `lvs`, `sim`,
  `layout-metrics` — see "Supported checks" below); an unknown name is an
  application error (exit 1). Each entry's fields (other than `threshold`)
  are that check's own invocation arguments, forwarded to the matching
  library function — see the per-check table below for which fields it
  reads. A string value containing a `{key}` placeholder is filled in from
  `--candidate` before the check runs.
- `gates` — an array of `checks` names that must all report `status: "pass"`
  for the response's top-level `valid` to be `true`. A name not present in
  `checks`, or not in the supported set, is an application error (exit 1).
- `objective` — `{"check", "metric", "polarity"}`. `check` names a `checks`
  entry (does not have to also be a `gates` entry). `metric` is a key — or a
  dot-separated path, e.g. `"drc.violation_count"` — into that check's own
  report. `polarity` is `"minimize"` or `"maximize"`; any other value is an
  application error (exit 1).

A check named by both a `gates` entry and `objective.check` is only
invoked **once** — its report is cached and reused, so declaring the same
check for both purposes never doubles the run cost.

### Supported checks

| `checks` name    | Library function                            | Required fields         | Optional fields |
| ---------------- | -------------------------------------------- | ------------------------ | ---------------- |
| `drc`            | `klayout_tools.drc.run_drc`                  | `file`, `deck`            | — |
| `lvs`            | `klayout_tools.lvs.run_lvs`                  | `request`                 | — |
| `sim`            | `klayout_tools.sim.run_sim`                  | `request`                 | `backend`, `max_workers`, `hosts`, `artifacts_dir` |
| `layout-metrics` | `klayout_tools.layout_metrics.layout_metrics_report` | `block`            | `deck`, `threshold` (see below) |

Each entry's own `docs/cli/<verb>.md` documents its full request shape and
report fields — `klt eval` forwards exactly what that verb accepts and
returns exactly what it reports, in the response's `metrics` bag.

### `layout-metrics` has no native pass/fail — use `threshold`

`drc`/`lvs`/`sim` each have their own clean/dirty verdict (`status: "clean"`/
`"violations"`, `"match"`/`"mismatch"`, `"pass"`/`"fail"`/`"error"`) that
`klt eval` reuses directly. `layout-metrics` does not — it is metrics-only
(see its own exit-code table: 0/1/2, nothing above 2). A `gates` entry
naming `layout-metrics` therefore **requires** a `threshold` object in its
`checks` entry: `{"metric": <name>, "max": <number>}` and/or `{"min":
<number>}`. The gate fails when the named metric is greater than `max` and/
or less than `min`. A `layout-metrics` gate entry with no `threshold` is a
malformed descriptor (exit 1) — using `layout-metrics` only for the
`objective` (not listed in `gates`) needs no `threshold` at all.

## Response

```json
{
  "schema_version": 1,
  "valid": false,
  "gates": [
    {"check": "drc", "status": "fail", "exit_code": 3, "count": 3},
    {"check": "lvs", "status": "pass", "exit_code": 0, "count": 0}
  ],
  "objective": {
    "check": "layout-metrics",
    "name": "cell_count",
    "value": 42,
    "polarity": "minimize"
  },
  "metrics": {
    "drc": { "...": "the full klt drc report" },
    "lvs": { "...": "the full klt lvs report" },
    "layout-metrics": { "...": "the full klt layout-metrics report" }
  },
  "candidate": {"layout": "candidate-042.gds"}
}
```

| Field                | Type                     | Description |
| -------------------- | ------------------------ | ----------- |
| `valid`               | boolean                  | `true` only if every `gates[]` entry's `status` is `"pass"`. `false` means the objective must not be used to rank this candidate — fix correctness first. |
| `gates`               | array\<object\>          | One entry per `gates` name, in descriptor order. See below. |
| `objective`           | object                   | `{"check", "name", "value", "polarity"}`. `value` is `null` when the declared `metric` is absent from that check's report — this is reported, not an error, since the checks that *did* run are still trustworthy (see "Objective can be null" below). |
| `metrics`             | object\<string, object\> | Every invoked check's full, unmodified report, keyed by its `checks` name — the same data `metrics`/`objective` were derived from, so a caller never needs a second invocation for trajectory logging. |
| `candidate`           | object\<string, string\> | Echoes the `--candidate KEY=VALUE` flags given, verbatim. |

### `gates[]` entries

| Field       | Type            | Description |
| ----------- | --------------- | ----------- |
| `check`     | string          | The `checks` name (cites which underlying `klt` subcommand produced this entry). |
| `status`    | `"pass"` \| `"fail"` | The gate's verdict. |
| `exit_code` | integer \| null | The equivalent standalone exit code that check's own `klt <verb>` would report for this outcome (e.g. `0`/`3` for `drc`/`lvs`, `0`/`3`/`4` for `sim`) — traceable back to that verb's own exit-code table. `null` for a `threshold`-derived gate (`layout-metrics`), which has no native exit code to cite. |
| `count`     | integer         | A generic "problem count" for this check: `drc`'s `violation_count`, `lvs`'s `mismatch_count`, `sim`'s `failed + errored` corner count, or `1`/`0` for a `threshold`-derived pass/fail. |

### Objective can be `null`

If the `objective.metric` path is absent from its check's report (a typo,
or a metric only present under some conditions — e.g. `layout-metrics`'s
`drc` sub-block, only present when that check itself declared a `deck`),
`objective.value` is `null`. This is **not** the same as `valid: false` — the
named check still ran and reported successfully; the descriptor is just
asking for a number that particular report doesn't have. Fix the `metric`
path, not the candidate.

## Exit codes

| Code | Meaning                                                                 |
| ---- | ------------------------------------------------------------------------ |
| `0`  | Ran, `valid: true` — every declared gate passed.                        |
| `1`  | Failed to run at all — malformed/unreadable descriptor, unknown check name, an unresolved `--candidate` placeholder, or any named check's own library function raising its own error (e.g. `DrcError`, `LvsError`, `SimError`, `LayoutMetricsError` — an unresolvable file, unknown deck, engine failure). |
| `2`  | Usage error (missing argument, bad `--format` value) — from argparse.    |
| `3`  | Ran successfully, `valid: false` — one or more declared gates failed.    |

This mirrors `klt drc`/`klt lvs`'s own 0/1/2/3 split (a clean/dirty verdict,
not `klt sim`'s extra "corner errored" outcome) — `klt eval` has no fourth
outcome of its own, since a check that "errors" internally (e.g. a `sim`
corner) is still a **documented** `status: "fail"`/`"error"` inside that
check's own report, not a failure to run `klt eval` itself.

**The distinction between exit `1` and exit `3` is the entire point of this
verb** (issue #387's own framing): an optimizer must never read a crash (exit
1, "the tool itself failed") as a bad score (exit 3, "the tool ran and says
this candidate is invalid") — conflating the two would poison a hill-climbing
loop with phantom "very bad" candidates that were never actually evaluated.

On error (exit 1), a concise message is written to **stderr** and nothing is
written to stdout, per `docs/json-contract.md`'s shared error shape. No
Python traceback is ever printed.

## Worked example

```bash
$ cat eval.json
{
  "checks": {
    "drc": {"file": "candidate.gds", "deck": "sky130"},
    "layout-metrics": {"block": "."}
  },
  "gates": ["drc"],
  "objective": {"check": "layout-metrics", "metric": "cell_count", "polarity": "minimize"}
}

$ klt eval eval.json --format json
{
  "schema_version": 1,
  "valid": true,
  "gates": [
    {"check": "drc", "status": "pass", "exit_code": 0, "count": 0}
  ],
  "objective": {
    "check": "layout-metrics",
    "name": "cell_count",
    "value": 12,
    "polarity": "minimize"
  },
  "metrics": { "...": "..." },
  "candidate": {}
}
$ echo $?
0
```

An optimizer loop swaps the candidate file per iteration without touching
`eval.json`:

```bash
$ klt eval eval.json --candidate layout=candidate-002.gds --format json
```

(paired with a descriptor whose `drc.file` is written as `"{layout}"`).

## Reuse, not reimplementation

`klt eval` never reimplements `drc`/`lvs`/`sim`/`layout-metrics` logic — it
calls each verb's own library entry point
(`klayout_tools.drc.run_drc`, `klayout_tools.lvs.run_lvs`,
`klayout_tools.sim.run_sim`,
`klayout_tools.layout_metrics.layout_metrics_report`) and reconciles their
already-documented outcomes. Every field in `metrics[<check>]` is exactly
what that verb's own `docs/cli/<verb>.md` documents — nothing is recomputed
or renamed.

## Future check kinds

The `checks`/`gates`/`objective` shape above is intentionally
check-name-agnostic: adding a check kind this version of `klt eval` doesn't
know about yet (e.g. a future synthesis, place-and-route, or functional-
verification check — see #391/#398) is a registry addition in
`klayout_tools/eval.py`, not an envelope change. The response shape
(`gates[].check`/`status`/`exit_code`/`count`, `objective`) already
generalizes to any check name a future descriptor names.
