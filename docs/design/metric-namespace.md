# Decision: a declared metric namespace for `klt` verbs (issue #247)

**Status:** decision record + first (pilot) implementation. This document
resolves issue #247's central blocker — additive vs. rename — and records
the registry shape and naming grammar every future verb adopting this
namespace should follow. It follows the same decision-record pattern the
accepted design docs in this directory use (e.g.
[`digital-fleet-unit-abstraction-decision.md`](digital-fleet-unit-abstraction-decision.md)),
adapted to a JSON-contract naming/schema question rather than an engine or
scheduler-reuse choice, and the same "spike before build" discipline
[`lvs-extraction-spike.md`](lvs-extraction-spike.md) (#161, Epic #153 Phase 1)
established for a larger capability. Unlike that spike, this decision ships
alongside a first, narrowly-scoped implementation (`klt layout-metrics`'s
pilot `metrics` block) rather than being purely a proposal — the additive
path carries negligible risk (see "Decision 1" below), so there is no
reason to defer landing it.

## Why this exists

`klt layout-metrics`, `klt drc`, `klt extract`, and `klt sim` each report
numeric fields under ad-hoc, per-verb names — `violation_count`,
`device_count`, `instance_count`, `corner_count`, ... — with no declared
semantics. Nothing states how a metric aggregates across blocks (sum? max?
mean?), whether a larger value is a better or worse outcome, or which
metrics are critical enough to gate signoff mechanically. That means any
cross-block rollup or better/worse comparison has to hard-code that
knowledge at every call site, and `src/klayout_tools/signoff.py` (issue
#309, the stated downstream consumer) has no mechanical way to aggregate a
metric beyond combining each input envelope's own pass/fail `status` — it
cannot answer "is this DRC error count worse than last week's," only "did
this DRC run pass."

**Prior art**: LibreLane/OpenROAD share a single metrics namespace
(METRICS2.1-style): double-underscore hierarchical names with modifier
suffixes — `design__core__area`, `design__instance__count`,
`timing__setup__ws`, `route__drc_errors`, `magic__drc_error__count`,
corner-qualified variants like `timing__setup__ws__corner:ss` — declared in
a registry (`Metric("design__core__area", aggregator=sum_aggregator,
higher_is_better=False)`). Because aggregation and polarity are *declared*,
block→chip rollup and regression detection are mechanical, and every tool in
the ecosystem speaks the same names. This decision reuses that convention's
naming grammar and registry shape, reusing an ecosystem name wherever a
`klt` concept genuinely matches one (`design__instance__count` is adopted
verbatim), rather than inventing a parallel vocabulary.

## Grounding: current field names, read from the merged code

Verified against `origin/main` at the time of writing (verb line numbers
drift release to release; field names are the stable reference):

| Verb | Current field(s) | Declared? |
| --- | --- | --- |
| `klt layout-metrics` (`layout_metrics.py`, `layout_metrics_report()`) | `layer_count`, `cell_count`, `instance_count`, nested `drc.violation_count` | No |
| `klt drc` (`drc.py`, `run_drc()`) | `violation_count` (int), `rule_counts` (dict of rule id → int) | No |
| `klt extract` (`extract.py`, `run_extract()`) | `device_count`, `net_count`, `pin_count`, `device_counts` (dict); with `--parasitics`: `r_count`, `c_count`, `total_resistance_ohm`, `total_capacitance_ff` | No |
| `klt sim` (`sim.py`, `run_sim()`) | `corner_count`, `passed`, `failed`, `errored`; plus caller-named `measurements[]` | No |

Shared envelope: `src/klayout_tools/cli/output.py`'s `emit_success()`/
`emit_error()` deliberately does not inject `schema_version` or hold metric
semantics — each verb's library function owns its own `schema_version`
(`docs/json-contract.md`, "Success shape": versioned **per command, not
globally**). There is no pre-existing `envelope.py` or metrics module;
`src/klayout_tools/metrics.py` (introduced by this issue) is new code, not
an extension of an existing structure.

**Downstream consumer, now real.** `src/klayout_tools/signoff.py` (issue
#309) exists on `origin/main` and already aggregates `klt drc`/`klt
lvs`/`klt extract`/`klt sim` JSON envelopes into a signoff verdict via their
`status` fields and shared `provenance` block. It does not yet consume this
registry — that is future work (see "Follow-on work" below) — but its
existence resolves the "no consumer yet" objection Champion raised when this
issue was first filed as `loom:operator-only` (per this issue's own 2026-08-10
finding and 2026-09-15 revision).

## Decision 1: additive `metrics` block, never a rename

**Decision: every verb that adopts this registry adds a parallel, top-level
`metrics` object alongside its existing fields. No existing field is ever
renamed, removed, or retyped in this pass.**

Per `docs/json-contract.md`'s `--format text` vs `--format json` section,
**adding a field is not a breaking change; renaming or removing one is**.
That makes the parallel-block path the only option that requires no
`schema_version` bump anywhere:

- A straight rename (`violation_count` → `drc__error__count`) is a breaking
  change to every consumer of that field, and — because `schema_version` is
  versioned **per command** (`docs/json-contract.md`) — would force
  independent, coordinated `schema_version` bumps across `klt
  layout-metrics`, `klt drc`, `klt extract`, and `klt sim` simultaneously to
  land the same convention everywhere at once. That is a materially larger,
  higher-risk migration than adding a field, for a purely cosmetic
  win (the same information under a different key).
- An additive `metrics` block costs nothing: existing consumers (the
  klayout-tools.org gallery loader, `tests/test_*.py` field assertions,
  `signoff.py`) are unaffected, `docs/json-contract.md`'s existing "adding a
  field never bumps `schema_version`" rule already covers it directly, and
  every verb can adopt it independently and incrementally, exactly as
  `provenance` (`docs/json-contract.md`, "Shared `provenance` block") and
  `--check`/`--rerun` were each rolled out verb-by-verb without a
  coordinated bump.
- A future major-version cleanup that actually *removes* the pre-registry
  field names (if ever justified) is explicitly out of scope for this
  decision — see "Out of scope" below. Nothing here schedules or implies
  one.

This mirrors `docs/json-contract.md`'s own worked precedent almost exactly:
`provenance` is "additive... adopting it required no `schema_version` bump
on any verb," landed on `drc`/`lvs`/`extract`/`sim`/`size`/`precheck`
independently, over time. The `metrics` block follows the identical rollout
shape.

## Decision 2: registry is data, not code — `{aggregator, higher_is_better, critical}`

**Decision: `src/klayout_tools/metrics.py` is a plain-data registry —
`name -> MetricDef(aggregator: str, higher_is_better: bool | None, critical:
bool)` — never a registry of Python callables.**

LibreLane's own `Metric` class takes an `aggregator` as a Python callable
(`aggregator=sum_aggregator`). `klt`'s registry deliberately stores the
*name* of the aggregator (`"sum"` / `"min"` / `"max"` / `"mean"`, a closed,
small enum) instead, for two reasons specific to this repo:

1. **The registry must be exposable through `docs/json-contract.md`.**
   Per this issue's acceptance criteria, the mapping "documented in
   `docs/json-contract.md` or a new `docs/metrics.md`" needs to be
   something a human (or another tool) can read as *data* — a Python
   closure has no meaningful textual representation to publish. Keeping the
   registry itself JSON-serializable-in-spirit (even though it's authored
   as a Python module today, not a JSON file) means the same information
   could later be exported to an actual JSON/YAML file for a non-Python
   consumer with no semantic loss.
2. **`klt`'s own JSON contract discipline.** CLAUDE.md: "JSON is the
   contract." A registry of callables is an implementation detail baked
   into one language; a registry of `{aggregator: "sum", ...}` tuples is a
   declaration any consumer can act on, matching the spirit of every other
   `klt` JSON payload.

`metrics.py` does still provide a small `aggregate(name, values)` dispatcher
(`sum`/`min`/`max`/`mean` over a sequence) so the registry is mechanically
actionable — not merely documentation nobody calls — without exposing
Python callables as the registry's own data shape.

**`higher_is_better` is a tri-state (`True` / `False` / `None`), not a plain
boolean.** Many of the metrics declared so far are purely structural counts
(`design__layer__count`, `design__cell__count`, `design__instance__count`) —
a bigger design isn't inherently "better" or "worse" by itself; it's a
sizing fact other decisions (area budget, wire length) act on. Forcing a
`True`/`False` polarity onto those would fabricate semantics the metric
itself does not carry, contradicting the everything-is-declared spirit of
this whole effort. `None` is a real, first-class registry state — see
`metrics.py`'s module docstring — not a "TODO, decide this later" stand-in.

**Naming grammar**: a declared name is `a__b__c...` — lowercase segments
joined by `__`, at least two segments. The first segment names the owning
domain/verb family (`design`, `drc`, `extract`, `sim`, ...); the last is
usually a value-kind suffix (`count`, `area`, ...). This mirrors
METRICS2.1/LibreLane's own convention closely enough that a name like
`design__instance__count` is directly recognizable to anyone coming from
that ecosystem — reusing an ecosystem name verbatim wherever the concept
genuinely matches, per this issue's own framing, rather than inventing a
parallel vocabulary that means the same thing under different spelling.

## Decision 3: what "pilot on one verb" means, concretely

**Decision: `klt layout-metrics` is the only verb whose JSON payload gains
a `metrics` block in this pass.** Its four fixed, non-caller-supplied
numeric fields map as follows:

| `layout-metrics` field | Declared name | `aggregator` | `higher_is_better` | `critical` |
| --- | --- | --- | --- | --- |
| `layer_count` | `design__layer__count` | `max` | `null` | `false` |
| `cell_count` | `design__cell__count` | `sum` | `null` | `false` |
| `instance_count` | `design__instance__count` | `sum` | `null` | `false` |
| `drc.violation_count` | `drc__error__count` | `sum` | `false` | `true` |

`design__layer__count` rolls up via `max`, not `sum`: layers are typically
*shared* across blocks in a hierarchy (the same metal stack is drawn
everywhere), so summing a per-block layer count would double-count layers
common to every block — `max` is the closest of the four declared
aggregators to "the chip-level set is at least this large," pending a
dedicated union-style aggregator if that precision is ever worth adding.
`design__cell__count`/`design__instance__count` roll up via `sum` — a
chip's total cell/instance count genuinely is the sum across its
constituent blocks (`design__instance__count`'s aggregator and name are
adopted directly from LibreLane's own metric of the same name).
`drc__error__count` reuses LibreLane/OpenROAD's
`magic__drc_error__count`/`route__drc_errors` family shape, adapted to this
repo's own singular-count field; it is `critical` because any nonzero DRC
error count is, by construction, a signoff blocker.

`layout-metrics`' `signals` field (a `klt sim` response attached verbatim,
see `layout_metrics.py`'s `_attach_signals`) is **not** re-keyed into
`metrics` in this pass — see "Out of scope" below.

### The `tests/golden_metrics/` naming is a separate, pre-existing convention

Issue #247's curator enhancement flagged #248's `tests/golden_metrics/`
fixtures as already using "this issue's proposed naming convention." As
shipped (`tests/golden_metrics/README.md`, `tests/helpers/
metrics_regression.py`), #248's `flatten_metrics()` actually produces
**dot-separated, verb-prefixed** keys mechanically flattened from each
verb's raw JSON response (`extract.device_count`, `drc.violation_count`,
`gen_compose.bbox_um.x1`) — not the double-underscore METRICS2.1-style names
this decision declares (`extract__device__count`, `drc__error__count`).
The two serve different purposes and are **not** in conflict:
`flatten_metrics()` is a generic, verb-agnostic regression-test mechanism
that flattens *whatever* a verb's response contains (including fields this
registry never declares, like `gen_compose.warnings_count`), while
`metrics.py`'s registry is a curated, declared subset with aggregation and
polarity semantics attached. Nothing in this change requires renaming
`tests/golden_metrics/`'s existing dot-separated keys, and this decision
does not attempt to unify the two conventions — a future issue could, if a
concrete need arises, but that is explicitly not part of this pass.

## Implementation (this PR)

- `src/klayout_tools/metrics.py` — the registry module: `MetricDef`
  dataclass, `register()`/`get_metric()`/`is_registered()`/`all_metrics()`,
  and the `aggregate()` dispatcher. Registers the four `layout-metrics`
  metrics from the table above.
- `src/klayout_tools/layout_metrics.py` — `layout_metrics_report()` now
  builds an additive `metrics` object (via `_build_metrics_block()`) from
  fields it already computed, attached only when non-empty. No existing
  field changed shape, name, or type; `SCHEMA_VERSION` stays `1`.
- `docs/json-contract.md` — new "Declared metric namespace (`metrics`
  block, issue #247)" section describing the cross-verb convention.
- `docs/cli/layout-metrics.md` — documents the new `metrics` field and its
  source-field mapping.
- `tests/test_metrics.py` — registry schema test (every registered entry
  has a valid `{aggregator, higher_is_better, critical}` tuple, naming
  grammar enforced, `aggregate()` dispatch covered).
- `tests/test_layout_metrics.py` — extended with `metrics`-block coverage
  (present/absent per status, `drc__error__count` only with `--deck`, CLI
  `--format json` round-trip).

## Follow-on work

Each remaining verb's adoption is filed as its own follow-on issue rather
than bundled here, so each can be reviewed and merged independently without
blocking on the others — the same incremental-rollout shape `provenance`
and `--check`/`--rerun` already used:

- **`klt drc`** — declare `violation_count` as `drc__error__count` (`sum`,
  `higher_is_better=false`, `critical=true` — the same entry
  `layout-metrics`' pilot already declares, reused verbatim since it names
  the same underlying value) and add a `metrics` block to `run_drc()`'s own
  payload directly (today only `layout-metrics`'s *nested* `drc.
  violation_count` is re-keyed; `klt drc`'s own top-level
  `violation_count` is not).
- **`klt extract`** — declare `device_count`, `net_count`, `pin_count`, and
  (when `--parasitics` was given) `r_count`/`c_count`/
  `total_resistance_ohm`/`total_capacitance_ff` under an `extract__*`
  namespace (e.g. `extract__device__count`, matching this issue's own
  original example naming).
- **`klt sim`** — declare `corner_count`/`passed`/`failed`/`errored` under a
  `sim__corner__*` namespace (e.g. `sim__corner__count`,
  `sim__corner__passed_count`). `measurements[].name` stays **permanently**
  out of scope for this registry: those names are defined by the caller's
  own request spec (`docs/cli/sim.md`), not by `klt`, so `klt` cannot
  declare them ahead of time — a future corner-qualified convention (e.g.
  METRICS2.1's `timing__setup__ws__corner:ss` pattern) is a `klt
  sim`-specific design question for that follow-on issue, not resolved
  here.
- **`signoff.py` (issue #309) consuming the registry** — **done (issue
  #1850)**. `build_signoff()`/`build_tier_report()` now read any registered
  `critical: true` metric present in a consumed envelope's own `metrics`
  block (via `is_registered()`/`get_metric()`) and mechanically block that
  check when the metric's value fails its own declared `higher_is_better`
  polarity, independent of the envelope's own `status`. This is generic
  over every kind and every declared critical metric — never hard-coded
  per-verb knowledge — so `klt extract`/`klt sim` landing more critical
  metrics (see their own follow-on issues above) is picked up automatically
  with no further wiring. See `_critical_metric_blockers()` in
  `src/klayout_tools/signoff.py`.

## Out of scope for this decision

- No existing field on any verb is renamed, removed, or retyped.
- No `schema_version` bump on any verb.
- `klt drc`, `klt extract`, and `klt sim` gain no `metrics` block in this
  pass — see "Follow-on work" above for their tracking issues.
- `layout-metrics`'s `signals` block (a verbatim `klt sim` response) is not
  re-keyed here; that is `klt sim`'s own follow-on issue's job, once `klt
  sim` itself declares its corner-count metrics.
- `tests/golden_metrics/`'s existing dot-separated flattened keys (issue
  #248) are untouched; unifying the two conventions is explicitly not
  attempted here (see "The `tests/golden_metrics/` naming is a separate,
  pre-existing convention" above).
- A mechanical `signoff.py` consumer of `critical` metrics was named as
  follow-on work, not built in this decision's original pass — see
  "Follow-on work" above, done as issue #1850.
