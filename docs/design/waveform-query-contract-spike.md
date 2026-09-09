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

---

# `klt wave` — port-vs-build decision record (Epic #1585, Phase 1C)

**Status:** decision record / spike. Nothing here authorises implementation —
no dependency was added, no `klt` subcommand or native crate was written, and
no code outside this document changed. This is
[Epic #1585](https://github.com/2AMLogic/klayout-tools/issues/1585)
("`klt wave` — waveform query surface for functional-verification traces")
Phase 1, issue #1591: *"Decide what to port from bwave ... what to write
fresh, and how attribution is carried (NOTICE + file headers). Decide
native-Rust-first vs Python-first."*

**Filename note (reconciliation flag for whoever merges Phase 1):** the epic
body names this file (`waveform-query-contract-spike.md`) as the natural home
for **both** the Phase 1B JSON contract (issue #1590) and this Phase 1C
decision record, and issue #1591's own Dependencies section says the two may
be "drafted in parallel and reconciled before Phase 1 closes." At the time
this document was written, #1590's contract content had not yet landed on
`main`. This file therefore currently contains **only** the Phase 1C
decision-record content below. If #1590 lands a version of this same file
with the request/response contract, the two need a merge pass that keeps
both — this section's own content is self-contained under the `##` headers
below specifically so that merge is mechanical (append, don't rewrite).
Companion document: [`docs/design/waveform-query-survey.md`](waveform-query-survey.md)
(issue #1589, survey of the query surface and trace formats — read alongside
this one; not re-derived here except where the port/build call needs a fact
the survey would otherwise duplicate).

**Everything sourced below was verified live, not recalled**, following the
verification discipline [`docs/design/mutation-testing-spike.md`](mutation-testing-spike.md)
§1 and [`docs/design/lvs-extraction-spike.md`](lvs-extraction-spike.md) §1
established: `boldaxolotl/booley`'s repository metadata, its `main` branch
HEAD commit, its `crates/bwave/src/*.rs` module headers and file sizes, its
vendored `fst-writer`'s `Cargo.toml`/`LICENSE`, and the `fst-reader`/
`fst-writer` crates.io license fields were all fetched live (GitHub REST API
+ raw content, crates.io API) on 2026-09-09, the date in the fetch citations
below.

## 1. Source repository, verified

[boldaxolotl/booley](https://github.com/boldaxolotl/booley) ("The
open-source agentic RTL IDE"):

| Fact | Value | Source |
| --- | --- | --- |
| License (repo-level, GitHub-detected) | Apache-2.0 (`spdx_id: "Apache-2.0"`) | `GET /repos/boldaxolotl/booley` |
| Default branch | `main` | same |
| `main` HEAD commit (this decision pins to it) | `a5b8909fb611cefc6913321f89d604256baba3f6` | `GET /repos/boldaxolotl/booley/commits/main`, committer date `2026-09-08T15:15:24Z` |
| Repo-root `NOTICE` file | **Does not exist** (404 on `raw.githubusercontent.com/boldaxolotl/booley/main/NOTICE`) | direct fetch, confirms the same finding `docs/design/mutation-testing-spike.md` "Open questions" already made for the same repo |
| `crates/bwave` package version | `0.2.15` | `crates/bwave/Cargo.toml`, `[package] version` |

**Pin the source commit SHA above (`a5b8909f...`), not the `0.2.15` version
string, in every attribution header written under this decision.** The epic
body's own "Prior art" section cites `~0.2.15 as of 2026-09-09`, a version
number; a version tag can move or be deleted upstream, a commit SHA cannot.
Phase 2's port issue must re-fetch this SHA at the moment it actually copies
code (bwave is under active development — `pushed_at: 2026-09-09T06:34:36Z`
at fetch time, hours before this decision was written) and use the SHA that
was actually current at copy time, updating the table above in the same PR.

## 2. Port-vs-build decision, module by module

`crates/bwave/src/` (fetched via `GET /repos/.../contents/crates/bwave/src`,
sizes in bytes as reported by that listing):

| Module | Size | Role (from its own header comment, quoted) | Decision |
| --- | --- | --- | --- |
| `fst.rs` | 122,725 | *"FST-backed implementation of the `ColumnCache` read interface ... The disk format is pure FST — no sidecars, no custom attributes."* | **Port, adapted.** This is the store-read layer the epic names ("FST store... design"); `klt wave`'s own store is the same FST-on-disk format (§4 of the epic body already commits to FST as the contracted artifact), so the read path is the highest-value, most directly reusable piece. |
| `cache.rs` | 138,173 | *"Query layer over the FST waveform store. `ColumnCache` presents header metadata ... plus transition-read primitives, and the ten `*_from_cache` query functions implement the CLI's query surface."* | **Port, adapted.** This is the "column-cache... and query implementations" the epic names directly. The ten `*_from_cache` functions are query-vocabulary logic (value-at-time, first/last match, count-between, sample-on-edge, stuck detection — the exact list #1589's survey enumerates), not CLI-shape logic, so they port with the least adaptation of anything in the crate. |
| `signal.rs` | 12,210 | Signal metadata, glob matching, scope-prefix utilities | **Port, largely as-is.** Small, self-contained, no CLI coupling; every query needs signal-name matching against a request's pattern list. |
| `format.rs` | 42,522 | Value formatting (bin→hex) + typed time-token parsing (`100c`/`100ns`/bare cycle) | **Port, adapted.** The typed-time-token design (cycle vs. simulation-time addressing, resolved once the store header is loaded) is exactly the dual time-addressing #1590's contract independently requires ("Time addressing in both simulation time and clock cycles", epic body Phase 1B). Reusing this rather than re-deriving it from scratch is a direct, low-risk win. |
| `virtual_signal.rs` | 74,842 | Boolean/derived-signal expressions (`name = expr` over a Verilog-literal subset) | **Defer to Phase 2 scoping, not decided here.** A real feature (letting a query reference a derived condition, e.g. `handshake = i_ready & o_valid`), but it is the single largest, most self-contained optional module in the crate and is not named in any of Epic #1585's Phase 1/2/3 success criteria. Recommendation for the Phase 2 issue: land without it first (every `*_from_cache` query still works against raw signals), port this module in a follow-on once the base verb has shipped and a real query has actually needed it — mirrors how `docs/design/mutation-testing-spike.md` §4 deferred the T1-threshold question until the base capability had real-world data. |
| `index.rs` | 8,663 | Cycle-based `.idx` sidecar for **fast seeking into large VCD files** (its own header: *"Cycle-based index for fast seeking into large VCD files"*) | **Not ported.** This indexes the *streaming VCD ingest* path, not the FST query path — `fst.rs`'s own header states the FST-backed cache needs "no sidecars." Since `klt wave build` converts to FST once and every subsequent `klt wave query` reads the FST store directly (§3 below), there is no repeated large-VCD-seek workload this index would speed up in the contracted `build`/`query` split. Revisit only if a future mode queries a raw VCD without first building a store — not in scope per the epic's own Phase 1B contract shape (`build` then `query`, two separate verbs). |
| `extract.rs` | 77,875 | *"Extraction state machine — all modes. Implements `VcdHandler` trait for streaming VCD event processing."* | **Not decided here — genuinely contingent on #1589's survey.** This is bwave's streaming-VCD-parse path, relevant only to the `klt wave build` (VCD→FST conversion) step, not to querying. Whether `klt wave build` needs its own VCD parser at all depends on a fact #1589 is explicitly tasked with measuring (its own Phase 1A description: "Survey FST vs VCD as the contracted artifact"): whether the simulators `klt functional-verification` already drives (Verilator, Icarus) can emit FST natively, making a VCD→FST conversion step unnecessary for those engines. If a from-scratch VCD parse is still needed for some engine/host combination, `parser.rs` (34,308 B) + `vcd_chunk.rs` (13,344 B) + the streaming parts of `extract.rs` are the port candidates for that path; if not, none of the three are needed and `klt wave build` becomes a thin wrapper around whatever native FST dump the engine already produces. **This decision record does not resolve it** — flagged as an explicit open item in §7 for #1589/#1590 to close before Phase 2 is scoped. |
| `main.rs` | 41,551 | `clap`-based CLI argument parsing, bwave's own flag surface (`--find`, `--stuck`, `--wave`, ...) | **Written fresh.** `klt wave`'s interface is JSON request/response documents (`klt wave build <request.json>`, `klt wave query <request.json>`, per the epic body's Phase 1B description and mirroring `klt techmap`/`klt lvs`), not a flag-per-query-mode CLI. bwave's flag vocabulary is still useful as a **naming reference** for the JSON field names #1590's contract should use, but the parsing code itself does not transfer. |
| `output.rs` | 5,785 | bwave's own JSON envelope (`{$schema, command, data, warnings}`, `$schema` pinned to a `boldaxolotl/booley` release tag URL) | **Written fresh.** `klt`'s envelope (`docs/json-contract.md`: `schema_version`, `provenance`, additive-only fields, the `0`/`1`/`2`/`3` exit-code convention) is a different, already-established shape every other `klt` verb uses — adopting bwave's own envelope here would fork `klt`'s own contract for one verb. Nothing in this module ports; the *idea* it embodies ("every emission goes through one envelope function") is already `klt`'s own convention, independently arrived at. |
| `docs.rs` | 3,751 | Embeds a narrative doc corpus (`docs/public/*.md`) into the binary, served by `bwave docs`/`bwave skill` | **Not ported as code; the idea is worth reusing.** `klt` has its own doc convention (`docs/cli/*.md`, referenced from `--help` text, not baked into the binary via `include_dir!`). The epic's own Phase 2 description ("`klt wave docs` (or the cli doc) carries the agent-facing 'which query answers which question' table, ported from bwave's embedded docs where useful") already scopes this correctly: port *content* (the query-to-question mapping table) into `docs/cli/wave.md`, not this embedding mechanism. |

### Verdict

**Port the query engine (`fst.rs`, `cache.rs`, `signal.rs`, `format.rs`),
write the `klt`-contract-facing layer fresh (CLI/JSON parsing, envelope,
docs), defer the derived-signal feature (`virtual_signal.rs`) to a Phase 2
follow-on, and leave the VCD-ingest path (`index.rs`/`extract.rs`/
`parser.rs`/`vcd_chunk.rs`) an open question for #1589's survey to close**
before the Phase 2 implementation issue is filed with a final module list.
This is consistent with the epic's own framing ("the FST store/index/
column-cache design and query implementations are candidates") — candidates,
not a blanket "port everything" — and follows the same selective-port
discipline `docs/design/mutation-testing-spike.md` §1a applied to booley's
mutation-testing seam (ported the ~200-line validation seam verbatim in
spirit, explicitly declined to port the co-located `mutation_lock.py` caching
machinery because it solved a problem `klt`'s own design does not have).

## 3. The `fst-writer` vendoring nuance — a second attribution chain

`crates/bwave/Cargo.toml` declares `fst-writer = { path = "vendor/fst-writer" }`,
not the crates.io release. Fetched directly:

```
# crates/bwave/vendor/fst-writer/Cargo.toml, first two lines:
# Vendored from crates.io fst-writer 0.3.1 (BSD-3-Clause, Kevin Laeufer).
# Vendored for conversion-throughput work the upstream API cannot express:
# parallel per-signal block packing at flush time and a cache-friendly
# value-change buffer layout.
```

and `vendor/fst-writer/LICENSE` is a BSD-3-Clause license, copyright Cornell
University (Kevin Laeufer's academic affiliation) — **not** Apache-2.0. Both
`fst-writer` and `fst-reader` (the crate the epic already names as a "normal
build dependency") are published by the same upstream author, confirmed via
crates.io: `fst-reader` `0.17.0`'s `license` field is `"BSD-3-Clause"`
(`GET https://crates.io/api/v1/crates/fst-reader/0.17.0`), repository
`github.com/ekiwi/fst-reader`. **This means "port bwave's FST layer" is not
a single-source attribution problem — it is two, stacked:**

1. `boldaxolotl/booley` itself — Apache-2.0, the design and query-logic
   source named throughout this document.
2. `ekiwi/fst-writer` (BSD-3-Clause, Kevin Laeufer) — the FST-writing crate
   `booley` vendors and modifies in `crates/bwave/vendor/fst-writer/` for a
   documented performance reason (parallel per-signal block packing).

**Decision: depend on upstream `fst-writer` directly from crates.io (the
same "normal build dependency" treatment the epic already gives
`fst-reader`), do not port booley's vendored-and-modified copy, unless and
until a Phase 2 benchmark shows `klt wave build`'s VCD→FST conversion is
throughput-bound in a way that specifically needs booley's parallel
block-packing patch.** Rationale: `klt-wave-native` gets a real,
independently-versioned, upstream-maintained FST writer for free (same
`[tool.uv.sources]`-free normal-crate treatment `native/mom`'s other crates.io
deps already get — no path override, no vendoring, no second license file to
carry), and the performance patch is real but unmeasured in this repo's own
context: #1589's survey should measure whether stock `fst-writer` 0.3.1's
throughput is adequate for the `klt wave build` sizes the epic's own success
criteria name ("≥ 100 MB VCD... under a second per query," a *query* budget,
not necessarily a *build* budget) before paying the cost of vendoring and
tracking a second, hand-modified upstream crate. If a future measurement
shows stock `fst-writer` is the bottleneck, the fallback is porting
booley's *diff* against upstream (not the whole vendored crate) with its own
BSD-3-Clause notice plus a documented pointer to booley's specific patch
commit — not decided further here, since it is contingent on a measurement
this document does not have.

**This does not change the Apache-2.0 attribution obligation for the ported
`bwave` query-engine modules in §2** — `fst-writer`/`fst-reader` are a
separate, independent BSD-3-Clause dependency relationship (normal
crates.io deps, like any other Rust crate `native/mom`/`native/statime`
already pull in), not code copied from `booley`.

## 4. Attribution plan

### 4a. `NOTICE` file — new, at repository root

**Decision: add a `NOTICE` file at the repository root (alongside `LICENSE`),
distinct from `docs/design/mutation-testing-spike.md`'s own "Open questions"
conclusion for issue #1586/#1592, which recommended *against* a repo-wide
`NOTICE` file for that spike's ~200-line ported Python seam.** That
conclusion is not overridden here — it is correctly scoped to its own,
smaller port. This decision diverges for `klt wave` specifically because:

1. **The epic's own Success Criteria are explicit and additive to file
   headers**: *"Ported code carries Apache-2.0 attribution to booley/bwave in
   `NOTICE` and file headers"* (Epic #1585 body, "Success Criteria" — both
   nouns named, not either/or).
2. **Scale.** §2 above ports roughly 300 KB of source across four modules
   (`fst.rs` + `cache.rs` + `signal.rs` + `format.rs`), the FST-format
   read/query engine `klt-wave-native`'s entire query surface will be built
   on — a structurally different scale of incorporation than a single
   ~200-line validation seam. A `NOTICE` file is the mechanism Apache-2.0 §4
   itself names for exactly this case (aggregating attribution for
   incorporated Apache-licensed works at the distribution root), and it
   gives one place a downstream redistributor of this repo checks, rather
   than requiring them to grep every source file for a header comment.
3. **A second upstream license is now in the chain** (§3 above,
   BSD-3-Clause). A repo-root `NOTICE` file is also where BSD-3-Clause's own
   "retain the above copyright notice" obligation is conventionally
   satisfied for a *dependency* (as opposed to a *file-header* obligation,
   which applies to copied/modified source, not an ordinary Cargo dependency
   declaration) — `fst-reader`/`fst-writer`'s BSD-3-Clause notices belong
   here even though no code from them is copied, matching how a Rust
   project's `NOTICE`/`THIRD-PARTY` file conventionally lists
   permissively-licensed dependency notices alongside any directly-ported
   code's own notices.

Proposed root `NOTICE` content (drafted here as the concrete shape Phase 2
should land verbatim, updated with the real commit SHA re-fetched at port
time per §1):

```
klayout-tools
Copyright (c) 2026 Two AM Logic, Inc

This product includes software developed by third parties, incorporated
under the terms below. See LICENSE for this project's own license (MIT).

--------------------------------------------------------------------------
native/wave/ (klt-wave-native): waveform store, index, and query engine
--------------------------------------------------------------------------

Portions of this code are ported and adapted from:

  boldaxolotl/booley (https://github.com/boldaxolotl/booley)
  crates/bwave/src/{fst,cache,signal,format}.rs
  Copyright (c) the boldaxolotl/booley contributors
  Licensed under the Apache License, Version 2.0
  Source commit: <SHA re-fetched at port time>

  A copy of the Apache License, Version 2.0 is available at
  http://www.apache.org/licenses/LICENSE-2.0

native/wave/ also depends on (unmodified, ordinary Cargo dependencies, no
code copied):

  fst-reader (https://github.com/ekiwi/fst-reader), BSD-3-Clause,
  Copyright (c) 2024, Cornell University (Kevin Laeufer)

  fst-writer (https://github.com/ekiwi/fst-writer), BSD-3-Clause,
  Copyright (c) 2024, Cornell University (Kevin Laeufer)
```

### 4b. File-header convention (applies to every file with ported content)

Every ported/adapted source file under `native/wave/src/` carries a header
comment block, in Rust doc-comment form, placed above the module's own
existing doc comment (not replacing it):

```rust
// Ported and adapted from boldaxolotl/booley, crates/bwave/src/fst.rs
// (commit <SHA re-fetched at port time>, https://github.com/boldaxolotl/booley).
// Copyright (c) the boldaxolotl/booley contributors.
// Licensed under the Apache License, Version 2.0; see /NOTICE.
//
// Modifications from upstream: <one line per substantive change, or "none
// at port time" — kept current as the file diverges>.
```

A file written fresh (§2's "written fresh" rows — the CLI/contract layer,
envelope, docs) carries **no** such header; only files whose content
originates in `booley` do. This mirrors the precedent
`docs/design/mutation-testing-spike.md`'s "Open questions" section already
set for the ported Python seam ("a short attribution header comment (origin
repo, file, license, fetch date)") — same per-file mechanism, applied here in
Rust doc-comment form, now paired with the `NOTICE` file per §4a's scale
argument.

### 4c. Compatibility check

An MIT-licensed project (this repo, per its own `LICENSE`) may incorporate
Apache-2.0-licensed and BSD-3-Clause-licensed components — both are
permissive licenses compatible with MIT redistribution — **as long as the
incorporated portions' own license terms and notices are retained**, which
§4a (`NOTICE`) and §4b (file headers) together accomplish. No code in this
repo outside `native/wave/`'s ported files changes license; the repository
as a whole remains MIT, with `NOTICE` documenting the specific
Apache-2.0/BSD-3-Clause components it carries — the same structure any Rust
project with a handful of permissively-licensed dependencies already has,
made explicit rather than left implicit because this repo, for the first
time, also **copies and adapts source** rather than only depending on
published crates.

## 5. Native-Rust-first vs. Python-first

**Decision: native-Rust-first**, following exactly the pattern
`native/techmap/` (issue #874) and `native/statime/` (issue #809, before its
issue #925 pyo3 promotion) established, verified against both crates'
`Cargo.toml`s in this checkout:

- **New crate at `native/wave/`** (directory-naming precedent:
  `native/techmap/`, `native/statime/`, `native/mom/` — bare capability name,
  not `native/klt-wave/`), package name `klt-wave-native`, binary name
  `klt-wave` — the exact `native/<capability>/` → `klt-<capability>-native`
  package → `klt-<capability>` binary naming triple both precedent crates
  use.
- **Plain `cargo build`/`cargo test` binary crate, no `pyo3`/`maturin`
  wiring at first landing** — matching `native/techmap/Cargo.toml`'s own
  documented rationale (*"Deliberately a plain `cargo build`/`cargo test`
  binary crate -- no `pyo3`/`maturin` wiring... issue #875... folding this
  crate into `klt synthesize`.../a `pyo3` extension-module shape... remain
  open, deferred until a 'Go' verdict warrants them"*) and
  `native/statime/Cargo.toml`'s identical framing before its own later
  promotion. `klt wave`'s own contract (`klt wave build <request.json>`,
  `klt wave query <request.json>`, per the epic body) is request-document-in,
  response-document-out — the same shape `klt-techmap <request.json>`
  already uses as a subprocess invocation from
  `klayout_tools/techmap.py` — so the plain-binary-plus-subprocess pattern
  is a direct fit, not a stretch.
- **Why not Python-first**: the core of what's being ported (§2) is binary
  FST decoding (block-level random access, `lz4`/`miniz_oxide`-compressed
  value-change data per `fst-writer`'s own dependency list) and streaming
  VCD line parsing over multi-hundred-megabyte files with a sub-second
  per-query latency target (epic Success Criteria: *"produces a store the
  agent can query in under a second per query"*). This is precisely the
  class of problem this repo has already decided belongs in Rust — every
  other native crate (`mom`, `congestion`, `yield`, `statime`, `techmap`)
  exists because the equivalent pure-Python implementation was measured or
  judged too slow for the target latency, and `klt wave` has no pure-Python
  precedent to lean on the way, say, `klt drc` leans on `pya`/KLayout's own
  compiled core. A from-scratch pure-Python FST decoder is a real,
  substantial engineering project in its own right (reimplementing exactly
  what `fst-reader`/`fst-writer` already provide, in a slower language, with
  no upstream to track fixes from) — not attempted here, and not
  recommended.
- **Why not `pywellen`/`pyvcd` as an alternative Python-first path**: named
  in the epic body as "a pure-Python fallback ... for hosts without the
  native binary" — evaluated as exactly that (a *fallback*, not the primary
  implementation) in §6 below, not as a replacement for the native engine.

## 6. Python fallback — not required, following established repo precedent

The epic's own "Risks & Considerations" section poses this directly: *"the
decision record must say whether a Python fallback is required for the verb
to be usable on all four fleet hosts, or whether the binary ships pre-built
like `klt-statime-native`."* Two corrections and a decision:

- **Correction: `klt-statime-native` does not, in fact, ship pre-built.**
  Checked directly against this checkout: `klayout_tools/techmap.py`'s
  `_binary_path()` looks for a compiled binary under
  `native/techmap/target/{release,debug}/klt-techmap` and, if neither
  exists, raises a `TechmapError` whose message reads *"the klt-techmap
  binary is not built -- from a repo checkout, run `cargo build --release`
  inside native/techmap/."* `klayout_tools/sta.py`'s own docstring (cited in
  `pyproject.toml`'s `[dependency-groups]` comment) documents the same
  degrade-cleanly contract for `klt-statime-native`: *"`klt synthesize`'s
  `sta` field degrades to `null` (never a hard failure) when the extension
  is absent."* Neither crate is pre-built and vendored into the repo or
  published as a binary release artifact — the epic body's framing of "ships
  pre-built like `klt-statime-native`" does not match this checkout's actual
  mechanism, and this decision record corrects that assumption rather than
  propagating it.
- **The actual established mechanism, verified across four native crates**
  (`native/mom`, `native/congestion`, `native/yield`, `native/statime`, per
  `pyproject.toml`'s `[dependency-groups]`/`[tool.uv.sources]` blocks): each
  is an **optional, checkout-local dependency group** (`uv sync --group
  <name>`, resolved from an in-repo path, never published to PyPI) that
  requires a local Rust toolchain to build, and the Python-facing verb
  **degrades cleanly** (a `null` field, or — for the subprocess-style
  crates like `techmap` — a clear, actionable error naming the exact build
  command) when the toolchain or the built artifact is absent. No `klt` verb
  today assumes every host has Rust; every one of them is designed to work
  (with reduced functionality, or with a legible failure) when it doesn't.
- **Decision: `klt wave` follows this same established pattern — no
  separate Python-implemented fallback engine is required.** A host without
  a Rust toolchain gets `klt wave build`/`klt wave query` failing with a
  clear, actionable message (mirroring `techmap.py`'s `_binary_path()`
  error) rather than a slower pure-Python re-implementation. Writing and
  maintaining a second, functionally-equivalent FST/VCD engine in Python
  purely for toolchain-less hosts would roughly double the engineering and
  ongoing-maintenance surface of this capability for a case every other
  native crate in this repo already treats as "degrade, don't duplicate."
  `pywellen`/`pyvcd` (named in the epic body) remain worth a **narrow**
  look in the Phase 2 implementation issue only for one specific purpose —
  a lightweight *correctness cross-check* in tests (comparable to how
  `native/statime/`'s own README compares its output against OpenSTA as an
  oracle, not as a shipped fallback engine) — not as a second production
  code path.

## 7. Open items carried to #1589/#1590 and the Phase 2 issue

- **`index.rs`/`extract.rs`/`parser.rs`/`vcd_chunk.rs` (VCD ingest path,
  §2):** port-or-not is contingent on #1589's own survey measuring whether
  Verilator/Icarus can emit FST natively for the traces `klt
  functional-verification` produces. Not resolved by this document.
- **`virtual_signal.rs` (derived/boolean signals, §2):** deferred to a
  Phase 2 follow-on rather than the first `klt wave` landing. Not resolved
  further here.
- **`fst-writer` vendored-vs-stock (§3):** depend on stock `fst-writer` from
  crates.io first; revisit only if a real Phase 2 throughput measurement on
  `klt wave build` shows it is the bottleneck.
- **Exact `NOTICE` wording and the real source commit SHA** (§4a): the SHA
  in this document (`a5b8909fb611cefc6913321f89d604256baba3f6`) is
  `main`'s HEAD as of 2026-09-09; whoever files the Phase 2 port issue must
  re-fetch it at the moment code is actually copied and use that value, not
  this one, if bwave has moved on by then.

## 8. Curator-checkable checklist for Phase 2

A checklist a Curator (or any reviewer) can verify against repo state
without needing Rust expertise — each item is a yes/no check against a
specific path or string, not a judgment call:

- [ ] `NOTICE` exists at the repository root and contains an entry naming
      `boldaxolotl/booley`, the Apache-2.0 license, and a 40-character git
      commit SHA (not a version string like `0.2.15`).
- [ ] `NOTICE` also lists `fst-reader` and `fst-writer` (BSD-3-Clause,
      Kevin Laeufer / `ekiwi`) as ordinary dependencies, even though no code
      from them is copied (§4a).
- [ ] Every file under `native/wave/src/` that contains code adapted from
      `crates/bwave/src/{fst,cache,signal,format}.rs` carries the header
      block from §4b, naming the same commit SHA as `NOTICE`.
- [ ] No file under `native/wave/src/` that was written fresh (the
      CLI/contract-parsing layer, the JSON envelope, doc-generation code)
      carries a `booley` attribution header — headers should exactly track
      which files have ported content, not be applied blanket.
- [ ] `native/wave/Cargo.toml`'s `[package] license` field is `"MIT"`
      (matching `native/techmap/Cargo.toml`/`native/statime/Cargo.toml`'s
      own `license = "MIT"` — the crate's own original code is MIT like the
      rest of this repo; `NOTICE`/file headers carry the *ported portions'*
      Apache-2.0/BSD-3-Clause terms, they do not relicense the crate).
- [ ] `native/wave/Cargo.toml`'s `fst-reader`/`fst-writer` dependencies
      (if used) point at crates.io releases, not a vendored/path copy of
      either — unless §7's "vendored-vs-stock" open item was explicitly
      revisited and the vendored choice was justified with a measurement in
      the Phase 2 PR description.
- [ ] `docs/cli/wave.md` exists and does not embed bwave's own
      `--flag`-based CLI surface as `klt wave`'s interface (the contract is
      JSON request/response documents, per #1590).
- [ ] `pyproject.toml` does **not** add `klt-wave-native` as a required
      (non-optional) dependency of the base `klayout-tools` package — it
      should follow the same optional dependency-group *or* plain-subprocess
      pattern §5/§6 establish, so a Rust-less host is not broken by
      installing `klayout-tools`.
- [ ] Whatever Python-facing entry point calls the `klt-wave` binary
      degrades with a clear, actionable error (naming the exact build
      command) when the binary is not built — mirroring
      `klayout_tools/techmap.py`'s `_binary_path()` — rather than an opaque
      stack trace.

## Related

- Epic #1585 — `klt wave` epic, this decision record's parent (Phase 1C).
- Issue #1589 — survey of the query surface and trace formats (companion
  Phase 1A document, `docs/design/waveform-query-survey.md`); several items
  in §7 above are explicitly deferred to it.
- Issue #1590 — the `klt wave` JSON contract (companion Phase 1B document,
  shares this file's name per the epic body — see the reconciliation note at
  the top of this document).
- [`docs/design/mutation-testing-spike.md`](mutation-testing-spike.md) —
  the sibling `boldaxolotl/booley` port-vs-build decision for the mutation-
  testing seam (issue #1586); §4a above explains why this document's
  `NOTICE`-file conclusion diverges from that one's, with reasons.
- [`docs/design/synth-techmap-stage-contract.md`](synth-techmap-stage-contract.md)
  and `native/techmap/Cargo.toml`, `native/statime/Cargo.toml` — the
  native-Rust-first, plain-binary-crate precedent §5 follows.
- `pyproject.toml` `[dependency-groups]` / `[tool.uv.sources]` — the
  optional, checkout-local, degrade-cleanly pattern §6 follows.
- [boldaxolotl/booley](https://github.com/boldaxolotl/booley) — source of
  the ported design (Apache-2.0), specifically
  `crates/bwave/src/{fst,cache,signal,format}.rs`.
- [ekiwi/fst-reader](https://github.com/ekiwi/fst-reader) and
  [ekiwi/fst-writer](https://github.com/ekiwi/fst-writer) — BSD-3-Clause,
  Kevin Laeufer; ordinary crates.io dependencies, not ported code (§3).
