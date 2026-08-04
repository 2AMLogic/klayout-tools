# `klt eval`

Score a candidate against a per-block gate/objective descriptor in **one**
invocation — the loop-closing "scorer" issue #387 adds between `klt drc`/`klt
lvs`/`klt sim`/`klt layout-metrics` (each with its own JSON envelope and exit
code vocabulary) and an agent that wants to hill-climb a design across many
turns without hand-rolling that reconciliation itself.

```
klt eval <descriptor> [--candidate <candidate>] [--format text|json]
```

- `<descriptor>` — a descriptor document (see "Descriptor" below), in any of
  three forms, the same path-or-inline convention `klt lvs`'s `request`
  argument uses:
  - a **path** to a descriptor JSON file, e.g. `klt eval descriptor.json`.
  - `-` to read the descriptor JSON document from **stdin**.
  - an **inline JSON object** string, e.g. `klt eval '{"gates": [...], ...}'`.
- `--candidate` — optional candidate substitution values (see "Candidate"
  below), same three forms. Omit when the descriptor's `args` need no
  `${name}` substitution.
- `--format` — `text` (default, a human-readable summary) or `json`.

`klt eval` is **pure orchestration**: it imports and calls the existing
library entry points behind `klt drc`/`klt lvs`/`klt sim`/`klt
layout-metrics`/`klt functional-verification`/`klt synthesize`/`klt
place-and-route` (`run_drc`/`run_lvs`/`run_sim`/`layout_metrics_report`/
`run_functional_verification`/`run_synthesize`/`run_place_and_route`) —
never re-implements DRC/LVS/sim/metrics/synthesis/place-and-route logic
itself, per this repo's "wrap the proven engine" convention.

## Why: one envelope instead of four

An agent wanting to answer "is this candidate better than the last one?"
otherwise has to fan out four subprocesses, reconcile four separate exit-code
vocabularies (`drc` 0/1/2/3, `sim` 0/1/2/3/4, `lvs` 0/1/2/3,
`layout-metrics` 0/1/2 with no pass/fail concept of its own), and hand-roll
the scoring — glue every block repo would otherwise write again, slightly
differently. `klt eval` returns one envelope: a hard `valid` gate that can't
be argued with, plus a single scalar `objective` with a declared polarity so
two candidates can be compared without domain knowledge.

## Descriptor-driven, never hardcoded

Which checks constitute the gate, and what the objective is, are declared in
the **descriptor** — never a fixed check list built into `klt eval` itself.
Seven checks are implemented today (`drc`/`lvs`/`sim`/`layout-metrics`/
`functional-verification`/`synthesize`/`place-and-route`), and the design is
check-name-agnostic by construction: adding the digital flow's
[`functional-verification`](functional-verification.md) gate (Epic #391
Phase 3) and its [`synthesize`](synthesize.md)/
[`place-and-route`](place-and-route.md) checks (Epic #391 Phase 5) each
needed no schema change here, only a new invoke adapter (plus, for
`functional-verification`, a status adapter — `synthesize`/`place-and-route`
have no gate semantics of their own, see "`gates[]` entries" below).

A digital candidate's descriptor chains the same four gate/objective/metrics
fields an analog descriptor uses, just with digital `check` names:
`synthesize` → `functional-verification` → `place-and-route` → `drc`/
`layout-metrics` (the last two running over the GDS `place-and-route`
produces). This is what lets a digital candidate evaluation produce the
same one `valid`/`objective`/`metrics` envelope, and the same trajectory-log
record shape (see [`klt trajectory`](trajectory.md)), that an analog
candidate already does.

## Descriptor

```json
{
  "gates": [
    { "check": "drc", "args": { "file": "${layout}", "deck": "sky130" } },
    { "check": "lvs", "args": { "request": "lvs_request.json" } },
    {
      "check": "layout-metrics",
      "args": { "block": "${block}" },
      "threshold": { "metric": "cell_count", "max": 500 }
    }
  ],
  "objective": {
    "check": "layout-metrics",
    "metric": "cell_count",
    "polarity": "minimize",
    "name": "cell_count",
    "args": { "block": "${block}" }
  },
  "metrics": [
    {
      "name": "layout_metrics",
      "check": "layout-metrics",
      "args": { "block": "${block}" }
    }
  ]
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `gates` | array\<object\>, required, non-empty | One entry per gate check — see "`gates[]` entries" below. `valid` is `true` only when every gate's `status` is `"pass"`. |
| `objective` | object, required | The single scalar to report — see "`objective`" below. |
| `metrics` | array\<object\> | Optional additional checks to run and report in the response's `metrics` bag, beyond the declared objective — see "`metrics[]` entries" below. Defaults to `[]`. |

### Digital flow example

A digital candidate's descriptor (Epic #391 Phase 5) chains `synthesize` →
`functional-verification` → `place-and-route` → `drc`/`layout-metrics` over
the P&R-produced GDS — the same descriptor shape above, just with digital
`check` names strung together:

```json
{
  "gates": [
    { "check": "synthesize", "args": { "request": "synth_request.json" } },
    {
      "check": "functional-verification",
      "args": { "request": "fv_request.json" }
    },
    {
      "check": "place-and-route",
      "args": { "request": "pnr_request.json" },
      "threshold": { "metric": "setup_violation_count", "max": 0 }
    },
    {
      "check": "drc",
      "args": { "file": ".klt/place-and-route/gcd.gds", "deck": "sky130hd" }
    }
  ],
  "objective": {
    "check": "place-and-route",
    "metric": "wirelength_um",
    "polarity": "minimize",
    "name": "wirelength_um",
    "args": { "request": "pnr_request.json" }
  },
  "metrics": [
    {
      "name": "synth",
      "check": "synthesize",
      "args": { "request": "synth_request.json" }
    }
  ]
}
```

`synthesize`'s/`place-and-route`'s own `request` args are file paths only
(never inline JSON/`-`), and each request document's own `hdl_toplevel`
governs where its `.klt/synthesize/`/`.klt/place-and-route/` artifacts land
— so `drc`'s `file` above can name that deterministic output path directly,
without candidate substitution, as long as the candidate's `hdl_toplevel`
stays fixed across turns (only the RTL/netlist inputs a request names need
to vary per candidate). See `docs/cli/synthesize.md`/
`docs/cli/place-and-route.md` for each request document's own schema.

### `gates[]` entries

| Field | Type | Description |
| ----- | ---- | ----------- |
| `check` | string, required | Which `klt` subcommand to run: `"drc"`, `"lvs"`, `"sim"`, `"layout-metrics"`, `"functional-verification"`, `"synthesize"`, or `"place-and-route"`. An unknown value is an application error (exit 1). |
| `name` | string | Label for this gate in the response's `gates[].name` — disambiguates two gates of the same `check` (e.g. two DRC decks). Defaults to `check`. |
| `args` | object | Arguments forwarded to the underlying check — see "Check `args`" below. `${name}`-style placeholders are substituted from `--candidate` before invocation. |
| `threshold` | object | Overrides this gate's pass/fail derivation: `{"metric": <dotted path>, "min": <number>, "max": <number>, "equals": <any>}`, at least one of `min`/`max`/`equals`. **Required** for `"layout-metrics"`/`"synthesize"`/`"place-and-route"` gates (none has an exit code above 2 / a "ran but found a problem" outcome of its own — `synthesize`/`place-and-route` always report `"status": "ok"`, see [`klt synthesize`](synthesize.md)/[`klt place-and-route`](place-and-route.md) — so there is no default status to derive); optional for `"drc"`/`"lvs"`/`"sim"`/`"functional-verification"` gates (e.g. gate on a violation-count ceiling instead of "any violation fails", or gate `"place-and-route"` on `worst_slack_ns` >= `0` for timing closure). |

### Check `args`

| `check` | Required `args` | Notes |
| ------- | ---------------- | ----- |
| `"drc"` | `file`, `deck` | Forwarded to `run_drc(file, deck)` — see [`klt drc`](drc.md). `file` resolves relative to the descriptor file's own directory when not absolute (mirrors `klt lvs`'s request-relative-path convention). |
| `"lvs"` | `request` | Forwarded to `run_lvs(request)` — see [`klt lvs`](lvs.md). A string resolves the same file/`-`/inline-JSON three-form convention `klt lvs`'s own `request` CLI argument uses (relative paths resolve against the descriptor file's directory); an inline JSON object is serialised and passed through directly (its own internal relative paths resolve against the current working directory, matching `klt lvs -`'s convention). |
| `"sim"` | `request` | Forwarded to `run_sim(request, ...)` — see [`klt sim`](sim.md). Same `request` resolution as `"lvs"` above. Optional `artifacts_dir`, `backend`, `max_workers`, `hosts` forward to `run_sim`'s matching keyword arguments. |
| `"layout-metrics"` | `block` | Forwarded to `layout_metrics_report(block, deck=...)` — see [`klt layout-metrics`](layout-metrics.md). `block` resolves the same way as `"drc"`'s `file`. Optional `deck` forwards to the DRC-violation-count sub-field. |
| `"functional-verification"` | `request` | Forwarded to `run_functional_verification(request)` — see [`klt functional-verification`](functional-verification.md). Same `request` resolution as `"lvs"` above. `status: "pass"` → `valid: true`, `status: "fail"` → `valid: false`; a run that never produced a `results.xml` is a `klt eval` error (exit 1), never a `false` score. |
| `"synthesize"` | `request` | Forwarded to `run_synthesize(request)` — see [`klt synthesize`](synthesize.md). **Unlike** `"lvs"`/`"sim"`/`"functional-verification"`, `request` resolves the same way as `"drc"`'s `file` (a path only — `run_synthesize`'s own `load_request` does not accept the `"-"`/inline-JSON forms). Reports `instance_count`, `area_um2`, `sequential_area_um2`, `netlist_path`, etc.; always `"status": "ok"` (synthesis either produces a netlist or the check itself fails to run — exit 1). |
| `"place-and-route"` | `request` | Forwarded to `run_place_and_route(request)` — see [`klt place-and-route`](place-and-route.md). Same file-path-only `request` resolution as `"synthesize"` above. Reports `wirelength_um`, `worst_slack_ns`, `total_negative_slack_ns`, `setup_violation_count`/`hold_violation_count`, `gds_path`, etc.; always `"status": "ok"` (same "produces or fails to run" posture as `"synthesize"`). A descriptor chaining `"drc"`/`"layout-metrics"` over the produced `gds_path` closes the loop from RTL candidate to a signed-off digital layout in one `klt eval` invocation. |

### `objective`

| Field | Type | Description |
| ----- | ---- | ----------- |
| `check` | string, required | Same vocabulary as a gate's `check`. May repeat a gate's check+args (cached — run once, reused for both). |
| `metric` | string, required | A dotted path into that check's report (e.g. `"cell_count"`, `"measurements.0.worst_case.value"`) naming the scalar to report. |
| `polarity` | `"minimize"` \| `"maximize"` | Which direction is better. Defaults to `"minimize"`. |
| `name` | string | The response's `objective.name`. Defaults to `metric`. **Opaque per-descriptor string for now** — soft-dependency on issue #247's (not-yet-landed) metric namespace; do not read semantics into it beyond what the descriptor author intends. |
| `args` | object | Same substitution/resolution convention as a gate's `args`. |

### `metrics[]` entries

Same shape as a gate entry, minus `threshold`, plus an optional `metric`:

| Field | Type | Description |
| ----- | ---- | ----------- |
| `check` | string, required | Same vocabulary as a gate's `check`. |
| `name` | string | Key under the response's `metrics` object. Defaults to `check`. |
| `args` | object | Same substitution/resolution convention as a gate's `args`. |
| `metric` | string | When given, a dotted path extracting one scalar (same as `objective.metric`) into `metrics[name]`. When omitted, the **entire** report from that check is attached under `metrics[name]` — useful for carrying a check's full detail (e.g. `layout-metrics`' whole metric set) for downstream trajectory logging (issue #388) without a second invocation. |

## Candidate

A small JSON object of `${name}` substitution values, applied (via
`string.Template`) to every string leaf of every gate's/`objective`'s/
`metrics[]` entry's `args` before that check is invoked:

```json
{ "layout": "candidates/turn-042/layout.gds", "block": "candidates/turn-042" }
```

This is what lets one descriptor — the fixed gate/objective composition for a
block — be reused unmodified across hundreds of optimizer turns, while only
the candidate's paths change each turn. A descriptor key referencing a
candidate name that `--candidate` did not provide is an application error
(exit 1): **an unresolvable candidate path must fail loudly, not silently
pass a literal `"${name}"` string to the underlying check.**

## Response

```json
{
  "schema_version": 1,
  "valid": false,
  "gates": [
    { "check": "drc", "name": "drc", "status": "fail", "exit_code": 3, "count": 3 }
  ],
  "objective": { "name": "cell_count", "value": 1240, "polarity": "minimize" },
  "metrics": {}
}
```

### Top-level fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema_version` | integer | Version of this command's JSON shape (starts at `1`; per-command, per [`docs/json-contract.md`](../json-contract.md)). |
| `valid` | boolean | `true` only when every `gates[]` entry's `status` is `"pass"`. `objective` is still reported when `false` (it is still computed) — but a `false` `valid` means the objective must not be used to rank candidates; fix correctness first. |
| `gates` | array\<object\> | One entry per descriptor `gates[]` entry, in declared order — see "`gates[]` entries" below. |
| `objective` | object | `{"name", "value", "polarity"}` — see "`objective`" above for field semantics. |
| `metrics` | object\<string, any\> | Free-form bag keyed by each descriptor `metrics[]` entry's `name` — empty (`{}`) when the descriptor declares no `metrics`. |

### `gates[]` entries (response)

| Field | Type | Description |
| ----- | ---- | ----------- |
| `check` | string | Which underlying `klt` subcommand produced this gate — cites the check for debuggability, even when `name` differs. |
| `name` | string | This gate's declared (or default) name. |
| `status` | `"pass"` \| `"fail"` | This gate's verdict. |
| `exit_code` | integer | The exit code the cited `check` would itself have returned for this report (e.g. `3` for a `drc` gate with violations) — `0` for a threshold-derived gate (the underlying check itself ran and reported successfully; the threshold comparison, not that check's own exit-code vocabulary, produced `status`). Lets a human debugging a `valid: false` run trace it back to the specific `klt <check>` invocation and outcome. |
| `count` | integer \| number | Present when the underlying check (or threshold) has a natural headline count/value: `violation_count` for `drc`, `mismatch_count` for `lvs`, failed/errored corner count for `sim`, the threshold's extracted metric value for a threshold-derived gate. Absent otherwise. |

## Exit codes

| Code | Meaning |
| ---- | ------- |
| `0` | Ran, `valid: true`. |
| `1` | Failed to run at all — bad/missing descriptor or candidate, an unknown `check` name, a descriptor referencing a candidate key `--candidate` did not provide (unresolvable candidate path), or an underlying `klt` subcommand's own "failed to run" error (bad file, unknown deck, engine error, ...). |
| `2` | Usage error (missing argument, bad `--format` value) — from argparse. |
| `3` | Ran successfully; `valid: false` (at least one gate's `status` is `"fail"`); the documented payload is still on stdout. |

This mirrors `klt drc`/`klt lvs`'s `0`/`1`/`2`/`3` split. **The distinction
between `1` and `3` is the whole point of this command for an unattended
optimizer loop**: exit `1` means the run produced no trustworthy score at
all (a crash — the candidate/descriptor itself could not be evaluated), while
exit `3` means the run completed and produced a real, if unfavourable,
verdict. An optimizer must never read a crash (`1`) as a bad score (`3`) —
doing so would let it "improve" a broken harness by making it crash less
informatively, or conversely discard a valid low-scoring candidate as a tooling
failure.

On error (exit `1`), a concise message is written to **stderr** and nothing
is written to stdout. No Python traceback is printed.

- `--format text` (default): a plain-text line prefixed `klt eval:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "eval", "message": "descriptor references candidate key 'layout' that 'klt eval' was not given -- unresolvable candidate path" } }
  ```

## Out of scope

A sizing-candidate proposer (issue #310) and a signoff-aggregation tool
(issue #309) are explicitly separate, adjacent pieces — `klt eval` is the
scorer that sits between them, not either one. `objective.name` is not yet
tied to issue #247's (unlanded) metric namespace; treat it as an opaque
per-descriptor string until that namespace lands.
