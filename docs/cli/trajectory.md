# `klt trajectory`

Render an **optimization trajectory log** — an append-only JSONL record of a
multi-candidate run — into (a) a markdown milestone table and (b) a
self-contained objective-vs-turn SVG plot, both suitable for embedding in a
block repo's README.

An optimization run is evidence in two halves. The *artifact* half — the
final layout and its `klt drc`/`klt lvs`/`klt sim` envelopes — proves what
the winning design measures. The *trajectory* half — how many candidates
were evaluated, what was tried and abandoned, and where the real wins came
from — proves the run did not just plateau after the low-hanging fruit. For
a canary repo whose entire purpose is public dogfood evidence, the
trajectory is arguably the more interesting half, and it is the thing that
makes a run reproducible-in-spirit by someone who cannot rerun the compute
(see #388).

```
klt trajectory <log> [--threshold FLOAT] [--plot PATH] [--format text|json]
```

- `<log>` — path to a JSONL trajectory log: one JSON object per line, each a
  trajectory record (schema below). Blank lines are ignored. Records are
  read in any order and sorted by `turn` ascending before rendering, so a
  parallel loop that appends turns as they finish still renders a monotonic
  trajectory.
- `--threshold` — the objective-unit improvement a record must beat the
  best-prior record by, *strictly*, to count as a milestone (default `0.0`,
  so any strict improvement is a milestone). See "Milestone detection".
- `--plot PATH` — also write the objective-vs-turn plot as a standalone SVG
  file to `PATH` (for committing into a README). Omit to only receive the
  SVG inline in the `--format json` payload's `plot_svg` field.
- `--format` — `text` (default, prints the markdown milestone table) or
  `json` (this command's own JSON envelope, see below).

## Why a standalone verb (not `klt report`)

[`klt report`](report.md) renders one or more *single-object* JSON envelopes
into a combined report, detecting each envelope's kind from its own top-level
shape. A trajectory log is a different input **format** entirely — an
append-only JSONL stream of many records, not one envelope — and this verb's
job is analytic (milestone detection *across* records) plus dual-artifact (a
markdown table **and** an SVG plot), neither of which fits `report`'s
"classify one envelope, render one section" model. Folding a JSONL-log kind
into `report` would overload it with a second input grammar and a plot
generator it otherwise has no reason to own. The record schema still mirrors
the `klt eval` envelope's `objective`/`gate_results` shape (#387), so the two
verbs stay vocabulary-compatible even though they are separate.

## The record schema

Each line of the log is one JSON object:

```json
{
  "schema_version": 1,
  "turn": 22,
  "candidate_ref": "cand/turn-22.gds",
  "objective": {"name": "gates", "value": 2010, "polarity": "minimize"},
  "gate_results": [{"check": "drc", "status": "clean"}, {"check": "lvs", "status": "match"}],
  "wall_clock_s": 9.8,
  "description": "hardware modulo divider replaced with iterative shift-subtract"
}
```

| Field            | Type                | Required | Description                                                                                     |
| ---------------- | ------------------- | -------- | ----------------------------------------------------------------------------------------------- |
| `schema_version` | integer             | no       | The record schema version (`1`). Recommended on every record; the renderer does not require it. |
| `turn`           | integer             | **yes**  | The evaluation/iteration index. Records are sorted by this ascending.                            |
| `candidate_ref`  | string              | **yes**  | A path or identifier for the candidate — **not** the inline artifact. See "References, not inline artifacts". |
| `objective`      | object              | **yes**  | `{name, value, polarity}` — mirrors the `klt eval` envelope's `objective` (#387). `polarity` is `"minimize"` or `"maximize"`. |
| `gate_results`   | array\<object\>     | no       | A `[{"check": ..., "status": ...}, ...]` list — the same shape a single scored gate emits. A *summary*, not the full DRC/LVS report. |
| `wall_clock_s`   | number              | no       | Wall-clock seconds this evaluation took.                                                         |
| `description`    | string              | no       | A human note used as the milestone table's "what changed" column.                               |

Every record in a log must share one objective `name` and `polarity` — a log
that mixes objectives is a malformed log (exit code 1).

### References, not inline artifacts

A 500-turn run with full gate output per turn is not small. A record carries
`candidate_ref` (a path/identifier) and a *summary* of that turn's gate
results, never the layout snapshot or the full DRC report. Downstream reads
the referenced file if it wants the detail. The record also stays
**candidate-shape agnostic**: `candidate_ref` is whatever the driving loop
points at — a GDS/OASIS path today, a synthesized netlist + P&R result later
(#391 Phase 5) — so a future digital flow reuses this log format unchanged.

### No live optimizer required

The renderer operates purely on the JSONL file on disk. A hand-written or
human-curated log renders identically to a machine-emitted one — the log
format and renderer are useful even when a human is the one proposing
candidates, and this is a hard requirement of the feature (see #388's
"Notes"), exercised directly by the test suite.

## Building a record from `klt eval` (issue #437)

`klayout_tools.trajectory.record_from_eval(report, *, turn, candidate_ref,
description=None, wall_clock_s=None)` builds one record directly from a
single [`klt eval`](eval.md) envelope (#387) — the concrete form of "the
record schema mirrors the `klt eval` envelope shape" above: `report`'s own
`objective` is reused verbatim, and its `gates[]` collapses to this record
schema's lighter `gate_results` (`{"check", "status"}` per entry, dropping
`exit_code`/`count`/`name`). `klayout_tools.trajectory.append_record(path,
record)` then appends the built record to a JSONL log, creating the file
(and any missing parent directories) on first write.

Both functions are check-name-agnostic — they work identically for an
analog `klt eval` envelope (`drc`/`lvs`/`sim`/`layout-metrics` gates) or a
digital one (`synthesize`/`functional-verification`/`place-and-route`
gates, Epic #391 Phase 5), since they only ever read `report["objective"]`/
`report["gates"]`. `klt eval`'s own `--trajectory-log`/`--turn`/
`--candidate-ref`/`--description`/`--wall-clock-s` flags (see
[`docs/cli/eval.md`](eval.md#trajectory-logging)) call exactly this pair, so
a single `klt eval` invocation can score a candidate *and* log the turn.

## Milestone detection

The **first** record establishes the baseline (there is no prior record to
improve on, so it is never itself a milestone). Each later record is a
**milestone** when its objective improves on the *best objective value seen
in any prior record* by strictly more than `--threshold`:

- `minimize` — the improvement is `best_prior - value` (lower is better).
- `maximize` — the improvement is `value - best_prior` (higher is better).

The comparison is **strict** (`> threshold`): an improvement of exactly the
threshold is deliberately *not* a milestone, so the boundary is unambiguous.
The running best is updated on every strict improvement (threshold or not),
so a milestone's `objective_before` is always the genuine best-so-far it
beat, not merely the previous milestone's value.

## JSON schema (the contract)

**JSON is the API.** Per the project's rules, breaking (renaming, removing,
or retyping) a field is a breaking change; new fields may be added without a
`schema_version` bump. See [`../json-contract.md`](../json-contract.md) for
the shared envelope.

```json
{
  "schema_version": 1,
  "source": "run.jsonl",
  "record_count": 4,
  "objective_name": "gates",
  "polarity": "minimize",
  "threshold": 0.0,
  "baseline": {"turn": 0, "candidate_ref": "cand/turn-0.gds", "objective": 8298},
  "best": {"turn": 113, "candidate_ref": "cand/turn-113.gds", "objective": 907},
  "milestone_count": 3,
  "milestones": [
    {
      "turn": 22,
      "prior_turn": 0,
      "candidate_ref": "cand/turn-22.gds",
      "objective_before": 8298,
      "objective_after": 2010,
      "delta": 6288,
      "description": "hardware modulo divider replaced with iterative shift-subtract"
    }
  ],
  "markdown": "### Optimization milestones (gates, minimize)\n\n| turns | gates | what changed |\n...",
  "plot_svg": "<svg xmlns=\"http://www.w3.org/2000/svg\" ...>...</svg>"
}
```

### Top-level fields

| Field             | Type            | Description                                                                       |
| ----------------- | --------------- | --------------------------------------------------------------------------------- |
| `schema_version`  | integer         | Version of this command's own JSON shape (starts at `1`).                          |
| `source`          | string          | The input log path, exactly as given.                                              |
| `record_count`    | integer         | Number of records read from the log.                                               |
| `objective_name`  | string          | The shared objective name across all records.                                      |
| `polarity`        | string          | `"minimize"` or `"maximize"`.                                                      |
| `threshold`       | number          | The `--threshold` used.                                                            |
| `baseline`        | object          | The first record's `{turn, candidate_ref, objective}`.                             |
| `best`            | object          | The best record's `{turn, candidate_ref, objective}`.                              |
| `milestone_count` | integer         | `len(milestones)`.                                                                 |
| `milestones`      | array\<object\> | One entry per milestone, in turn order — see below.                                |
| `markdown`        | string          | The milestone table as GitHub Flavored Markdown — identical to `--format text`'s stdout. |
| `plot_svg`        | string          | The self-contained objective-vs-turn SVG — identical to what `--plot` writes.      |

### `milestones[]` entries

| Field              | Type    | Description                                                              |
| ------------------ | ------- | ----------------------------------------------------------------------- |
| `turn`             | integer | This record's turn.                                                     |
| `prior_turn`       | integer | The turn that held the best value this milestone beat (its turn range's start). |
| `candidate_ref`    | string  | This record's candidate reference.                                      |
| `objective_before` | number  | The best-prior objective value this milestone beat.                     |
| `objective_after`  | number  | This record's objective value.                                          |
| `delta`            | number  | The signed improvement magnitude (always `> 0`).                        |
| `description`      | string  | This record's `description`, or `""`.                                   |

## The plot

`plot_svg` (and the file written by `--plot`) is a self-contained SVG — pure
string-built, deterministic, with **no plotting dependency** (no matplotlib,
no GUI), so it renders in CI and embeds directly in a GitHub README via
`<img src="trajectory.svg">` or by committing the file. Every record is a
point on the objective curve; milestone turns get a larger filled marker so
the plot lines up visually with the table's rows.

## Exit codes and errors

| Exit code | Meaning                                                                        |
| --------- | ------------------------------------------------------------------------------ |
| `0`       | Trajectory rendered successfully.                                              |
| `1`       | Log missing/unreadable, empty, held a malformed record, mixed objectives, or the `--plot` file could not be written. |
| `2`       | Usage error (no `<log>` given, bad `--format` value) — from argparse.          |

Like `klt report`, there is no additional exit code for "rendered but the
run regressed" — a trajectory's milestones are content this command reports,
not a verdict it re-derives an exit code around.

On error, a concise message is written to **stderr** and nothing to stdout.
No Python traceback is printed. Under `--format json` the error is the
documented JSON error envelope (see [`../json-contract.md`](../json-contract.md)):

```json
{
  "schema_version": 1,
  "error": {
    "command": "trajectory",
    "message": "trajectory log 'run.jsonl' contains no records (an empty log is not a valid trajectory)"
  }
}
```

## Publication note (canary Tier-2 sweep)

A trajectory log is **new published prose surface**. When a canary repo is
flipped to public at canary-flip time, its trajectory logs and the
`description` free-text they carry are published content and must be covered
by the **same Tier-2 audit sweep** as every other published surface (README
prose, block descriptions). A `description` field is author-written free
text — treat it as content requiring review before publication, not as
machine-derived data exempt from it. Do not commit a trajectory log to a
public canary repo without that audit.

## Worked example

```
$ klt trajectory run.jsonl --plot trajectory.svg
### Optimization milestones (gates, minimize)

| turns | gates | what changed |
| --- | --- | --- |
| 0-22 | 8298 -> 2010 | hardware modulo divider replaced with iterative shift-subtract |
| 22-48 | 2010 -> 1304 | bypassed REDUCE stage, merged reduction modules |
| 48-113 | 1304 -> 907 | pruned registers + FSM, early-exit for even operands |

plot written to trajectory.svg
```
