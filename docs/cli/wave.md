# `klt wave`

Index a VCD/FST functional-verification waveform trace into a compact,
queryable store, then answer one or more questions against it — the piece
that closes the gap
[`klt functional-verification`'s own "Out of scope"](functional-verification.md#out-of-scope)
named before Epic #1585 Phase 3 wired the two verbs together: *"Batch
pass-fail + coverage is the contract... no waveform artifact is
contracted."* Without `klt wave`, an agent debugging a failing cocotb
regression has only RTL and log text to reason from — no way to ask "when
did `o_valid` first go high after reset?" or "how many handshakes happened
between 1,000 ns and 2,000 ns?" against the simulation that actually ran.
`klt functional-verification`'s own `options.trace` (Phase 3, issue #1845)
now names that artifact directly — its `trace` response field drops
straight into this verb's own `trace` request field unchanged.

Phase 2c of [Epic #1585](https://github.com/2AMLogic/klayout-tools/issues/1585)
("`klt wave` — waveform query surface for functional-verification traces").
Full request/response contract:
[`docs/design/waveform-query-contract-spike.md`](../design/waveform-query-contract-spike.md)
— read that document first for every field's exact semantics; this page
covers the CLI surface, exit codes, and an agent-facing "which query
answers which question" table.

```
klt wave build <request> [--format text|json]
klt wave query <request> [--format text|json]
```

This command is a thin Python wrapper (`klayout_tools/wave.py`): it invokes
the standalone `klt-wave` binary (`native/wave/`, issues #1599/#1600, built
via `cargo build --release` inside that directory) as a subprocess and
never re-implements the FST/VCD engine. Like `klt lvs`/`klt sim`/`klt
techmap`, both subcommands take a **request document** path, not
positional trace/store file args:

- `<request>` — a path to a `klt.wave_build.request/1` (for `build`) or
  `klt.wave_query.request/1` (for `query`) JSON file. Relative paths inside
  the request resolve against the **request file's own directory**.
- `--format` — `text` (default) or `json`.

## Building the native binary

`klt-wave-native` (`native/wave/`) is a plain `cargo build`/`cargo test`
binary crate — no `pyo3`/`maturin` wiring, and **no entry in
`pyproject.toml`** at all (neither a required dependency nor an optional
group), the same "deliberately a plain binary crate" shape
[`klt techmap`](techmap.md) already uses. There is nothing for `uv`/`pip`
to resolve; build it directly:

```sh
cd native/wave && cargo build --release
```

On a checkout without a built binary, `klt wave build`/`klt wave query`
fail with a clear, actionable message naming this exact command (exit 1),
never an opaque stack trace or an install-time break — mirroring
`klt techmap`'s `klayout_tools.techmap._binary_path()` degrade-cleanly
convention (`docs/design/waveform-query-contract-spike.md` section 14).

## `klt wave build`

Index a VCD or FST trace into a compact FST store — built once, queried
many times, exactly like `klt extract`'s netlist output.

### Request

```json
{
  "schema": "klt.wave_build.request/1",
  "trace": { "path": "gcd_tb.vcd", "format": "vcd" },
  "clock": { "signal": "tb.clk", "edge": "rising" },
  "reset": { "signal": "tb.rst_n", "active": "low" },
  "signals": null,
  "store": { "path": ".klt/wave/gcd_tb.klwave" }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `trace.path` | string | Path to the input trace (VCD or FST). Required. |
| `trace.format` | string | `"vcd"` or `"fst"` — optional, inferred from `trace.path`'s extension when omitted. |
| `clock.signal` / `.edge` | string | Optional. Declaring `clock` enables `cycle` addressing (see "Time addressing" below) in every query against the resulting store; `edge` is `"rising"` (default) or `"falling"`. |
| `reset.signal` / `.active` | string | Optional. Declaring `reset` alongside `clock` anchors cycle 0 at the first active clock edge at or after reset release; `active` is `"low"` (default) or `"high"`. |
| `signals` | array\<string\> \| null | Optional allow-list of signal paths to index; `null` (default) indexes everything. A non-null, empty array is a request error (exit 1). |
| `store.path` | string | Where to write the built store. Required — `klt wave build` never picks an implicit output path. |

### Response

The binary's own `klt.wave_build.response/1` JSON: `trace`, `store`,
`clock`, `reset`, `timescale`, `time_range`, `signal_count`,
`value_change_count`, `provenance`. Full field table:
[the contract spike, section 4](../design/waveform-query-contract-spike.md#4-contract-klt-wave-build).

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

Schema: [`docs/schemas/wave-build-request.schema.json`](../schemas/wave-build-request.schema.json) /
[`wave-build-response.schema.json`](../schemas/wave-build-response.schema.json).

`provenance.input` is `{content_hash}` only — no `role` key. This is a
deliberate, permanent divergence from the shared Python `build_provenance`
helper's `{content_hash, role}` shape, decided in issue #2039: see
[`docs/json-contract.md`](../json-contract.md)'s `provenance.input.role`
section for the stated reason (no existing role value honestly describes a
waveform trace, and no `klt signoff` check kind consumes this block today).

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Store built successfully. |
| `1` | Failed to build — unreadable/malformed trace, an unresolvable `trace.format`, a named `clock.signal`/`reset.signal`/`signals` entry absent from the trace, an empty `signals` array, or an unwritable `store.path`. |
| `2` | Usage error (missing argument, bad `--format` value, or — for `build` only — an unreadable/malformed request file). |

**No exit code 3.** Building a store has no pass/fail concept of its own —
it either produces a queryable store or it fails outright. Any pass/fail
verdict belongs to `klt wave query`'s own predicates, applied afterward.

## `klt wave query`

Answer one or more query operations against a store `klt wave build`
produced. A single query is simply a one-entry `ops` array — there is no
separate non-batched request shape.

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

| Field | Type | Description |
| --- | --- | --- |
| `store` | string | Path to a store `klt wave build` produced. Required. |
| `ops` | array\<object\> | One or more query operations (see the table below). Required, non-empty — an empty array is exit 1. |

Every op object carries a required `op` discriminator plus op-specific
fields, and an **optional** `predicate` object (except `sample`/`wave`,
which are dump-shaped, not verdict-shaped — a `predicate` on either is a
request error, exit 1).

Schema: [`docs/schemas/wave-query-request.schema.json`](../schemas/wave-query-request.schema.json) /
[`wave-query-response.schema.json`](../schemas/wave-query-response.schema.json).

### Which query answers which question

The agent-facing map from a debugging question to the op that answers it:

| Question | Op | Request shape | Response fields |
| --- | --- | --- | --- |
| "What's `sig`'s value at cycle 42 / at 84 ns?" | `value` | `signal`, `at` (time point) | `value` (4-state bit-vector text, e.g. `"1"`, `"01x0"`), `at` (both time forms) |
| "When did `sig` first/last go high (or transition) after reset?" | `find` | `signal`, `match` (`{"value"}` or `{"edge"}`), `occurrence` (`"first"`\|`"last"`), `window` | `found`, `at` (`null` when not found) |
| "How many times did `sig` pulse/transition between T0 and T1?" | `count` | `signal`, `match`, `window` | `count` |
| "What did `sigA`/`sigB`/... look like at every clock edge?" | `sample` | `signals`, `edge_of`, `edge` (`"rising"`\|`"falling"`\|`"both"`), `window` | `samples` (array of `{"at", "values": {signal: value}}`) |
| "Is `sig` stuck at one value for at least N cycles/ns?" | `stuck` | `signal`, `window`, `min_span` (`{"cycles"}` or `{"time_ns"}`) | `stuck`, `value` (`null` unless stuck), `span` (the longest constant run found, regardless of `min_span`) |
| "Where does `sig` first diverge from this same signal in another run?" | `diff` | `signal`, `other_store`, `window` | `diverges`, `at` (`null` unless diverging), `distance` (divergent value-change count) |
| "Dump `sig`'s raw value-change timeline (bounded)." | `wave` | `signal`, `window`, `max_entries` (default 256) | `entries` (run-length-encoded `{"from", "to", "value"}`), `truncated` |

Each op's `window` (all optional) is `{"from": <time-point>, "to":
<time-point>}`; an omitted `window` means the whole trace. A time point is
`{"time_ns": <number>}` or `{"cycle": <integer>}`, never both.

### Predicates and exit code 3

Every op above except `sample`/`wave` may carry an optional `predicate`
object declaring what "satisfied" means for that op's own result:

| `op` | Predicate shape |
| --- | --- |
| `value` | `{"equals": <string>}` |
| `find` | `{"max_cycle": <int>}` and/or `{"max_time_ns": <number>}`, plus `{"must_find": <bool>}` |
| `count` | `{"min": <int>}` and/or `{"max": <int>}` |
| `stuck` | `{"expect": <bool>}` |
| `diff` | `{"expect_diverges": <bool>}` |

An op with no `predicate` is purely informational — it never affects the
response's `status`. An op whose predicate holds contributes `satisfied:
true`; one whose predicate fails contributes `satisfied: false`, but the
op still ran and its data fields are still populated — the same way `klt
drc` finding violations is a successful run, not an error.

### Response

```json
{
  "schema_version": 1,
  "store": { "path": ".klt/wave/gcd_tb.klwave", "content_hash": "sha256:<hex>" },
  "status": "unsatisfied",
  "results": [
    { "op": "value", "signal": "dut.o_valid", "at": { "time_ns": 84.0, "cycle": 42 }, "value": "1" },
    { "op": "find", "signal": "dut.o_valid", "found": true, "at": { "time_ns": 114.0, "cycle": 57 }, "satisfied": false }
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

`results` has one entry per request `ops[]` entry, in the same order,
each carrying that op's own `op`/`signal`(s) echoed plus its data fields
(see the op table above), plus `satisfied` when that op declared a
`predicate`. `status` is `"ok"` when every declared predicate is
satisfied (or none was declared), `"unsatisfied"` when at least one
failed.

`provenance.input` is role-less here too, for the same reason as `klt wave
build` above — see that section's note and
[`docs/json-contract.md`](../json-contract.md).

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Every op ran and every declared `predicate` (if any) is satisfied (`status: "ok"`). |
| `1` | Failed to run — bad request, unreadable/missing/corrupt `store`, an op naming a signal absent from the store, `cycle` addressing against a store built with no `clock` declared, a `predicate` on `sample`/`wave`, or `ops` empty. |
| `2` | Usage error (missing argument, bad `--format` value). |
| `3` | Every op ran successfully, but at least one declared `predicate` was not satisfied (`status: "unsatisfied"`). |

**Partial-failure design.** A single malformed or unresolvable op inside
an otherwise-valid batch fails the **whole** request (exit 1, no
envelope) rather than silently dropping the bad op — the same "one bad
input among many fails the batch" posture `klt drc`'s per-shape checking
already has. An agent that wants graceful degradation across several
speculative signal names issues one op per request and reads the exit
code per call.

## Time addressing

Every time point is `{"time_ns": <number>}` (absolute simulation time,
scaled from the trace's own timescale) or `{"cycle": <integer>}`
(clock-relative), never both. A response always reports both forms
together, regardless of which form the request used. Cycle addressing
requires that `build`'s request declared a `clock`; a query op addressing
by `cycle` against a store built with no clock declared is exit 1.

### Cycle-0 anchoring

Cycle `0` is the first active edge of `build`'s `clock.signal` **at or
after** `reset.signal`'s release point (its own `release` response field);
cycle `N` is the `N`-th such edge after that. When no `reset` was declared
(or one was declared but its release was never observed in the trace),
cycle `0` is simply the trace's first active clock edge, full stop — a
benign degrade, not an error. An instant before cycle 0's anchor reports a
real `time_ns` and a `null` cycle, rather than inventing a negative cycle
number.

## Composing with `klt functional-verification`

Not wired up yet — Phase 3 of Epic #1585 adds an opt-in `options.trace`
field to `klt functional-verification`'s own request, naming the trace
artifact `klt wave build`'s `trace.path` then indexes:

```
klt functional-verification (options.trace) ──► trace file ──► klt wave build ──► store ──► klt wave query (×N)
```

Until Phase 3 lands, point `klt wave build`'s `trace.path` at whatever
trace your own simulation run (cocotb's `$dumpfile`, Verilator's
`--trace-fst`, or a standalone Icarus/`iverilog`+`vvp` testbench) already
wrote to disk. For a worked, end-to-end example of diagnosing a real
cocotb failure this way — value-at-time, first-match, count-between, and
`stuck` queries, no RTL read required — see
[`docs/guides/waveform-first-debugging.md`](../guides/waveform-first-debugging.md).

## Out of scope

- **A waveform viewer / interactive debug UI.** Both verbs are batch/JSON
  only — no GUI, no REPL, no VS Code integration (Epic #1585's own "scope
  creep toward a GUI" risk).
- **Derived/virtual signals** (a query referencing a boolean expression
  over raw signals, e.g. `handshake = i_ready & o_valid`) — deferred to a
  future follow-on; every op today addresses raw trace signals only (see
  `docs/design/waveform-query-contract-spike.md` section 10,
  `virtual_signal.rs`).
