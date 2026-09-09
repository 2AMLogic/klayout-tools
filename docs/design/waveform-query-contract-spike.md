# Spike: the `klt wave` JSON contract (`build` / `query`)

**Status:** spike / contract proposal. Nothing here authorises
implementation — no dependency was added, no `klt` subcommand was written,
and no code under `src/klayout_tools/` changed as part of this document.
This is Phase 1, sub-item B, of [Epic
#1585](https://github.com/2AMLogic/klayout-tools/issues/1585) (`klt wave` —
waveform query surface for functional-verification traces), tracked as
issue #1590. Per [docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "How
capabilities arrive," this is the "propose the JSON contract" step, which
must land (and be reviewed against
[docs/json-contract.md](../json-contract.md)) before any implementation
issue is filed. It follows the same shape
[docs/design/digital-flow-contracts-spike.md](digital-flow-contracts-spike.md)
(Epic #391 Phase 1) already established for a multi-verb JSON-contract
proposal in this repo.

**Relationship to the other two Phase 1 sub-items.** Epic #1585 splits
Phase 1 into three issues: #1589 (survey the query surface and trace
formats against `boldaxolotl/booley`'s `bwave` crate and the FST/VCD reader
landscape), #1590 (this document — propose the contract), and #1591 (the
port-vs-build decision record). This document does not depend on #1589
having merged: the query vocabulary it contracts is taken directly from
Epic #1585's own body, which already names `bwave`'s command set (`build`,
`signal`, `wave`, `value`, `find`, `sample`, `diff`, `distance`, `stats`,
`stuck`) and its `ExtractConfig` design (clock/reset-pattern awareness,
cycle-vs-time addressing, first/last match, `find_stuck`, `sample_at`,
`diff_points`) as the candidate vocabulary to cover. A byte-exact
comparison of `bwave`'s actual CLI output, and the FST-vs-VCD-vs-reader
size/speed measurements, are #1589's job, not this one's — if that survey
later needs a field this document did not anticipate, it is an additive
change to the request/response shapes below (per
[docs/json-contract.md](../json-contract.md)'s additive-envelope
convention), not a rewrite of the contract itself. Equally, this document
does not decide what gets ported from `bwave` vs. written fresh, or the
native-Rust-vs-Python posture — that is #1591's job. The op names below are
deliberately `klt`-native (following this repo's own field-naming
conventions, e.g. `snake_case`, no verb named after a `bwave` Rust module),
not verbatim ports of `bwave`'s own subcommand names.

## 1. Overview

`klt functional-verification`'s own "Out of scope" section
([docs/cli/functional-verification.md](../cli/functional-verification.md))
says: *"Waveform inspection / interactive debug. Batch pass-fail +
coverage is the contract... no waveform artifact is contracted."* This
leaves an agent debugging a failing cocotb regression with only RTL and
log text to reason from — no way to ask "when did `o_valid` first go high
after reset?" or "how many handshakes happened between 1,000 ns and 2,000
ns?" against the simulation that actually ran.

`klt wave` closes that gap with two verbs, in the same two-step
build-then-query shape `klt extract` (build a netlist once, query/diff it
many times) already established:

```
klt wave build <request.json> [--format text|json]
klt wave query <request.json> [--format text|json]
```

- **`klt wave build`** takes a trace file (VCD or FST) plus clock/reset
  metadata and produces a compact, indexed **store** artifact — built
  once, queried many times, exactly like `klt extract`'s netlist output.
- **`klt wave query`** takes a store path plus one or more **query
  operations** and answers them — either a single query per call, or a
  batched list in one call, so an agent debugging several signals at once
  does not pay a store-load round trip per question.

Both verbs are batch/JSON only, per Epic #1585's own "Scope creep toward a
GUI" risk: no viewer, no interactive REPL, no VS-Code integration.

## 2. Shared envelope conformance

Both contracts emit through the same shared shape
[docs/json-contract.md](../json-contract.md) already defines for every
`klt` verb: a flat top-level payload plus `schema_version` (versioned per
command, starting at `1`); the `{"schema_version": 1, "error": {"command",
"message"}}` shape on stderr for a failed run, with **stdout left empty**
and **no envelope emitted** for that run; and the `0`/`1`/`2` exit-code
baseline (`0` success, `1` application error, `2` usage error from
argparse), with `klt wave query` defining an additional code `3` (§5
below).

### `provenance`, with `pdk`/`deck` always `null`

Both verbs emit the shared `provenance` block
([docs/json-contract.md](../json-contract.md) → "Shared `provenance`
block"): `{klt_version, klayout_version, pdk, deck, input}`. **`klt wave`
is the first verb documented to emit this block with `pdk` and `deck`
permanently `null`** — worth calling out explicitly, since
`docs/json-contract.md`'s own prose currently lists `drc`/`lvs`/`extract`/
`sim`/`size`/`precheck` as "the verbs whose verdict depends on the exact
tool build, PDK release, and rule deck." A waveform trace has neither: no
PDK is resolved (there is no layout involved) and no rule deck is applied
(the "check" a query op runs, if any, is the caller's own inline
`predicate`, not a curated rule set — see §5). `klayout_version` is also
always `null`: neither verb invokes the `klayout`/`pya` engine at all,
consistent with the block's own documented convention that an unresolved
field is `null`, never fabricated. What the block still earns its keep
for: `klt_version` (which build produced this store/answer) and `input`
(a `content_hash` pinning exactly which file the run was made against —
the trace file for `build`, the store file for `query`), the same
reproducibility role `klt extract`'s `input` field already plays for a
layout stream.

This is a deliberate design decision of this spike, not an oversight —
flagged here explicitly so it is checked during the Acceptance Criterion
#4 review ("Contract reviewed against `docs/json-contract.md` before Phase
1 is considered complete") rather than discovered as a surprise later.

## 3. Time addressing: simulation time and clock cycles

Every place a query or a build result names a point or a window in time,
it does so through one shared, reusable shape — used identically in both
`klt wave build`'s response and every `klt wave query` op:

```json
{ "time_ns": 84.0 }
```
```json
{ "cycle": 42 }
```

- **`time_ns`** (number) — absolute simulation time, in nanoseconds,
  scaled from the trace's own timescale (VCD `$timescale/FST` header) at
  build time so a query never has to know the trace's native unit.
- **`cycle`** (integer) — a clock-relative index: cycle `0` is the first
  active edge of `build`'s request `clock.signal` **at or after**
  `reset.signal`'s release point (see `klt wave build`'s `reset` field,
  §4), and cycle `N` is the `N`-th such edge after that. Cycle addressing
  requires that `build`'s request declared a `clock` block — see "No
  clock declared" below.
- A time-point object carries **exactly one** of `time_ns` / `cycle`,
  never both — a request supplying both is an application error (exit
  `1`), since the two could disagree (e.g. a `cycle` that does not fall
  exactly on the declared clock edge) and this contract does not define a
  precedence rule to silently resolve that instead of rejecting it.
- Every query-op response that reports "where" also reports **both**
  forms together, regardless of which form the request used to ask
  (`{"time_ns": 114.0, "cycle": 57}`) — so an agent that queried by cycle
  gets the wall-clock time for free (e.g. to correlate against a log
  line), and vice versa. This mirrors `klt place-and-route`'s "top-level
  fields are always the last stage's own values restated" discipline: a
  response never makes the caller re-derive a value it could trivially
  compute once and echo back.
- **No clock declared.** `klt wave build`'s `clock` field is optional
  (§4) — a trace with no discernible clock (e.g. an asynchronous reset
  self-check, or a combinational-only DUT) can still be built and queried
  by `time_ns` alone. A query op that addresses by `cycle` against a
  store built with no `clock` declared is an application error (exit `1`,
  `"cycle addressing requires a clock declared at build time"`), not a
  silent fallback to some inferred period.
- **Windows** (a bounded span used by `count`/`sample`/`stuck`/`wave`
  below) are `{"from": <time-point>, "to": <time-point>}`, where `from`
  and `to` may independently use `time_ns` or `cycle` — e.g. `{"from":
  {"cycle": 0}, "to": {"time_ns": 2000.0}}` is valid, resolved
  independently against the store's own clock/timescale metadata. `to`
  omitted means "through the end of the trace."

## 4. Contract: `klt wave build`

```
klt wave build <request.json> [--format text|json]
```

### Request

```json
{
  "schema": "klt.wave_build.request/1",
  "trace": { "path": "gcd_tb.vcd", "format": "vcd" },
  "clock": { "signal": "clk", "edge": "rising" },
  "reset": { "signal": "rst_n", "active": "low" },
  "signals": null,
  "store": { "path": ".klt/wave/gcd_tb.klwave" }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema` | string | Request contract identifier + major version (matches `klt lvs`/`klt synthesize`/the functional-verification contract's own request-side convention). Not validated — user- or generator-authored input, never emitted by this tool. |
| `trace.path` | string | Path to the input trace, resolved relative to the request (the same convention `klt functional-verification`'s `sources` entries already use). Required. |
| `trace.format` | string | `"vcd"` or `"fst"`. Optional — inferred from `trace.path`'s extension when omitted; an unresolvable/ambiguous extension with `trace.format` omitted is exit `1`. |
| `clock.signal` | string | Optional. Hierarchical path to the clock signal (e.g. `tb.clk`, resolved the same way `klt functional-verification`'s `hdl_toplevel` scoping works — relative to the trace's top scope). Declaring this is what enables `cycle` addressing (§3) in every query against the resulting store; omitted means the store supports `time_ns`-only addressing. |
| `clock.edge` | string | `"rising"` (default) or `"falling"` — which edge of `clock.signal` defines cycle boundaries. Only meaningful when `clock.signal` is given. |
| `reset.signal` | string | Optional. Hierarchical path to the reset signal. When given together with `clock.signal`, cycle `0` (§3) is defined as the first active clock edge at or after this signal's release; when omitted, cycle `0` is the trace's first active clock edge, full stop. |
| `reset.active` | string | `"low"` (default) or `"high"` — which level of `reset.signal` is the asserted (in-reset) state. Only meaningful when `reset.signal` is given. |
| `signals` | array\<string\> \| null | Optional allow-list of hierarchical signal paths to index. `null` (default) indexes every signal the trace declares. A non-null, empty array is a request error (exit `1`) — an empty allow-list can never usefully answer any query op, so it is rejected rather than silently producing a queryable-but-empty store. Narrowing this is a size/build-time optimization for a trace with far more declared signals than an agent will ever query (see Epic #1585's own "trace size" risk) — it does not change any query op's request/response shape. |
| `store.path` | string | Where to write the built store, resolved relative to the request. Required — `klt wave build` never picks an implicit output path, matching `klt extract`'s own explicit-`netlist_path`-on-request-or-default-derived-from-input posture is intentionally *not* followed here: unlike a `.gds`'s netlist, a wave store's identity (which trace, which clock/reset framing, which signal subset) is meaningful enough that this contract requires the caller to name it explicitly. |

### Response

```json
{
  "schema_version": 1,
  "trace": { "path": "gcd_tb.vcd", "format": "vcd", "size_bytes": 184320000 },
  "store": {
    "path": ".klt/wave/gcd_tb.klwave",
    "size_bytes": 6291456,
    "content_hash": "sha256:<hex>"
  },
  "clock": { "signal": "tb.clk", "edge": "rising", "period_ns": 2.0 },
  "reset": { "signal": "tb.rst_n", "active": "low", "release": { "time_ns": 40.0, "cycle": 0 } },
  "timescale": { "unit": "ns", "value": 1 },
  "time_range": {
    "from": { "time_ns": 0.0, "cycle": null },
    "to": { "time_ns": 20000.0, "cycle": 9980 }
  },
  "signal_count": 128,
  "value_change_count": 458213,
  "provenance": {
    "klt_version": "0.4.2",
    "klayout_version": null,
    "pdk": null,
    "deck": null,
    "input": { "content_hash": "sha256:<hex>" }
  }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | Per-command version, per [`docs/json-contract.md`](../json-contract.md). |
| `trace.path` / `trace.format` | string | Echo of the request, `trace.format` resolved (never `null` in a successful response, even if the request omitted it). |
| `trace.size_bytes` | integer | Size of the input trace file, so a caller can see the compaction ratio against `store.size_bytes` without a second filesystem call — this is the concrete number Epic #1585's own Success Criteria asks the Phase 1 spike to measure (a ≥100 MB VCD's build should compact meaningfully; the exact ratio is a Phase 2 measurement, this field just carries it forward as a first-class response value from day one). |
| `store.path` | string | Echo of the request. |
| `store.size_bytes` / `store.content_hash` | integer / string | The built store's size and a `sha256:`-prefixed content hash (same shape as `provenance.deck.content_hash`) — this is the value a `klt wave query` request's `store` field points at, and the value `klt wave query`'s own response echoes back in its `provenance.input.content_hash`, so a chain of build → query → cited-in-a-report is reproducibility-checkable end to end, the same chain `klt synthesize`'s `netlist_path` → `klt place-and-route`'s `netlist` already establishes for the digital-flow contracts. |
| `clock.signal` / `.edge` | string | Echo of the request, `null`/omitted-equivalent when no `clock` block was given. |
| `clock.period_ns` | number \| null | The clock's own measured period (median inter-edge spacing of `clock.signal`), in nanoseconds — `null` when no `clock` was declared, or when `clock.signal` toggled fewer than twice in the trace (no period to measure; not an error, since a single stuck-high/low "clock" is a legitimate thing to build a store against for `time_ns`-only queries). |
| `reset.release` | object \| null | The resolved cycle-`0` anchor point, in both time forms (§3) — `null` when no `reset` block was given. |
| `timescale.unit` / `.value` | string / number | The trace's own declared timescale (e.g. VCD's `$timescale 1ns $end` → `{"unit": "ns", "value": 1}`), recorded so `time_ns` in every downstream query response is traceable to a real trace-file statement, not an assumed unit. |
| `time_range.from` / `.to` | object | The trace's first and last recorded value-change instants, in both time forms (§3) — `time_range.from.cycle` is `null` whenever the very first trace instant precedes cycle `0`'s anchor point (i.e. before reset release, or before the trace's first clock edge with no reset declared): a "before cycle 0" instant reports a `time_ns` and a `null` cycle rather than inventing a negative cycle number. |
| `signal_count` | integer | Count of indexed signals — reflects `signals` filtering, i.e. equals `len(signals)` when a non-null allow-list was given, or the trace's own full declared-signal count otherwise. |
| `value_change_count` | integer | Total indexed value-change events across all indexed signals — the same kind of "how much did this actually index" figure `klt extract`'s `device_count` plays for a netlist. |
| `provenance` | object | Shared block (§2); `pdk`/`deck` always `null` (§2); `input.content_hash` is the **trace** file's hash. |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Store built successfully; the documented payload is on stdout. |
| `1` | Failed to run — unreadable/malformed trace file, `trace.format` unresolvable and not inferable from the path, `clock.signal`/`reset.signal` named but absent from the trace, `signals` present but empty, or an unwritable `store.path`. |
| `2` | Usage error (missing argument, bad `--format` value) — from argparse. |

**No exit code `3`.** Matching `klt synthesize`/`klt extract`'s own
reasoning (see
[digital-flow-contracts-spike.md](digital-flow-contracts-spike.md) §4):
building a store has no pass/fail concept of its own — it either produces
a queryable store or it fails outright. Any pass/fail verdict belongs to
`klt wave query`'s own predicates (§5), applied afterward.

## 5. Contract: `klt wave query`

```
klt wave query <request.json> [--format text|json]
```

### Request

```json
{
  "schema": "klt.wave_query.request/1",
  "store": ".klt/wave/gcd_tb.klwave",
  "ops": [
    { "op": "value", "signal": "dut.o_valid", "at": { "cycle": 42 } },
    {
      "op": "find",
      "signal": "dut.o_valid",
      "match": { "value": "1" },
      "occurrence": "first",
      "window": { "from": { "cycle": 0 } },
      "predicate": { "max_cycle": 100 }
    }
  ]
}
```

A single query is simply a one-entry `ops` array — there is no separate
non-batched request shape, so one query and a batch of ten always parse
and respond identically. This matches the acceptance criterion's "one
query op per call, or a batched list" by making the batch the only shape:
a caller asking exactly one question pays no structural difference from a
caller asking ten.

| Field | Type | Description |
| --- | --- | --- |
| `schema` | string | Request contract identifier + major version. Not validated. |
| `store` | string | Path to a store produced by `klt wave build`, resolved relative to the request. Required. An unreadable/corrupt/missing store is exit `1`. |
| `ops` | array\<object\> | One or more query operations (see "Query op vocabulary" below), evaluated independently — one op's failure does not abort the others (see "Partial-failure design" below). Required, non-empty; an empty `ops` array is exit `1` (mirroring `klt wave build`'s empty-`signals` rejection: a query batch that asks nothing can never usefully respond). |

#### Query op vocabulary

Every op object carries a required `op` discriminator plus op-specific
fields, and an **optional** `predicate` object shared across all seven ops
(§5's "Predicates and exit code 3" below). The seven ops below are the
direct restatement of the questions Epic #1585's own body names as the
motivating gap ("when did `o_valid` first go high after reset?", "how many
handshakes happened between 1,000 and 2,000 ns?", "which signal is stuck?")
plus the additional vocabulary its "prior art" section names from
`bwave`'s `ExtractConfig` (cycle/time addressing, first/last match,
`find_stuck`, `sample_at`, `diff_points`):

| `op` | Purpose | Op-specific request fields | Op-specific response fields |
| --- | --- | --- | --- |
| `"value"` | Value of one signal at one time point. | `signal` (string), `at` (time-point, §3) | `value` (string, the signal's 4-state bit vector as text, e.g. `"1"`, `"01x0"`), `at` (both time forms echoed) |
| `"find"` | First/last time a signal matches a value or transition within a window. | `signal`, `match` (`{"value": <string>}` for a level match, or `{"edge": "rising"\|"falling"\|"any"}` for a transition match), `occurrence` (`"first"` default \| `"last"`), `window` (§3, optional — default is the whole trace) | `found` (boolean), `at` (time-point, both forms; omitted/`null` when `found: false`) |
| `"count"` | How many times a signal matches a value or transition within a window. | `signal`, `match` (same shape as `find`'s), `window` (§3, optional) | `count` (integer) |
| `"sample"` | Sample one or more signals at every edge of a named clock/strobe signal within a window (`bwave`'s `sample_at`). | `signals` (array\<string\>), `edge_of` (string, the sampling signal), `edge` (`"rising"` default \| `"falling"` \| `"both"`), `window` (§3, optional) | `samples` (array\<object\>), each `{"at": <time-point>, "values": {"<signal>": "<value>", ...}}` |
| `"stuck"` | Whether a signal held one constant value for at least a given span within a window (`bwave`'s `find_stuck`). | `signal`, `window` (§3, optional), `min_span` (time-point-shaped span, e.g. `{"cycles": 50}` or `{"time_ns": 100.0}` — the only field in this contract expressing a **duration** rather than a point, see "Durations vs. points" below) | `stuck` (boolean), `value` (string \| null — the constant value, when `stuck: true`), `span` (object \| null — `{"from", "to"}` time-points bounding the longest constant run found, regardless of whether it meets `min_span`) |
| `"diff"` | First point of divergence between this store's signal and the same signal in a second store (a second simulation run) — folds in `bwave`'s separate `distance` command as one extra response field rather than a second op, since both read the same two traces (`bwave`'s `diff`/`distance` split is not adopted verbatim — see the epic-body caveat in §1). | `signal`, `other_store` (string, path to a second `klt wave build` store, resolved relative to the request), `window` (§3, optional, applied identically to both stores' own time axes) | `diverges` (boolean), `at` (time-point \| null, first divergent instant), `distance` (integer, count of divergent value-change events across the whole window — `0` when `diverges: false`) |
| `"wave"` | Bounded, run-length-encoded dump of a signal's value changes within a window — the "ask for the raw trace, bounded" escape hatch, deliberately capped so a query can never flood an agent's context (Epic #1585's own "trace size" risk). | `signal`, `window` (§3, optional), `max_entries` (integer, default `256`) | `entries` (array\<object\>, each `{"from": <time-point>, "to": <time-point \| null>, "value": <string>}`, run-length-encoded — consecutive identical values are one entry, not one per underlying VCD line), `truncated` (boolean — `true` when the window contained more runs than `max_entries` and the array was cut short, so a caller can distinguish "the signal only changed twice" from "there were 40,000 runs and I only got the first 256") |

**Durations vs. points.** Every field above other than `stuck`'s
`min_span` is a **point** (§3) or a **window** bounded by two points. A
duration is spelled `{"cycles": <integer>}` or `{"time_ns": <number>}` —
a sibling shape to a time-point, not reused verbatim, because "42 cycles
long" and "at cycle 42" are different concepts that happen to share a
key name (`cycles` is plural, `cycle` singular) precisely so the two
shapes are never visually confusable in a hand-written request document.

#### Predicates and exit code 3

Every op above may carry an optional `predicate` object, declaring what
"satisfied" means for that op's own result field — this is the mechanism
behind the acceptance criterion's "exit code 3 for query ran, predicate
not satisfied, mirroring `klt drc`'s convention":

| `op` | Predicate shape | Checked against |
| --- | --- | --- |
| `"value"` | `{"equals": <string>}` | the op's own `value` |
| `"find"` | `{"max_cycle": <int>}` and/or `{"max_time_ns": <number>}` (both may be given; both must hold) — plus `{"must_find": <bool>}` (default `true` when any other predicate key is present) | `found` and, when `found: true`, `at` |
| `"count"` | `{"min": <int>}` and/or `{"max": <int>}` | the op's own `count` |
| `"stuck"` | `{"expect": <bool>}` | the op's own `stuck` |
| `"diff"` | `{"expect_diverges": <bool>}` | the op's own `diverges` |
| `"sample"` / `"wave"` | *(no predicate defined)* — these two ops are dump-shaped, not verdict-shaped; a `predicate` key on either is a request error (exit `1`), not silently ignored. |

An op with no `predicate` key is purely informational: its result never
contributes to the query's own `status`. An op whose `predicate` holds
contributes `satisfied: true`; one whose predicate fails contributes
`satisfied: false` but is **not** itself a run failure — the op still
executed and its data-bearing fields are still populated, exactly as `klt
drc` finding violations is a successful run with `status: "violations"`,
not an error.

### Response

```json
{
  "schema_version": 1,
  "store": { "path": ".klt/wave/gcd_tb.klwave", "content_hash": "sha256:<hex>" },
  "status": "unsatisfied",
  "results": [
    {
      "op": "value",
      "signal": "dut.o_valid",
      "at": { "time_ns": 84.0, "cycle": 42 },
      "value": "1"
    },
    {
      "op": "find",
      "signal": "dut.o_valid",
      "found": true,
      "at": { "time_ns": 114.0, "cycle": 57 },
      "satisfied": false
    }
  ],
  "provenance": {
    "klt_version": "0.4.2",
    "klayout_version": null,
    "pdk": null,
    "deck": null,
    "input": { "content_hash": "sha256:<hex>" }
  }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | Per-command version, per [`docs/json-contract.md`](../json-contract.md). |
| `store.path` / `.content_hash` | string | Echo of the request's `store`, plus the store file's own content hash (same value `klt wave build`'s response reported for this store), so a query result is pinned to an exact store build the same way `provenance.deck.content_hash` pins a DRC/LVS run to an exact rule deck. |
| `status` | string | `"ok"` when every op that declared a `predicate` is `satisfied: true` (an op with no `predicate` never affects this), or when no op in the batch declared one at all; `"unsatisfied"` when at least one declared predicate is `satisfied: false`. Never `"error"` in-band — a run that failed to execute at all (bad store, unknown signal, malformed op) emits no envelope (see "Exit codes"). |
| `results` | array\<object\> | One entry per request `ops[]` entry, **same order**, each carrying that op's own `op`/`signal`(s) echoed plus its data fields from the vocabulary table above, plus `satisfied` (boolean, present only when that op declared a `predicate`). |
| `provenance` | object | Shared block (§2); `pdk`/`deck` always `null` (§2); `input.content_hash` is the **store** file's hash (the artifact this run actually read), matching `store.content_hash` above — the two are the same value, reported in both places because `store` is this contract's own natural "which artifact" field and `provenance.input` is the cross-verb-consistent name every other verb's report uses for the same concept. |

#### Partial-failure design

A single malformed or unresolvable op inside an otherwise-valid batch
(e.g. `ops[2]` names a signal absent from the store, while `ops[0]` and
`ops[1]` are fine) is treated the same way `klt drc`'s per-shape checking
already treats one bad input among many: **the whole request fails**
(exit `1`, no envelope, no partial `results[]`) rather than silently
dropping the bad op or inventing a per-op error object inside an otherwise
successful envelope. Rationale: unlike `klt place-and-route`'s
`target_stage` partial-completion design (which models an engine that
*itself* stops partway through a well-defined pipeline), a query batch
with one unresolvable op is a **malformed request** the same way a
`klt lvs` reference file that doesn't parse is — not a normal, expected
partial outcome. An agent that wants graceful degradation across several
speculative signal names issues one op per request and reads the exit
code per call, rather than relying on this contract to triage its own
batch.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Every op ran and every declared `predicate` (if any) is satisfied (`status: "ok"`). |
| `1` | Failed to run — bad request, unreadable/missing/corrupt `store`, an op naming a signal absent from the store, an op using `cycle` addressing against a store built with no `clock` declared, a `predicate` key on an op that does not define one (`"sample"`/`"wave"`), or `ops` empty. |
| `2` | Usage error (missing argument, bad `--format` value) — from argparse. |
| `3` | Every op ran successfully, but at least one declared `predicate` was not satisfied (`status: "unsatisfied"`). |

This is the same `0`/`1`/`2`/`3` trichotomy `klt drc`/`klt lvs`/`klt
functional-verification` all use (see
[docs/cli/drc.md](../cli/drc.md) → "Exit codes" for the original
rationale this contract reuses verbatim): `2` is reserved for argparse
everywhere in this repo, so it is never repurposed for "predicate not
satisfied" even though some tools conflate the two. A caller that wants a
single composable pass/fail gate over a `klt wave query` predicate — the
same way `klt eval`'s gates already compose `klt drc`/`klt
functional-verification`'s exit codes — gets that for free, with no
adaptation, exactly as
[functional-verification.md](../cli/functional-verification.md) →
"Composing into `klt eval`" already documents for that verb's own exit
`3`.

## 6. Composition with `klt functional-verification`

Not implemented by this spike (that is Epic #1585 Phase 3's job — "Wire
it into functional verification"), but stated here so the two contracts'
shapes are checked compatible now rather than reconciled later: Phase 3
adds an opt-in `options.trace` field to `klt functional-verification`'s
request, contracting a trace artifact whose path is exactly the
`trace.path` this document's `klt wave build` request expects — the same
"one verb's output is the next verb's input, unmodified" chaining
[digital-flow-contracts-spike.md](digital-flow-contracts-spike.md) §7
already established for `klt synthesize` → `klt place-and-route`. No
field in this document changes to support that; `klt wave build`'s
`trace.path` was already a generic file path, not something that assumes
it was produced by `klt wave` itself.

```
klt functional-verification (options.trace) ──► trace file ──► klt wave build ──► store ──► klt wave query (×N)
```

## 7. Out of scope for this spike

No `klt` subcommand, dependency, or code under `src/klayout_tools/` was
added or changed. No trace was actually built or queried — every example
above is illustrative, not measured (measurement is #1589's job). Phase 2
(the `klt wave` verb itself) and Phase 3 (wiring into
`klt functional-verification`) of Epic #1585 carry the implementation,
gated on this document per that epic's own Phase 1 completion criterion
("Phase 1 is complete when both documents are merged and the contract has
been reviewed against `docs/json-contract.md`") and this issue's own
Acceptance Criterion #4.

## 8. Open questions carried forward for Phase 2 (not resolved here)

- **Store format.** Whether the on-disk store (`klt wave build`'s
  `store.path` artifact) is itself FST, a `klt`-native binary index, or a
  thin index layered over a kept FST copy of the trace is a Phase 2
  implementation decision informed by #1589's measurements and #1591's
  port-vs-build call — this contract only names the artifact
  (`store.path`, `store.content_hash`), never its internal format, the
  same way `klt extract`'s `netlist_path` names an artifact without this
  repo's contract-level docs specifying its exact serialization beyond
  "a Verilog netlist."
- **Multi-bit signal value text encoding.** The `value` fields above are
  specified as "4-state bit vector as text" (`"1"`, `"01x0"`) matching
  VCD's own native `b`-value syntax; whether `klt wave` also offers a
  decimal/hex rendering as an additive response field, or leaves that to
  the caller, is deferred to Phase 2.
- **Hierarchical signal-path syntax.** This document assumes dot-separated
  scope paths (`dut.o_valid`) matching cocotb/Verilog convention, but does
  not specify escaping rules for a scope name containing a literal `.` —
  deferred to Phase 2, informed by whatever `fst-reader`/VCD parser #1591
  selects and its own native path syntax.
- **`sample`'s multi-signal fan-out cost.** No upper bound is placed on
  `len(signals)` for a `"sample"` op today; whether that needs its own
  `max_entries`-style cap (like `"wave"`'s) once #1589's real-trace
  measurements are in is left to Phase 2.
- **#1591's port-vs-build decision** may find that a specific op in §5's
  vocabulary (most likely `"diff"`/`distance` folding, per the explicit
  caveat above) is materially harder to port than to write fresh, or vice
  versa — that finding does not change this contract's request/response
  shape either way, only Phase 2's implementation plan.

## Related

- #1585 parent epic (`klt wave` — waveform query surface)
- #1589 survey issue (query surface + trace format survey, feeds this
  document's vocabulary and will re-verify its size/speed claims)
- #1591 port-vs-build decision record (Phase 1, sub-item C)
- [docs/json-contract.md](../json-contract.md) — the shared envelope this
  document conforms to
- [docs/cli/functional-verification.md](../cli/functional-verification.md)
  → "Out of scope" — the waveform-inspection gap this epic closes
- [docs/cli/drc.md](../cli/drc.md) → "Exit codes" — the `0`/`1`/`2`/`3`
  convention this contract reuses for `klt wave query`
- [docs/design/digital-flow-contracts-spike.md](digital-flow-contracts-spike.md)
  — the sibling multi-verb JSON-contract spike this document's structure
  follows
- Source of the borrowed design: https://github.com/boldaxolotl/booley
  (`crates/bwave`, Apache-2.0) — attribution and porting decisions are
  #1591's job, not this document's.
