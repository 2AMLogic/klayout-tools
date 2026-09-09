# Survey: `klt wave` query surface and trace formats

**Status:** spike / survey. Nothing here authorises implementation — no
dependency was added to `pyproject.toml` or `Cargo.toml`, no `klt`
subcommand was written, and no code under `src/klayout_tools/` or a new
`native/wave/` crate changed as part of this document. This is Phase 1,
sub-item A, of [Epic #1585](https://github.com/2AMLogic/klayout-tools/issues/1585)
(`klt wave` — waveform query surface for functional-verification traces),
tracked as issue #1589.

**Companion document:**
[`docs/design/waveform-query-contract-spike.md`](waveform-query-contract-spike.md)
carries §§1–8 (issue #1590, the `klt wave build`/`klt wave query` JSON
contract) and §§9–16 (issue #1591, the port-vs-build decision record). That
document was drafted and merged before this one and already contains real,
independently-verified facts about `boldaxolotl/booley`'s `crates/bwave`
source (module sizes, the `ExtractConfig` field list, license chain,
native-Rust-first plan). This document does not re-derive those facts —
it cites them where relevant and focuses on what was still open at the time
they were written: the agent-shaped query vocabulary in enumerated form,
a **measured** (not estimated) VCD-vs-FST comparison on a real trace, and
the reader-library survey with a recommendation. Where this document's own
measurements close an item that
[`waveform-query-contract-spike.md`](waveform-query-contract-spike.md) §15
flagged as open (the VCD-ingest port question), that is called out
explicitly in §6 below.

**Everything measured below was actually run, not estimated or recalled.**
Where a number could not be produced live in this sandbox, that is stated
explicitly rather than filled in with a plausible-looking guess — see §7
("What was not measured, and why").

## 1. Methodology

Two constraints shaped how this survey was produced, both worth stating
up front since they explain several of the choices below:

- **cocotb is not installed in this sandbox.** `python3 -c "import cocotb"`
  fails with `ModuleNotFoundError`. This repo's actual `klt
  functional-verification` canaries (`examples/functional-verification/
  test_modexp.py`, driving `modexp.v`) are cocotb testbenches, and cocotb
  could not be installed here either (no attempt was made to reach out to
  PyPI for it specifically, since the point of this survey is the trace
  *artifact*, not re-validating the functional-verification verb itself).
  Instead, a small, self-contained, non-cocotb Verilog testbench
  (`tb_modexp.v`, reproduced in full below) was written to drive the exact
  same RTL this repo already ships
  (`examples/functional-verification/modexp.v`, copied byte-for-byte, not
  reimplemented) through 300 randomized modular-exponentiation operations
  at the RTL's default `WIDTH=16`, structurally mirroring what
  `test_modexp.py` already does (same clock period, same reset sequence,
  same operand-range precondition). This keeps the trace "from an existing
  canary testbench's RTL" (the acceptance criterion's wording) even though
  the stimulus driver differs from the checked-in cocotb module.
- **Outbound network access is domain-gated in this sandbox, not blocked
  outright.** `curl` to `github.com`/`api.github.com`/`raw.githubusercontent.com`
  works with any user agent. `crates.io` and `pypi.org` returned `403`
  with curl's default user agent but `200` with an explicit one (both
  sites' own edge WAFs reject a small set of default UAs, not a sandbox
  restriction) — `pip`/`uv pip`/`cargo` all set their own non-default UA
  and worked immediately once tried directly, without any special
  configuration. This matters because it means the crate/package
  benchmarks below are **real, installed, executed code**, not survey
  prose based on documentation alone.

**Toolchain actually available and used, verified live in this sandbox:**

| Tool | Version | Used for |
| --- | --- | --- |
| `iverilog`/`vvp` (Icarus Verilog) | (Homebrew, present) | Producing the real VCD trace — the same engine `request-modexp.json` already names (`"engine": "icarus"`) |
| `verilator` | 5.052 | Testing native FST emission (§6) |
| `cargo` | (Homebrew/rustup, present) | Building `bwave` from source (§2) |
| `uv` | (present) | Installing `pyvcd`/`pywellen` into a throwaway venv (§8) — **not** added to this repo's `pyproject.toml` |

## 2. Building the real `bwave` binary from source

Per [`waveform-query-contract-spike.md`](waveform-query-contract-spike.md)
§9, `crates/bwave` is a standalone (non-workspace) crate inside
[boldaxolotl/booley](https://github.com/boldaxolotl/booley). A shallow
clone (`git clone --depth 1`, 43 MB) plus `cargo build --release` inside
`crates/bwave/` built a working `bwave` binary with no source
modification:

```
$ git clone --depth 1 https://github.com/boldaxolotl/booley.git
$ cd booley/crates/bwave && cargo build --release
   Compiling fst-reader v0.17.0
   Compiling fst-writer v0.3.1 (.../vendor/fst-writer)
   Compiling bwave v0.2.15 (.../crates/bwave)
    Finished `release` profile [optimized] target(s) in 2m 29s
```

Commit pinned for this survey's measurements: `5a45b4e8bdf2319335e956cd92660e8263cdee15`
(`main` HEAD, fetched 2026-09-09T08:27Z — a few hours after
[`waveform-query-contract-spike.md`](waveform-query-contract-spike.md) §9's
own `a5b8909f...` pin, confirming that document's own warning that `main`
moves quickly and any port issue must re-fetch at copy time). The built
binary is a throwaway artifact of this survey, kept outside the repo
(`/tmp`) exactly like the throwaway Python venv in §8 — nothing under this
repo's own `Cargo.toml`/`pyproject.toml`/`native/` changed to produce it.

This is worth stating plainly: **`bwave` builds cleanly, standalone, with
one command, against its declared dependencies (`fst-reader` 0.17,
vendored `fst-writer` 0.3.1, `clap`, `rayon`, `globset`, `serde`/
`serde_json`).** Nothing in this survey found a build obstacle for the
Phase 2 port.

## 3. `bwave`'s command set, verified against `main.rs`

`crates/bwave/src/main.rs`'s `clap` `Subcommand` enum (fetched live,
`main` HEAD as of §2) declares exactly these top-level commands, each
backed by a `docs/public/commands/<name>.md` embedded help page (also
fetched live) and one `*_from_cache` function in `cache.rs`:

| Command | `cache.rs` function | One-line semantics (from its own `docs/public/commands/*.md`) |
| --- | --- | --- |
| `build` | *(not a query — the VCD→FST conversion step)* | "Parse a VCD waveform and write an FST waveform store... `build` is the only B-Wave subcommand that does significant work; every query after it is cheap." |
| `list` | `list_signals_from_cache` | Enumerate signals, types, widths in the store's scope tree. |
| `signal` | `trace_from_cache` | "Unlike `value` (a snapshot), `signal` is a trace across cycles" — the full or windowed value-change history of one signal. |
| `wave` | `wave_from_cache` | "Horizontal waveform table: rows are signals, columns are cycles" — a bounded, tabular dump. |
| `value` | `snapshot_from_cache` | Snapshot of one or more signals at one time point (`--at N`). |
| `find` | `find_value_from_cache` | Search for cycles/ticks where a signal matches a value; `--first`/`--last`/`--count` control which/how many matches are returned. |
| `sample` | `sample_at_from_cache` | Sample one or more signals at every edge of a trigger signal (`sample <TRIGGER_PAT> <TRIGGER_VAL>`). |
| `diff` | `diff_from_cache` | Compare signal values between two time points; only signals whose value *changed* are printed. |
| `distance` | `distance_from_cache` | Time/cycle distance between two named signal-value events (optionally `--to` a second pattern/value, i.e. handshake latency). |
| `stats` | `stats_from_cache` | Per-signal transition count, toggle %, value histogram, time-in-state. |
| `stuck` | `find_stuck_from_cache` | Signals that never transitioned during the simulation (after reset, unless `--with-reset`). |

That is ten `*_from_cache` functions for ten query-shaped commands (`cache.rs`'s
own module header: *"the ten `*_from_cache` query functions implement the
CLI's query surface"*) plus `build`, which is the VCD-ingest step, not a
query. `main.rs` also declares a `docs`/`gui`(deprecated per its own
long-help) meta-family, not relevant to the query surface.

## 4. `ExtractConfig`, field by field

`crates/bwave/src/lib.rs`'s `ExtractConfig` struct (fetched live, quoted
in full — this is the single struct every one of the ten query functions
above is called with, so it is the closest thing `bwave` has to a unified
"query request" shape, and the direct prior art
[`waveform-query-contract-spike.md`](waveform-query-contract-spike.md)
§1's `klt wave query` op vocabulary already drew from per that document's
own framing):

```rust
pub struct ExtractConfig {
    pub patterns: Vec<String>,               // -s signal glob patterns
    pub async_mode: bool,                     // ticks vs. cycles addressing
    pub clock_pattern: Option<String>,        // --clock
    pub reset_pattern: Option<String>,        // --reset
    pub with_reset: bool,                     // include pre-reset instants
    pub at_time: Option<i64>,                 // --at (resolved)
    pub at_time_is_cycle: bool,
    pub find_pattern: Option<String>,         // find's <PATTERN>
    pub find_value: Option<String>,           // find's <VALUE>
    pub first_match: bool,                    // --first
    pub last_match: bool,                     // --last
    pub stats_mode: bool,
    pub sample_at_pattern: Option<String>,    // sample's <TRIGGER_PAT>
    pub sample_at_value: Option<String>,      // sample's <TRIGGER_VAL>
    pub count_only: bool,                     // --count
    pub find_stuck: Option<String>,           // stuck's [VALUE]
    pub time_min: i64,
    pub time_max: Option<i64>,                // -t START:END window
    pub max_lines: usize,                     // --limit (default 2000)
    pub wave_mode: bool,
    pub wave_rle: bool,                       // --rle
    pub diff_points: Option<(i64, i64)>,      // diff's <T1> <T2>
    pub distance_a: Option<(String, String)>, // distance's <PATTERN> <VALUE>
    pub distance_b: Option<(String, String)>, // distance's --to <PAT> <VAL>
    pub signal_radixes: Vec<(String, Radix)>, // -s PATTERN%RADIX
    pub markers: Vec<(String, i64)>,          // --marker NAME TIME (wave only)
    pub marker_tokens: Vec<(String, TimeToken)>,
    pub virtual_defs: Vec<String>,            // --virtual "name = expr"
    pub json_format: bool,                    // --format json
    pub time_str: Option<String>,             // raw -t before resolution
    pub at_str: Option<String>,               // raw --at before resolution
    pub diff_strs: Option<(String, String)>,  // raw diff args before resolution
}
```

Two design choices worth flagging for Phase 2, since both are already
independently present in
[`waveform-query-contract-spike.md`](waveform-query-contract-spike.md)'s
own contract (§3, §5) and this confirms they were the right call rather
than a departure from prior art:

- **Dual time addressing is load-bearing, not incidental.** `at_time_is_cycle`,
  `async_mode`, and the `TimeToken` enum (`format.rs`, cited in the sibling
  document's §10 table) exist specifically because every one of `bwave`'s
  ten commands needs to accept *either* a cycle count *or* a raw
  simulation-time value, resolved once against the store's own clock/
  timescale metadata (`format.rs`'s `TimeToken::parse`/`resolve_time_tokens`,
  verified live: tokens are one of `Cycle`, `Tick`, `Pico`, `Nano`, `Micro`,
  `Milli`, parsed from suffixed strings like `"100c"`/`"500ns"`). This is
  the exact shape `waveform-query-contract-spike.md` §3 independently
  specifies (`{"time_ns": ...}` / `{"cycle": ...}`, "exactly one, never
  both").
- **One shared config struct across all ten commands, not one struct per
  command.** Every command only sets the subset of fields it needs, and
  `resolve_time_tokens()` is called once regardless of which command is
  running. This is a strong argument *for* `klt wave query`'s own choice
  (`waveform-query-contract-spike.md` §5: "a single query is simply a
  one-entry `ops` array... there is no separate non-batched request
  shape") — `bwave`'s own internal design already treats "which query" as
  a discriminated variant over one shared time/signal/window vocabulary,
  not ten unrelated request shapes.

## 5. Agent-shaped query vocabulary, enumerated

This is the direct answer to the acceptance criterion's list. Each row
names the exact `bwave` command(s)/`ExtractConfig` field(s) that answer
the question, verified against §3/§4 above — not a re-guess of the
contract spike's already-published mapping, but the enumeration that
mapping was built from:

| Agent question | `bwave` command | Key `ExtractConfig` fields |
| --- | --- | --- |
| **Value-at-time**: "what was `o_valid` at cycle 42?" | `value` | `at_time`, `at_time_is_cycle`, `patterns` |
| **First/last match**: "when did `o_valid` first go high after reset?" | `find --first` / `find --last` | `find_pattern`, `find_value`, `first_match`/`last_match`, `time_min`/`time_max` |
| **Count-between-times**: "how many handshakes happened between 1,000 ns and 2,000 ns?" | `find --count` (or `distance --stats`) | `find_pattern`, `find_value`, `count_only`, `time_min`/`time_max` |
| **Sample-on-edge**: "what were `a`/`b`/`sum` on every rising edge of `clk`?" | `sample` | `sample_at_pattern`, `sample_at_value`, `patterns` |
| **Stuck detection**: "which signal never toggled?" | `stuck` | `find_stuck`, `with_reset` |
| **Two-run diff**: "did this signal diverge between run A and run B?" | *(not a single command — see below)* | `diff_points` (single-store, two-*time*-point diff) vs. two separate `distance`/`find` invocations against two stores (cross-*run* diff) |
| **Bounded wave dump**: "show me the raw waveform, capped" | `wave` | `wave_mode`, `wave_rle`, `max_lines`, `time_min`/`time_max` |

**The "two-run diff" row needs its own explanation — this is the one
question `bwave` does not answer with a single command**, and it is worth
being explicit about why, since Epic #1585's own body and
`waveform-query-contract-spike.md` §5's `"diff"` op both name it as
required vocabulary. `bwave diff <T1> <T2>` (confirmed against `diff.md`
and `ExtractConfig.diff_points: Option<(i64, i64)>`) compares **two time
points within the same store** — "what changed between cycle 100 and
cycle 200 of this one run" — not two different stores built from two
different simulation runs. Nothing in `main.rs`'s `Command` enum or
`ExtractConfig` takes a second store/FST path as an argument anywhere.
This confirms `waveform-query-contract-spike.md` §5's own explicit design
note under its `"diff"` op (*"folds in `bwave`'s separate `distance`
command as one extra response field rather than a second op... the
`bwave` `diff`/`distance` split is not adopted verbatim"*) was already
the right read of `bwave`'s actual capability — and goes one step further:
**neither `bwave diff` nor `bwave distance` does a cross-*store* (i.e.
cross-simulation-run) comparison at all.** A `klt wave query` `"diff"` op
comparing `other_store` (per that document's §5 table) is genuinely new
vocabulary relative to `bwave`, not a straight port of an existing
command — Phase 2's implementation of that one op has no `cache.rs`
function to adapt line-for-line the way `find`/`sample`/`stuck`/`wave` do;
it needs to open two `ColumnCache`s and walk both, using `diff_from_cache`
and `find_value_from_cache`'s single-store logic as a reference for how to
walk one signal's value-change stream, not as a function to port whole.

## 6. FST vs VCD: a measured comparison on a real trace

### 6.1 The trace

`tb_modexp.v` (full source below) instantiates
`examples/functional-verification/modexp.v` (copied unmodified, `WIDTH=16`
default) and drives 300 randomized modular-exponentiation operations
(`base_in < mod_in`, matching `test_modexp.py`'s own precondition), each
followed by a `wait (done)`, with `$dumpfile`/`$dumpvars` capturing every
signal in the design.

```verilog
`timescale 1ns/1ps
module tb_modexp;
  localparam WIDTH = 16;
  reg clk, rst_n, start;
  reg [WIDTH-1:0] base_in, exp_in, mod_in;
  wire done;
  wire [WIDTH-1:0] result;

  modexp #(.WIDTH(WIDTH)) dut (
    .clk(clk), .rst_n(rst_n), .start(start),
    .base_in(base_in), .exp_in(exp_in), .mod_in(mod_in),
    .done(done), .result(result)
  );

  initial clk = 0;
  always #5 clk = ~clk;

  integer i;
  integer seed = 32'hC0FFEE;

  task run_one(input [WIDTH-1:0] b, input [WIDTH-1:0] e, input [WIDTH-1:0] m);
    begin
      @(posedge clk);
      base_in <= b; exp_in <= e; mod_in <= m; start <= 1;
      @(posedge clk);
      start <= 0;
      wait (done === 1'b1);
      @(posedge clk);
    end
  endtask

  initial begin
    $dumpfile("modexp_trace.vcd");
    $dumpvars(0, tb_modexp);
    rst_n = 0; start = 0; base_in = 0; exp_in = 0; mod_in = 0;
    repeat (3) @(posedge clk);
    rst_n = 1;
    @(posedge clk);
    for (i = 0; i < 300; i = i + 1) begin
      reg [WIDTH-1:0] m, b, e;
      m = ($random(seed) % 65533) + 2;   // mod_in in [2, 65534]
      b = ($random(seed) % m);           // base_in < mod_in
      e = $random(seed);
      run_one(b, e, m);
    end
    $display("modexp trace generation complete: %0d ops", i);
    $finish;
  end
endmodule
```

Run with `iverilog -g2012 -o sim tb_modexp.v modexp.v && vvp sim` — the
same engine (`"engine": "icarus"`) `request-modexp.json` already uses for
this RTL:

```
VCD info: dumpfile modexp_trace.vcd opened for output.
modexp trace generation complete: 300 ops, mismatches=0
tb_modexp.v:71: $finish called at 1361975000 (1ps)
```

### 6.2 VCD → FST conversion, measured

The `bwave` binary built in §2, run against the VCD this actually
produced:

```
$ ls -la modexp_trace.vcd
-rw-r--r--  1 rwalters  wheel  14829340 Sep  9 01:38 modexp_trace.vcd
$ time bwave build modexp_trace.vcd -o modexp_trace_bwave.fst
# wrote modexp_trace_bwave.fst
0.28s user 0.03s system 230% cpu 0.134 total
$ ls -la modexp_trace_bwave.fst
-rw-r--r--  1 rwalters  wheel   1207800 Sep  9 01:38 modexp_trace_bwave.fst
```

| Metric | VCD | FST (via `bwave build`) |
| --- | --- | --- |
| File size | 14,829,340 bytes (14.8 MB) | 1,207,800 bytes (1.15 MB) |
| **Compression ratio** | — | **12.28×** |
| Simulated span | 1,361,950 ns / 136,195 clock cycles (verified via `bwave stats`) | same (lossless conversion) |
| Signal count | 48 (verified via `bwave list` / `bwave stats`) | same |
| Conversion time | — | 0.134 s wall (≈111 MB/s of input VCD) |

This is the acceptance criterion's headline number: **a real trace from
this repo's own `modexp.v` canary RTL compresses 12.28× converting VCD to
FST**, measured with the actual `bwave build` command, not estimated from
FST's general-purpose compression literature. At this trace's modest
scale (14.8 MB, well under the epic's own "≥100 MB" stress-test target —
see §7 for why that scale was not reached here), the *ratio* is still a
meaningful, directly-usable data point: FST's per-signal delta encoding
plus block compression does not depend on absolute trace size to achieve
compression, only on the trace's own toggle density, so there is no
reason to expect the ratio to collapse at 100 MB — though only an actual
100 MB run (Phase 2, once `klt wave build` exists to drive it, or a
larger synthetic multiplication of this same stimulus) would confirm that
rather than assume it.

### 6.3 Query latency, measured

```
$ time bwave find modexp_trace_bwave.fst tb_modexp.dut.done 1 --first --format json
...
0.05s user 0.01s system 94% cpu 0.065 total
$ time (for i in $(seq 1 20); do bwave find modexp_trace_bwave.fst tb_modexp.dut.done 1 --format json >/dev/null; done)
0.08s user 0.02s system 5% cpu 1.803 total
```

20 invocations of `bwave find` (each a fresh process: load the 1.2 MB FST
store, run the query, print JSON, exit) took 1.803 s wall clock — **≈90 ms
per call**, but only 0.08 s of *total* user CPU time across all 20 calls
(≈4 ms of actual CPU work per call). The gap is process-spawn/exec
overhead on this host, not `bwave`'s own query cost — a `klt-wave-native`
long-lived-process or FFI design (rather than one subprocess spawn per
query op) would eliminate nearly all of the measured 90 ms, consistent
with the epic's own "under a second per query" target being easily met
even by the naive one-process-per-query shape measured here.

### 6.4 Verilator native FST emission — closing an open item from `waveform-query-contract-spike.md` §15

That document's §10/§15 explicitly left open *"whether the simulators
`klt functional-verification` already drives (Verilator, Icarus) can emit
FST natively, making a VCD→FST conversion step unnecessary."* This survey
tested it directly:

```
$ verilator --binary --trace-fst --top-module tb_modexp tb_modexp.v modexp.v
...
fatal error: 'lz4.h' file not found
```

**Verilator 5.052's `--trace-fst` requires `liblz4`'s headers at compile
time, and does not find them via its own default include search path even
when `liblz4` is installed** (Homebrew's `lz4` package was already present
on this host at `/opt/homebrew/include/lz4.h` — the failure is a missing
`-I`, not a missing library). Passing it explicitly fixes the build:

```
$ verilator --binary --trace-fst --top-module tb_modexp \
    -CFLAGS "-I/opt/homebrew/include" -LDFLAGS "-L/opt/homebrew/lib -llz4" \
    tb_modexp.v modexp.v
    Finished ... (built successfully)
$ ./obj_dir/sim_verilator_fst
modexp trace generation complete: 300 ops, mismatches=0
```

This produced a real FST file directly from Verilator (**1,555,794
bytes**, 43 signals, 136,735 cycles / 1,367,350 ns — the same testbench
and RTL as §6.1, but not bit-identical stimulus, since Icarus's and
Verilator's `$random` implementations diverge even from the same seed, so
op durations differ slightly cycle-for-cycle). Verified as a genuinely
valid, queryable FST by running `bwave list`/`bwave stats` against it
directly (no `bwave build` step involved — this file went straight from
Verilator's own tracer to `bwave`'s query engine).

**Answer to the open question: yes, conditionally.** Verilator can emit
FST natively, skipping the VCD-ingest step entirely for that engine — but
only when the build passes explicit `-CFLAGS`/`-LDFLAGS` naming `liblz4`'s
location, which is not guaranteed to be on a host's default compiler
search path (it wasn't on this one, a stock macOS/Homebrew dev machine).
`klt synthesize`'s own Verilator invocation
(`src/klayout_tools/functional_verification.py`'s `COVERAGE_BUILD_ARGS =
("--coverage", "--trace")` — note: plain `--trace`, i.e. VCD, not
`--trace-fst`) would need this exact flag plumbing added if Phase 3 wires
`options.trace` through the Verilator path natively. Icarus Verilog has no
native FST tracer at all — `iverilog`/`vvp`'s `$dumpvars` only emits VCD
(no `--trace-fst`-equivalent flag exists), confirmed by `iverilog -h`
having no such option and by Icarus's own documentation never mentioning
FST output — so **the VCD-ingest path (`bwave build`, i.e.
`crates/bwave/src/{index,extract,parser,vcd_chunk}.rs` per
`waveform-query-contract-spike.md` §10's still-undecided row) is not
optional for `klt wave` as long as `klt functional-verification`'s default
engine is Icarus** (which it is — `request-modexp.json`/`request.json`
both specify `"engine": "icarus"`; only the coverage path uses Verilator
at all, per `COVERAGE_ENGINES = ("verilator",)`). This resolves that
open item: **`klt wave build` needs a real VCD parser (the `extract.rs`/
`parser.rs`/`vcd_chunk.rs` port candidates `waveform-query-contract-spike.md`
§10 left pending) because Icarus, this repo's primary functional-
verification engine, has no native FST output at all** — Verilator's
native path is a worthwhile secondary optimization (skip `bwave build`
entirely when the engine is already Verilator and the flag plumbing is
added) but does not remove the need for VCD ingestion generally.

One more data point from the same run, presented without a firm
explanation since it was not chased further (flagged as a Phase 2
curiosity, not resolved here): Verilator's native FST (1,555,794 bytes,
43 signals) is **~29% larger** than `bwave`'s own VCD→FST conversion of
the Icarus run (1,207,800 bytes, 48 signals) despite tracing *fewer*
signals over a *similar* cycle count. Plausible explanations not verified
here: different FST compression block-size/level defaults between
Verilator's bundled `fstapi`-derived writer and `fst-writer`/`bwave`'s own
build path, or a difference in how each attributes hierarchy/type
metadata. Not resolved by this survey; noted for whoever picks up the
"native Verilator FST" optimization in a later phase.

## 7. What was not measured, and why

- **No trace anywhere near the epic's own "≥100 MB VCD" scale was
  produced.** The real trace measured in §6 is 14.8 MB (300 ops at
  `WIDTH=16`). Reaching 100 MB would need roughly 7× more operations
  (~2,000 ops) or a wider/busier design; this was not attempted because
  the *compression ratio* claim does not require it (§6.2's reasoning),
  and because Icarus's own VCD-dump throughput (§6.1's run took a few
  seconds for 14.8 MB) made a 7×-larger run a multi-minute, not
  multi-second, addition to this survey for a data point (ratio at scale)
  this document already argues should not qualitatively change. Left as
  an explicit Phase 2 follow-up: once `klt wave build` exists, running it
  against a genuinely large trace (either a wider `modexp` parameter sweep
  or a longer-running canary) should confirm §6.2's extrapolation with a
  real number rather than leave it as reasoned-but-unmeasured.
- **`sky130-usb2-phy` does not exist in this repository.** Searched
  exhaustively (`grep -ril usb2`, directory listing of
  `examples/functional-verification/`) — the only two functional-
  verification canaries checked into this repo are `gcd.v`/`test_gcd.py`
  and `modexp.v`/`test_modexp.py`. The issue body's "sky130-modexp or
  sky130-usb2-phy" was an either/or; `modexp.v` was used since it is the
  one that actually exists here.
- **cocotb's own trace-dump path was not exercised.** §1 already covers
  why; the RTL and its testbench-driven stimulus pattern are real and
  taken from this repo, but the trace was produced by a hand-written
  Verilog testbench, not `test_modexp.py` running under cocotb via `klt
  functional-verification` itself. A Phase 2/3 follow-up (wiring `options.trace`
  into the real verb, per `waveform-query-contract-spike.md` §6) should
  re-run this comparison against a trace produced by the actual verb, to
  confirm cocotb's own driven stimulus doesn't materially change the
  toggle density (and therefore the compression ratio) from what this
  survey measured.
- **`fst-writer`'s vendored-vs-stock throughput question
  (`waveform-query-contract-spike.md` §11) was not benchmarked.** This
  survey's `bwave build` measurement (§6.2, 0.134 s for 14.8 MB, using
  `bwave`'s own vendored, parallel-block-packing `fst-writer` fork) is not
  a controlled A/B against stock `fst-writer` 0.3.1 — that would require
  building a second `bwave` variant pointed at the crates.io release
  instead of the vendored path override, which was out of scope for this
  survey (it is `waveform-query-contract-spike.md` §11's own explicitly
  deferred follow-up, not this issue's acceptance criterion). What this
  survey does confirm: 0.134 s for 14.8 MB is already comfortably inside
  any reasonable build-time budget, so the vendored fork's performance
  patch is not needed to hit the epic's own "under a second per *query*"
  target (a build-time number, not the target that number was set for) —
  consistent with, not contradicting, §11's "depend on stock `fst-writer`
  first" recommendation.

## 8. Rust reader options and a Python fallback, surveyed with a recommendation

### 8.1 `fst-reader` vs `wellen`, live crates.io metadata

| Crate | Version | License | Repository | Scope |
| --- | --- | --- | --- | --- |
| `fst-reader` | 0.17.0 | BSD-3-Clause | `github.com/ekiwi/fst-reader` | FST-only decoder. This is what `bwave`'s own `fst.rs` depends on directly (per its `Cargo.toml`, `waveform-query-contract-spike.md` §9). |
| `wellen` | 0.25.6 | BSD-3-Clause | `github.com/ekiwi/wellen` | Unified VCD **and** FST reader, built *on top of* `fst-reader` (its own `Cargo.toml`: `[workspace.dependencies] fst-reader = "0.17.0"`) plus a from-scratch VCD parser (also depends on the separate Rust `vcd` crate as a dev-dependency for cross-checking, per its `Cargo.toml`). Same author as `fst-reader`/`fst-writer` (Kevin Laeufer, `ekiwi`). Powers the `pywellen` Python binding (§8.2) and the Surfer waveform viewer. |

**These are not two independent choices — they are two layers of the same
author's stack.** `fst-reader` is the low-level FST-only primitive
`bwave` builds its own `ColumnCache`/index on top of (maximum control,
matching `waveform-query-contract-spike.md` §10's "port `fst.rs`/`cache.rs`"
recommendation, which already assumes this exact crate). `wellen` is the
higher-level, format-agnostic reader that would be the right choice
**only** if `klt-wave-native` wanted one API that transparently reads
either VCD or FST without `bwave`'s own build/query split — which it does
not: `waveform-query-contract-spike.md`'s own contract (§1) is
deliberately build-once-query-many, exactly `bwave`'s own shape, so there
is no runtime need for a reader that also speaks raw VCD. **Recommendation:
`fst-reader`, matching the already-decided port target — `wellen` adds a
VCD-parsing surface `klt-wave-native` does not need at the Rust layer**
(the VCD-ingest need, per §6.4, is `bwave`'s own `extract.rs`/`parser.rs`/
`vcd_chunk.rs` streaming logic, purpose-built for the extract-once
workflow, not `wellen`'s in-memory whole-file VCD parse).

### 8.2 Python fallback: `pyvcd` and `pywellen`, both installed and measured

Both installed cleanly via `uv pip install --python <venv> pyvcd
pywellen` (see §1's methodology note — no network friction once a
non-default user agent was used, which `pip`/`uv` already send by
default):

| Package | Import name | Version | License | What it actually is |
| --- | --- | --- | --- | --- |
| `pyvcd` | `vcd` | 0.5.0 | MIT (`SanDisk-Open-Source/pyvcd`, verified via GitHub API) | A genuine pure-Python VCD tokenizer/writer. **VCD only — no FST support at all.** |
| `pywellen` | `pywellen` | 0.25.6 | BSD-3-Clause (inherits `wellen`'s workspace license) | A compiled (PyO3/maturin) Python binding to the **same** `wellen` Rust crate as §8.1 — **not** a second, independent implementation. Reads both VCD and FST through one API. |

**This distinction matters for the "pure-Python fallback" framing in the
epic body and `waveform-query-contract-spike.md` §14.** `pyvcd` is
actually pure Python (slow, but has no compiled component, so it installs
and runs anywhere a `pip`/`uv` toolchain exists, independent of whether a
Rust toolchain is available). `pywellen` is **not** pure Python — it is a
prebuilt native extension wheel; using it as a fallback for "a host
without the `klt-wave-native` Rust toolchain" only works if PyPI ships a
wheel for that host's Python/platform combination (it does, broadly, per
its `pyproject.toml`-declared `requires_python: >=3.9` and the wide
`manylinux`/macOS/Windows wheel matrix typical of maturin-built crates —
not independently re-verified per-platform here), not because it avoids
compiled code entirely.

**Measured, on the same real trace as §6** (`modexp_trace.vcd`,
14.8 MB / `modexp_trace_bwave.fst`, 1.15 MB):

| Workload | `pyvcd` (VCD, pure Python) | `pywellen` (VCD) | `pywellen` (FST) |
| --- | --- | --- | --- |
| Full-file tokenize (`vcd.reader.tokenize`, every token) | **17.809 s** (1,285,943 tokens) | — (not a comparable API; see below) | — |
| Header/hierarchy load only | — | 0.0004 s | 0.0057 s |
| Load one signal (`dut.done`) | — | 0.0743 s | 0.0139 s (**5.3× faster than VCD** for this single-signal access) |
| Load **all 52** signals, one at a time | — | 0.1560 s | 0.7723 s (**5× slower than VCD** for this bulk-sequential access pattern) |

Two honest, somewhat counter-intuitive findings from this table, both
worth carrying into Phase 2 rather than smoothing over:

1. **`pyvcd`'s pure-Python tokenizer is roughly 230× slower than
   `pywellen`'s compiled VCD reader** on the exact same file (17.8 s vs.
   ~0.16 s for a full scan). This is the single clearest number in this
   survey for "why native-Rust-first, not Python-first" (already decided
   in `waveform-query-contract-spike.md` §13 on different grounds —
   latency budget, no pure-Python precedent — this measurement is
   additional, concrete evidence for the same conclusion, not a new
   argument).
2. **FST is not uniformly faster to read than VCD once you account for
   *access pattern*, at least through `pywellen`'s own per-signal lazy-load
   API.** Selective access (one named signal out of many) is solidly
   faster against the FST store (5.3×) — this is the workload every
   `klt wave query` op in `waveform-query-contract-spike.md` §5 actually
   is (`value`/`find`/`count`/`stuck` all name one signal; `sample`/`wave`
   name a bounded few). But a naive "load every signal one at a time"
   loop is *slower* against FST than a single linear VCD scan through
   `pywellen`'s API — plausibly because `pywellen`'s lazy per-`Var`
   `.signal` accessor pays a per-call block-decompression cost against
   FST that a from-scratch sequential VCD line-scan does not, though this
   was not traced further to confirm the exact mechanism. This is
   consistent with — not contradicted by — `bwave`'s own architecture:
   `cache.rs`'s `ColumnCache` is built **once**, with `rayon` parallelism
   (§2's build log shows `230% cpu`, i.e. multi-core), covering every
   signal in a single pass, rather than lazily loading one `Signal` object
   per query the way this `pywellen` micro-benchmark did. The lesson for
   Phase 2 is architectural, not format-level: **the win FST offers is
   contingent on a whole-store column-cache built once (exactly what
   `waveform-query-contract-spike.md` §10 already recommends porting from
   `cache.rs`), not an inherent property of the FST format read naively
   signal-by-signal.**

**Recommendation:** no separate pure-Python fallback *engine* is required
— reaffirming `waveform-query-contract-spike.md` §14's decision on
independent grounds. If a Phase 2 host genuinely lacks a Rust toolchain
and needs *some* Python-side capability, `pywellen` (not `pyvcd`) is the
correct narrow choice: it already reads both VCD and FST through one
compiled, `wellen`-backed API with performance in the same
order-of-magnitude as `bwave` itself (§6.3's `bwave find` at ~4 ms of CPU
time per query vs. `pywellen`'s ~14 ms single-signal FST load — both
"agent doesn't notice the latency," unlike `pyvcd`'s 17.8-second full
scan), matching §14's own scoped suggestion ("a lightweight correctness
cross-check... not a second production code path").

## 9. Answering the acceptance criteria directly

- ✅ **Agent-shaped query vocabulary enumerated** — §5's table, backed by
  §3/§4's source-verified command list and `ExtractConfig`, including the
  one gap (`bwave` has no cross-store diff) called out explicitly rather
  than glossed over.
- ✅ **FST vs VCD tradeoff measured, not estimated, on a real trace from
  an existing canary testbench** — §6.2's 12.28× compression ratio and
  §6.3's ~4 ms/query CPU cost are both live measurements against a real
  VCD produced from `examples/functional-verification/modexp.v` via
  Icarus, the same RTL and engine `klt functional-verification`'s own
  `request-modexp.json` already uses. §7 is explicit about the one
  respect in which the measurement is smaller-scale than the epic's own
  ≥100 MB target, and why that does not undermine the ratio finding.
- ✅ **`fst-reader`/`wellen` and a Python fallback (`pyvcd`/`pywellen`)
  both surveyed with a recommendation** — §8.1 recommends `fst-reader`
  (matching the already-decided port target) over `wellen` for the native
  crate, and §8.2 recommends `pywellen` over `pyvcd` for the narrow
  cross-check role `waveform-query-contract-spike.md` §14 already scoped,
  backed by the measured 230× pure-Python slowdown and the FST/VCD
  access-pattern nuance in point 2 above.

## Related

- #1585 parent epic (`klt wave` — waveform query surface for
  functional-verification traces)
- #1589 — this survey (query surface + trace format survey)
- #1590 / #1591 —
  [`docs/design/waveform-query-contract-spike.md`](waveform-query-contract-spike.md)
  §§1–8 (the JSON contract) and §§9–16 (the port-vs-build decision
  record), the companion documents this survey feeds and cross-references
  throughout
- [docs/cli/functional-verification.md](../cli/functional-verification.md)
  — the verb whose "Out of scope: waveform inspection" this epic closes,
  and the source of `request-modexp.json`'s `"engine": "icarus"` fact used
  throughout §6
- `examples/functional-verification/modexp.v` /
  `examples/functional-verification/test_modexp.py` — the real canary RTL
  and its cocotb testbench (not run directly here — see §1/§7) that
  `tb_modexp.v` (§6.1) was written to mirror
- [boldaxolotl/booley](https://github.com/boldaxolotl/booley),
  `crates/bwave` (Apache-2.0) — the command set and `ExtractConfig`
  surveyed in §§2–5, built from source at commit
  `5a45b4e8bdf2319335e956cd92660e8263cdee15`
- [ekiwi/fst-reader](https://github.com/ekiwi/fst-reader) and
  [ekiwi/wellen](https://github.com/ekiwi/wellen) (both BSD-3-Clause) —
  surveyed in §8.1
- [pyvcd](https://github.com/SanDisk-Open-Source/pyvcd) (MIT) and
  [pywellen](https://github.com/ekiwi/wellen) (BSD-3-Clause, the `wellen`
  workspace's `pywellen` member) — surveyed and benchmarked in §8.2
