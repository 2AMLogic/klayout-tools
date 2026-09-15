# Waveform-first debugging: diagnosing a cocotb failure with `klt wave` alone

`klt functional-verification`'s own "Out of scope" section is blunt about
what it does not give you: *"Waveform inspection / interactive debug. Batch
pass-fail + coverage is the contract... no waveform artifact is
contracted."* Until this guide, an agent debugging a failing cocotb
regression on a digital canary (`sky130-modexp`, `sky130-usb2-phy`,
`sky130-fpga`, ...) had exactly two sources of truth: the RTL source and the
simulator's log text. Reasoning from RTL means re-deriving the design's
intended behavior from a `case` statement, cross-checking it against the
failure by hand, one signal at a time — slow, and every re-check re-reads the
whole state machine.

This guide walks a **real** cocotb failure end to end using only
`klt wave build` / `klt wave query` — the four ops Epic
[#1585](https://github.com/2AMLogic/klayout-tools/issues/1585)'s own
Success Criteria name (value-at-time, first-match, count-between, stuck).
The RTL's implementation (`modexp.v`'s `case` statement) is never opened
during the diagnosis below; the only design-level fact used at all is the
textbook algorithm the header comment *names* — "square-and-multiply" — to
state two invariants (an expected pass count, an expected per-pass cycle
bound) that `count`/`stuck` then check mechanically, never a step-through
of the implementation itself.

Full op reference: [`docs/cli/wave.md`](../cli/wave.md) ("Which query
answers which question"). Full contract:
[`docs/design/waveform-query-contract-spike.md`](../design/waveform-query-contract-spike.md).

## The scenario

[`examples/wave/`](../../examples/wave/) ships the fixtures this guide
runs against — see [`examples/wave/README.md`](../../examples/wave/README.md)
for exact provenance and the regeneration recipe. In short: the
`sky130-modexp` canary's own cocotb testbench
([`examples/functional-verification/test_modexp.py`](../../examples/functional-verification/test_modexp.py))
driven against a **fixture-only** mutated copy of its RTL
(`examples/wave/modexp-stuck-multiply.v` — not the real, shipped
`modexp.v`) fails on the very first vector:

```
modexp(2,10,1000): got 376, want 24
assert 376 == 24
```

That transcript — an assertion mismatch, one number wrong — is all the
agent starts with. `klt functional-verification`'s own `options.trace`
(Epic #1585 Phase 3a, issue
[#1845](https://github.com/2AMLogic/klayout-tools/issues/1845)) has not
landed yet, so the trace artifact here comes straight from the simulator
run itself (cocotb's `Runner(..., waves=True)`, the same Icarus engine
`klt functional-verification` drives) — exactly the fallback
[`docs/cli/wave.md`](../cli/wave.md#composing-with-klt-functional-verification)
already documents: *"point `klt wave build`'s `trace.path` at whatever
trace your own simulation run... already wrote to disk."* Once Phase 3a
lands, this step is a field read off the `klt functional-verification`
response instead of a side-channel file.

`examples/wave/modexp-stuck-multiply.fst` is that trace — committed,
3.2 KB, cocotb's own `$dumpvars(0, modexp)` dump (no separate testbench
wrapper scope; every signal below is `modexp.<name>`).

## Step 1 — index the trace

```
$ cd examples/wave
$ cat > build-request.json <<'EOF'
{
  "schema": "klt.wave_build.request/1",
  "trace": { "path": "modexp-stuck-multiply.fst", "format": "fst" },
  "clock": { "signal": "modexp.clk", "edge": "rising" },
  "reset": { "signal": "modexp.rst_n", "active": "low" },
  "signals": null,
  "store": { "path": ".klt/wave/modexp-stuck-multiply.klwave" }
}
EOF
$ klt wave build build-request.json --format json
```

```json
{
  "schema_version": 1,
  "trace": { "path": "modexp-stuck-multiply.fst", "format": "fst", "size_bytes": 3279 },
  "store": {
    "path": ".klt/wave/modexp-stuck-multiply.klwave",
    "size_bytes": 4524,
    "content_hash": "sha256:580f96b446ce08b46ff66863e59b107d682fd46423537639aa77919c4c257392"
  },
  "clock": { "signal": "modexp.clk", "edge": "rising", "period_ns": 10.0 },
  "reset": { "signal": "modexp.rst_n", "active": "low", "release": { "time_ns": 20.0, "cycle": 0 } },
  "timescale": { "unit": "ps", "value": 1 },
  "time_range": { "from": { "time_ns": 0.0, "cycle": null }, "to": { "time_ns": 3830.0, "cycle": 381 } },
  "signal_count": 34,
  "value_change_count": 2442,
  "provenance": { "klt_version": "0.5.0", "klayout_version": null, "pdk": null, "deck": null,
    "input": { "content_hash": "sha256:a882a1df2f800aa954ae8137849159fbf01c70ea5ac422720dc79b32eafb5b09" } }
}
```

Already useful without a single query: `reset.release` confirms the reset
protocol behaved (release at 20ns, cycle 0), and `time_range.to` — cycle 381,
3830ns — is the whole run's length, matching the cocotb log's own
`SIM TIME (ns)` of `3830.00`. If those two disagreed, the trace wouldn't be
the run that actually failed — check that before trusting anything below.

## Step 2 — value-at-time: what did the DUT actually output?

The cocotb log already says `got 376`, but it says so via a Python-side
`int(dut.result.value)` read — a second, independent source confirms the
RTL's own registered `result` really did settle on the wrong number, not
that the testbench misread it:

```
$ cat > query-value.json <<'EOF'
{
  "schema": "klt.wave_query.request/1",
  "store": ".klt/wave/modexp-stuck-multiply.klwave",
  "ops": [
    { "op": "value", "signal": "modexp.result", "at": { "cycle": 380 } }
  ]
}
EOF
$ klt wave query query-value.json --format json
```

```json
{ "...": "...", "results": [
  { "op": "value", "signal": "modexp.result", "at": { "cycle": 380, "time_ns": 3820.0 }, "value": "178" }
] }
```

**`value` reports multi-bit signals in hex, not decimal** — the response
field is `"178"` (hex), and `0x178 == 376` decimal: the exact wrong number
the assertion reported. `0x18 == 24` would have been the correct hex value.
Converting hex to decimal is arithmetic on a string this query already
returned, never a re-derivation from RTL.

## Step 3 — first-match: when did the failing handshake complete?

```
$ cat > query-find.json <<'EOF'
{
  "schema": "klt.wave_query.request/1",
  "store": ".klt/wave/modexp-stuck-multiply.klwave",
  "ops": [
    {
      "op": "find",
      "signal": "modexp.done",
      "match": { "value": "1" },
      "occurrence": "first",
      "window": { "from": { "cycle": 0 } }
    }
  ]
}
EOF
$ klt wave query query-find.json --format json
```

```json
{ "...": "...", "results": [
  { "op": "find", "signal": "modexp.done", "found": true, "at": { "cycle": 380, "time_ns": 3820.0 } }
] }
```

`done` rises exactly once in the whole trace, at cycle 380 — this is not a
hang (the design did finish and did assert `done`), it computed the wrong
answer and stopped. That rules out "the FSM never terminates" as the
hypothesis and narrows the search to "the FSM terminates, but late or with
a corrupted intermediate value."

## Step 4 — count-between: did every pass run, or did one go missing?

`base=2, exp=10, mod=1000` at `WIDTH=16`: the design's own header comment
names the algorithm — textbook left-to-right square-and-multiply — and
that class of algorithm always does exactly one square per exponent bit
plus one multiply per *set* exponent bit, independent of how any
implementation of it happens to be built. 16 squares (one per bit of a
16-bit exponent), and `10 == 0b1010` has 2 set bits in its 16-bit
zero-padded form, so 18 total modular-multiply passes is the number the
*algorithm* predicts — a textbook fact about square-and-multiply, not a
line this guide read out of the RTL's implementation. `klt wave query`'s
`count` op checks the trace actually did that:

```
$ cat > query-count.json <<'EOF'
{
  "schema": "klt.wave_query.request/1",
  "store": ".klt/wave/modexp-stuck-multiply.klwave",
  "ops": [
    {
      "op": "count",
      "signal": "modexp.state",
      "match": { "value": "2" },
      "window": { "from": { "cycle": 0 }, "to": { "cycle": 380 } }
    }
  ]
}
EOF
$ klt wave query query-count.json --format json
```

```json
{ "...": "...", "results": [
  { "op": "count", "signal": "modexp.state", "count": 18 }
] }
```

(`modexp.state == 2` is the modular-multiply-run phase — its numeric
encoding is visible directly in the trace as soon as any query names it, no
RTL read required to learn that `2` is *a* meaningfully distinct phase, only
that this particular encoding is the one worth asking about next.) 18
matches the spec-predicted pass count exactly, on **both** this failing
trace and the known-good trace
(`examples/wave/modexp-known-good.fst`, same query, same `count: 18`) — so
the bug is not "a pass went missing or ran twice." Something is wrong
*inside* how long each pass takes, not how many passes ran.

## Step 5 — stuck: is one signal parked somewhere it shouldn't be?

The design's own documented contract is a fixed-width interleaved multiply:
one step per exponent bit, so no single modular-multiply-run phase should
occupy `modexp.state == 2` for more than `WIDTH` (16) consecutive cycles.
`stuck` checks exactly that invariant, with a threshold one cycle past the
documented bound:

```
$ cat > query-stuck.json <<'EOF'
{
  "schema": "klt.wave_query.request/1",
  "store": ".klt/wave/modexp-stuck-multiply.klwave",
  "ops": [
    {
      "op": "stuck",
      "signal": "modexp.state",
      "window": { "from": { "cycle": 0 }, "to": { "cycle": 380 } },
      "min_span": { "cycles": 17 },
      "predicate": { "expect": false }
    }
  ]
}
EOF
$ klt wave query query-stuck.json --format json; echo "exit=$?"
```

```json
{ "...": "...", "status": "unsatisfied", "results": [
  {
    "op": "stuck", "signal": "modexp.state", "stuck": true, "value": "2",
    "span": { "from": { "cycle": 359, "time_ns": 3610.0 }, "to": { "cycle": 377, "time_ns": 3790.0 } },
    "satisfied": false
  }
] }
exit=3
```

`stuck: true`, span cycle 359→377 — **18 cycles**, two more than the
16-cycle bound the spec promises — with `predicate: {"expect": false}`
declared, `satisfied: false` and exit code `3` mechanize "this is the
anomaly" into a script-checkable verdict instead of an eyeballed number.
Running the identical query (same `min_span`) against
`examples/wave/modexp-known-good.fst` returns `"stuck": false`, span 16
cycles (325→341) — under the threshold, `predicate` satisfied, exit `0`.
Same signal, same phase encoding, same query, two different trace files:
one out of every 18 modular-multiply passes in the failing run — the exact
one containing the wrong-answer computation — ran two cycles longer than
every pass in the passing run. That single fact (not a `case` statement
read, not a shifted-register trace stepped through by hand) is the whole
diagnosis: whatever decides when a modular-multiply pass ends is off by a
fixed two-cycle amount, consistently. `examples/wave/README.md` names the
actual one-line mutation for the curious, but nothing in Steps 1–5 needed
it.

## What this replaces

Before this guide, the loop was: read `modexp.v`'s state machine, mentally
simulate it against the failing vector, get the "should be WIDTH cycles"
bound from a re-read of the `case` statement itself, then hand-count cycles
in a text log or a GUI waveform viewer to see where it diverges — repeated
for every hypothesis. Every step above instead started from a `klt wave
query` op named in Epic #1585's Success Criteria (value-at-time,
first-match, count-between, stuck) and a JSON response an agent can also
feed straight into a predicate check (`status`/exit code) without a human
reading a waveform GUI at all. `docs/cli/wave.md`'s own "which query
answers which question" table is the fast index back into this vocabulary
once the specific question changes.

## Out of scope

- **Reading the mutated RTL to *find* the bug.** This guide names the
  mutation once, in `examples/wave/README.md`, purely so the fixture is
  auditable — the diagnosis above never depends on it.
- **`options.trace`.** Not landed yet (issue #1845); this guide's Step 1
  input is a trace file the simulation run wrote directly, per
  [`docs/cli/wave.md`](../cli/wave.md#composing-with-klt-functional-verification).
- **A waveform viewer.** Every step above is `klt wave query` plus a JSON
  response — see `docs/cli/wave.md`'s own "Out of scope" for why that stays
  true going forward.
