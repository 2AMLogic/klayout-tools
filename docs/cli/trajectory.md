# `klt trajectory`

Render an append-only **JSONL optimization trajectory log** — one record
per candidate evaluation — into a derived milestone table and an
objective-vs-turn plot.

Our other evidence is artifact-shaped: a `klt drc`/`klt sim` envelope
pinned to a commit, proving what the *final* design measures. It says
nothing about how the design got there — how many candidates were
evaluated, what was tried and abandoned, and where the real wins came
from. For a long multi-candidate run, that path is arguably the more
interesting half of the evidence, and it is the part that makes a result
reproducible-in-spirit by someone who cannot rerun the compute. A
trajectory log records it; this verb turns it into something a reader can
agree or disagree with:

| turns | gate_count (gates) | what changed |
| --- | --- | --- |
| 1-22 | 8,298 -> 2,010 | hardware modulo divider replaced with iterative shift-subtract |
| 22-48 | 2,010 -> 1,304 | bypassed REDUCE stage, merged reduction modules |

That table is **derived from the log**, not hand-written — which is the
whole point. A hand-written milestone table is a claim; a derived one is a
consequence of records that also name the candidate artifact behind each
row.

```
klt trajectory <log.jsonl> [--threshold F] [--threshold-pct F]
                           [--plot PATH] [--format text|json|github-summary]
```

- `<log.jsonl>` — path to a JSONL trajectory log, or `-` to read the log
  from stdin (same as every other `klt` verb that accepts `-`).
- `--threshold` — minimum improvement, in the objective's own units, for a
  record to count as a milestone (default `0.0`, i.e. any strict
  improvement).
- `--threshold-pct` — minimum improvement as a percentage of the best prior
  value (default `0.0`). Applied **together with** `--threshold`: both must
  be exceeded.
- `--plot` — write an objective-vs-turn plot as a standalone SVG to this
  path. Omitted by default.
- `--format` — `text` (default), `json` (this command's own JSON envelope,
  see below), or `github-summary` (GitHub Flavored Markdown suitable for a
  block repo's README or `$GITHUB_STEP_SUMMARY`).

Like [`klt report`](report.md) and unlike every other verb, `--format` has a
**third** choice, `github-summary` — for the same reason: the rendering is
meant to be pasted somewhere else, and its markup is neither the plain
`text` courtesy rendering nor the machine-readable `json` contract.

## No optimizer required

`klt trajectory` never proposes, evaluates, or runs anything. It reads a
text file. A hand-written or human-curated log — a designer recording their
own iterations by hand — renders exactly like a machine-emitted one. The
log format plus this renderer are useful today, before any optimizer or
proposer exists; when one does, it emits the same records.

## The record schema

One JSON object per line, appended, never rewritten. Blank lines are
skipped; everything else must parse and validate.

```json
{
  "record_schema_version": 1,
  "turn": 22,
  "candidate_ref": "runs/t22/div.gds",
  "objective": {"name": "gate_count", "value": 2010, "polarity": "minimize", "unit": "gates"},
  "gate_results": [{"check": "drc", "status": "clean"}, {"check": "lvs", "status": "match"}],
  "eval_ref": "runs/t22/eval.json",
  "wall_clock_s": 41.2,
  "note": "hardware modulo divider replaced with iterative shift-subtract"
}
```

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `record_schema_version` | integer | no | Version of the **record** shape (starts at `1`, currently `1`). Defaults to `1` when absent, so a hand-written log need not carry it. A record from a *newer* schema is a hard error rather than a silent misread. |
| `turn` | integer >= 0 | yes | The evaluation's index in the run. Must not go backwards between successive lines (a log is append-only); ties are allowed, so a batch of candidates evaluated in parallel can share a turn. |
| `candidate_ref` | string | yes | A path or identifier pointing at the candidate that was evaluated — a GDS/OASIS path today, a synthesized netlist or P&R result later. Never the artifact's contents. |
| `objective.name` | string | yes | The scalar being optimized. Every record in one log must agree on it. |
| `objective.value` | number | yes | This candidate's objective value. Must be finite. |
| `objective.polarity` | `"minimize"` \| `"maximize"` | yes | Which direction counts as improvement. Every record in one log must agree on it. |
| `objective.unit` | string \| null | no | Display unit for the tables and plot (e.g. `"gates"`, `"um^2"`). |
| `gate_results` | array\<object\> \| null | no | Pass/fail checks scored on this candidate, as `{"check": ..., "status": ...}` entries. See "Gates" below. |
| `eval_ref` | string \| null | no | Path to the evaluation envelope that produced `gate_results`/`objective` (a `klt eval` envelope, once that verb exists). Carried through to the output but **never dereferenced** — see "References, not payloads". |
| `wall_clock_s` | number \| null | no | Seconds this evaluation took. Optional when reading (a hand-curated log may not know it); always written by the library's `make_record()`. |
| `note` | string \| null | no | Free text: what changed in this candidate. This is the milestone table's "what changed" cell. |

### References, not payloads

A record points at its candidate and its evaluation; it never embeds them.
A 500-turn run with full gate output per turn is not a small thing, and a
log that inlined it would be unreadable, un-diffable, and unusable as the
one artifact a reader skims. Full per-turn artifacts (gate output, layout
snapshots, raw simulator output) live wherever the run wrote them and are
reached through `candidate_ref`/`eval_ref`.

Consequently this verb performs **no I/O beyond reading the log itself**:
it does not open `candidate_ref`, and it does not open `eval_ref`. A
trajectory renders identically on a machine that no longer has the run's
artifacts.

### Gates

`gate_results` is a plain list of `{"check", "status"}` entries with no
hardcoded check-name enum anywhere — the same discipline `klt eval` (#387)
applies, so an analog run (`drc`/`lvs`/`sim`) and a future digital
synth+P&R run (#391) use one vocabulary. A record's gate status is reduced
to one of three values:

| Reduced status | When |
| --- | --- |
| `pass` | `gate_results` is present and every entry's `status` is one of `pass`, `passed`, `clean`, `match`, `ok` (case-insensitive) |
| `fail` | `gate_results` is present and any entry's status is anything else |
| `unknown` | `gate_results` is absent or empty |

A `fail` record is **ineligible**: it stays in the log and in the output
(an abandoned candidate is part of the evidence, and it is drawn on the
plot), but it can neither become the best-so-far nor be flagged a
milestone. An objective win that fails its checks is not a win. An
`unknown` record is treated as eligible — a hand-curated log is not obliged
to restate gates it never ran.

## Milestone detection

A record is flagged a **milestone** when it improves the objective by *more
than* both configured thresholds, relative to the **best prior eligible
record** (not the previous record, and not the baseline):

- improvement under `"minimize"` is `best_prior - value`; under
  `"maximize"` it is `value - best_prior`.
- `--threshold` compares that improvement in the objective's own units.
- `--threshold-pct` compares it as a percentage of `|best_prior|`.
- Both comparisons are **strict**. An improvement landing exactly on a
  threshold is not a milestone. (A zero best-prior value has no meaningful
  percentage; any positive improvement against it is treated as infinite,
  so a percentage threshold can never silently swallow it.)

The first eligible record is the **baseline**: with no prior record there
is no improvement to measure, so it is never itself a milestone — it is the
"before" value of the first milestone row.

Best-so-far tracking is independent of milestone flagging: *any* strict
improvement updates the best, whether or not it cleared the thresholds. So
a run of sub-threshold nudges raises the bar for the next milestone rather
than accumulating into a spurious one.

## The plot

`--plot out.svg` writes a standalone, dependency-free SVG: the objective
plotted against turn, with milestones marked and labelled by turn, and
gate-failing candidates drawn as hollow marks off the line.

SVG is hand-emitted on purpose. A README-embeddable chart must not drag a
plotting stack into a toolkit whose runtime dependencies are KLayout and
`jsonschema` and whose every command must run headless in CI (CLAUDE.md's
"headless always"). The output is deterministic, diffable text, so a
committed plot's changes are reviewable rather than an opaque binary blob.

## Why a standalone verb rather than a `klt report` envelope kind

[`klt report`](report.md) already renders `klt` JSON envelopes into a
combined text/markdown report, so extending it was the obvious
alternative. It was rejected for three concrete reasons:

1. **Shape.** `klt report` reads *one JSON object per input file* and
   detects its kind from that object's own top-level fields. A trajectory
   log is a *sequence* of objects; supporting it would force `klt report`
   to special-case "this file is many objects, not one" ahead of its shape
   detector.
2. **Derived, not transcribed.** Every `klt report` section is a
   transcription of fields already in its input. A trajectory's interesting
   content — best-so-far tracking, milestones — exists only *across*
   records and has to be computed.
3. **Knobs.** `--threshold`, `--threshold-pct`, and `--plot` are
   meaningless for `drc`/`lvs`/`layout-metrics`/`error` envelopes. Folding
   them into `klt report` would give it flags that silently do nothing for
   four of its five input kinds.

The verbs stay composable instead: `klt trajectory --format
github-summary` emits markdown that concatenates cleanly with `klt
report`'s output in the same step summary.

## Publication: trajectory logs are audited prose

A trajectory log's `note` fields are **prose written during a run and
published afterwards** — new published surface, not just numbers. In a
canary/dogfood repo, that means a log is subject to the same
pre-publication content audit as any other prose the repo ships, before it
is committed to a public repo or rendered into a README. Concretely, before
publishing a log:

- **Notes are engineering statements, not commentary.** Each `note`
  describes what changed in the candidate. Scratch commentary, speculation
  about non-public work, and anything naming a third party do not belong
  in a published log.
- **No proprietary or NDA'd content.** Per CLAUDE.md's open-PDKs-only rule,
  a note must never reference proprietary PDK data or NDA'd design rules —
  including indirectly, by describing a rule the public deck does not
  contain.
- **`candidate_ref`/`eval_ref` are repo-relative paths**, not absolute
  paths from someone's machine (which leak usernames and local layout) and
  not URLs requiring credentials.
- **No secrets.** A log is committed text like any other; the same
  no-credentials rule applies.

A log that cannot pass that audit is a run artifact, not evidence — keep it
out of the published tree.

## JSON schema (the contract)

**JSON is the API.** Per the project's rules, **breaking (renaming,
removing, or retyping) a field is a breaking change**; new fields may be
added without breaking the contract. See
[`../json-contract.md`](../json-contract.md) for the envelope shared across
all `klt` commands (`schema_version`, error shape, exit codes).

Two versions travel in this payload, and they are independent:
`schema_version` is *this command's own output* shape, and
`record_schema_version` is the *log record* shape it read.

```json
{
  "schema_version": 1,
  "record_schema_version": 1,
  "source": "trajectory.jsonl",
  "objective": {"name": "gate_count", "polarity": "minimize", "unit": "gates"},
  "threshold": {"absolute": 100.0, "percent": 0.0},
  "record_count": 5,
  "first_turn": 1,
  "last_turn": 60,
  "baseline": {"turn": 1, "value": 8298, "candidate_ref": "runs/t1/div.gds"},
  "best": {"turn": 60, "value": 1300, "candidate_ref": "runs/t60/div.gds"},
  "total_improvement": 6998.0,
  "total_improvement_pct": 84.33,
  "wall_clock_s_total": 146.7,
  "milestone_count": 2,
  "milestones": [
    {
      "turn": 22,
      "from_turn": 1,
      "from_value": 8298,
      "value": 2010,
      "improvement": 6288.0,
      "improvement_pct": 75.78,
      "candidate_ref": "runs/t22/div.gds",
      "gate_status": "pass",
      "note": "hardware modulo divider replaced with iterative shift-subtract"
    }
  ],
  "records": [
    {
      "turn": 22,
      "candidate_ref": "runs/t22/div.gds",
      "value": 2010,
      "gate_status": "pass",
      "eval_ref": "runs/t22/eval.json",
      "wall_clock_s": 41.2,
      "note": "hardware modulo divider replaced with iterative shift-subtract",
      "milestone": true,
      "improvement": 6288.0,
      "improvement_pct": 75.78,
      "best_so_far": true
    }
  ],
  "plot": {"format": "svg", "path": "trajectory.svg"},
  "markdown": "## Optimization Trajectory: gate_count (gates)\n...",
  "text": "Optimization Trajectory: gate_count (gates)\n..."
}
```

### Top-level fields

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | Version of this command's own JSON shape (starts at `1`). |
| `record_schema_version` | integer | Version of the log *record* shape this build understands (starts at `1`). |
| `source` | string | The log path as given (or `"-"` for stdin). |
| `objective` | object | `{name, polarity, unit}` — the objective every record agreed on. |
| `threshold` | object | `{absolute, percent}` — the thresholds milestone detection was run with, echoed so a rendered table is self-describing. |
| `record_count` | integer | Number of records read. |
| `first_turn` / `last_turn` | integer | Turn of the first/last record. |
| `baseline` | object \| null | `{turn, value, candidate_ref}` of the first eligible record. `null` only if every record failed its gates. |
| `best` | object \| null | `{turn, value, candidate_ref}` of the best eligible record. |
| `total_improvement` | number \| null | Polarity-aware improvement from `baseline` to `best`. |
| `total_improvement_pct` | number \| null | The same as a percentage of the baseline value. |
| `wall_clock_s_total` | number \| null | Sum of every record's `wall_clock_s`; `null` when no record reported one. |
| `milestone_count` | integer | `len(milestones)`. |
| `milestones` | array\<object\> | One entry per flagged milestone, in turn order — see below. |
| `records` | array\<object\> | One entry per log record, in file order — the derived per-record view. |
| `plot` | object \| null | `{format, path}` when `--plot` was given, else `null`. |
| `markdown` | string | The full rendering as GitHub Flavored Markdown — identical to `--format github-summary`'s stdout. |
| `text` | string | The full rendering as plain text — identical to `--format text`'s stdout. |

### `milestones[]` entries

| Field | Type | Description |
| --- | --- | --- |
| `turn` | integer | The milestone record's turn. |
| `from_turn` | integer | Turn of the best prior record it improved on. Rendered as the table's `turns` span (`from_turn-turn`, or just `turn` when they are equal). |
| `from_value` / `value` | number | Objective before and after — the table's `x -> y` cell. |
| `improvement` | number | Polarity-aware `from_value` -> `value` improvement. |
| `improvement_pct` | number | The same as a percentage of `\|from_value\|`. |
| `candidate_ref` | string | The candidate this milestone was measured on. |
| `gate_status` | string | `"pass"` or `"unknown"` (a `"fail"` record can never be a milestone). |
| `note` | string \| null | The record's `note` — the table's "what changed" cell. |

### `records[]` entries

Every record's derived view: `turn`, `candidate_ref`, `value`,
`gate_status`, `eval_ref`, `wall_clock_s`, `note`, plus `milestone`
(boolean), `improvement`/`improvement_pct` (relative to the best prior
record, `null` for the baseline and for ineligible records), and
`best_so_far` (boolean — this record set a new best when it was evaluated).

`records[]` never embeds gate output or candidate contents; it is a derived
index into the log, not a re-export of the run's artifacts.

## Exit codes and errors

| Exit code | Meaning |
| --- | --- |
| `0` | Trajectory rendered successfully. |
| `1` | The log was missing/unreadable, empty (no records), contained a line that is not a JSON object, contained a record failing schema validation, disagreed with itself about the objective, had a turn go backwards, or `--plot`'s path could not be written. |
| `2` | Usage error (no `<log>` given, bad `--format`/`--threshold` value) — from argparse. |

There is no additional exit code: **a trajectory with zero milestones is a
successful render of a real result** ("nothing cleared the threshold" is a
finding), not a failure. A malformed or empty log is the opposite — it is
an error, never a silently empty table, so a typo'd path or a truncated log
fails loudly.

On error, a concise message is written to **stderr** and nothing is written
to stdout. No Python traceback is printed.

- `--format text` (default) and `--format github-summary`: a plain-text
  line prefixed `klt trajectory:`.
- `--format json`: the documented JSON error envelope, with the offending
  line named:

  ```json
  {
    "schema_version": 1,
    "error": {
      "command": "trajectory",
      "message": "runs/trajectory.jsonl:3: 'objective.polarity' must be one of ['minimize', 'maximize'], got 'smaller'"
    }
  }
  ```

## Worked example

```
$ klt trajectory runs/trajectory.jsonl --threshold 100 --plot runs/trajectory.svg
Optimization Trajectory: gate_count (gates)
-------------------------------------------
Objective: gate_count (minimize)
Records: 5 (turns 1-60)
Milestones: 2
Milestone threshold: >100 absolute, >0% relative
Baseline: 8,298 at turn 1
Best: 1,300 at turn 60
Total improvement: 6,998 (84.3%)
Wall clock: 146.7s total

turns  gate_count (gates)  what changed
-----  ------------------  ------------------------------------------------------
1-22   8,298 -> 2,010      hardware modulo divider replaced with shift-subtract
22-48  2,010 -> 1,304      bypassed REDUCE stage, merged reduction modules

plot: runs/trajectory.svg
```

Piping the same run into a step summary alongside the final artifact's own
report:

```
klt trajectory runs/trajectory.jsonl --format github-summary >> "$GITHUB_STEP_SUMMARY"
klt report runs/final-drc.json --format github-summary >> "$GITHUB_STEP_SUMMARY"
```

## Writing a log

The library backing this verb (`src/klayout_tools/trajectory.py`) exposes
the emitter side too, so a driving loop does not hand-roll JSONL:

```python
from klayout_tools.trajectory import append_record, make_record

append_record(
    "runs/trajectory.jsonl",
    make_record(
        turn=22,
        candidate_ref="runs/t22/div.gds",
        objective_name="gate_count",
        objective_value=2010,
        polarity="minimize",
        wall_clock_s=41.2,
        unit="gates",
        gate_results=[{"check": "drc", "status": "clean"}],
        eval_ref="runs/t22/eval.json",
        note="hardware modulo divider replaced with iterative shift-subtract",
    ),
)
```

`make_record()` validates before returning and `append_record()` validates
before writing, so a malformed record fails at the point it is produced —
not later, when someone tries to render the log as evidence.
