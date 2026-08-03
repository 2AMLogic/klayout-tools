# Spike: cocotb/Verilator/Icarus functional-verification survey

**Status:** spike / proposal. Nothing here is scheduled, and nothing here
authorises implementation. Per
[docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "How capabilities arrive," a
major capability arrives by spiking a design epic first — candidate-engine
survey, proposed JSON contract, wrap/build decision — and this document is
Phase 1 of Epic #391 ("adopt the digital engine class — Yosys + OpenROAD —
RTL→GDS as a first-class `klt` flow"), specifically issue #398: the
cocotb/Verilator/Icarus survey behind `klt`'s future `functional-verification`
verb. Its sibling Phase 1 surveys are #396 (Yosys/synthesis) and #397
(OpenROAD/place-and-route); #399 synthesizes all three into the epic's
contract set. It follows the same wrap/build decision-record structure the
prior accepted spikes use — most directly
[docs/design/lvs-extraction-spike.md](lvs-extraction-spike.md) (candidate
survey → contract → wrap/build decision).

**Forcing function:** marketing#56, the first digital canary (GCD/RSA
modexp on sky130, operator ruling 2026-08-03). This capability is also the
hard gate in #387's scored-gate `valid` field ("functional verification —
testbench + design → pass/fail + coverage. Engine: cocotb/Verilator/Icarus.
This is the hard gate in #387's `valid` field.") — a design that doesn't
pass functional verification must never score as `valid: true`, regardless
of how good its area/timing numbers look. That single sentence is the load-
bearing constraint on everything below: whatever "pass/fail" means at this
verb's contract boundary must collapse cleanly into #387's `valid: bool` +
`0`/`1`/`3` exit-code convention (already shipped, PR #403/#387), not invent
a new, richer taxonomy that gate has to special-case.

**Everything below was run, not recalled.** cocotb 2.0.1, Verilator 5.050,
and Icarus Verilog 13.0 (stable, v13_0) were installed locally
(Homebrew-provided `verilator`/`icarus-verilog`, cocotb via `pip install
cocotb` into a Python 3.13 venv — cocotb 2.0.1 caps at Python ≤3.13, see
"Version and licensing" below) and driven directly for §6's worked example;
raw captured output is quoted, not paraphrased. License/activity data was
fetched live via the GitHub REST API and each project's own README/COPYING
text, the same verification discipline
[docs/design/lvs-extraction-spike.md](lvs-extraction-spike.md) §1 and
[docs/design/layout-generator-spike.md](layout-generator-spike.md) §1 used.

## 1. Candidate-engine survey

### cocotb (the harness)

| Property | Finding |
| -------- | ------- |
| Upstream | [`cocotb/cocotb`](https://github.com/cocotb/cocotb) — verified live: `pushed_at: 2026-08-03`, not archived, license `BSD-3-Clause` (GitHub license detector, matching `pip show cocotb`'s own `License: BSD-3-Clause`). Fully compatible with this repo's MIT posture — permissive, no copyleft obligation, embeddable as a runtime dependency without constraining `klt`'s own license. |
| What it is | A Python coroutine-based cosimulation library: the RTL runs in an HDL simulator (Verilator, Icarus, or several commercial simulators); the testbench is ordinary Python (`async def` coroutines decorated `@cocotb.test()`), driving/sampling the DUT's signals through a VPI/FLI/VHPI bridge the simulator loads as a shared library. `klt` never needs its own HDL parser or event scheduler — cocotb and the simulator under it do that work; `klt` only needs to generate the request, invoke the run, and structure the result. |
| Version constraint (load-bearing) | **cocotb 2.0.1 requires Python ≤3.13.** Installing into a Python 3.14 venv fails outright: `RuntimeError: cocotb 2.0.1 only supports a maximum Python version of 3.13. You can suppress this error by defining the environment variable COCOTB_IGNORE_PYTHON_REQUIRES. There is no guarantee this will work and no support will be provided.` (verified live, `pip install cocotb` under a 3.14 venv). This repo's own `pyproject.toml` targets Python 3.10+ with no upper pin; a `klt`-bundled cocotb dependency would need either an explicit upper bound coordinated with this cap, or tracking cocotb's own 3.14 support as it lands — a Phase 3 implementation concern, not a blocker to this spike, but worth flagging now so Phase 3 doesn't rediscover it mid-build. |
| Harness API surface used below | `cocotb.test()` (test registration), `cocotb.clock.Clock` (clock generation), `cocotb.triggers.RisingEdge`/`ClockCycles` (synchronization), plain `dut.<signal>.value` reads/writes (signal access) — all present and used in §6's worked example exactly as documented. |
| Official Python `Runner` API (cocotb ≥ 2.0) | `cocotb_tools.runner` ships `get_runner(simulator_name)` → a `Runner` with `.build(...)` and `.test(...)` methods, plus a `get_results(results_xml) -> (num_tests, num_failures)` helper — a first-party, in-process/subprocess Python invocation surface that supersedes the historical Makefile-only flow. See §3 — this is the invocation surface this spike recommends `klt` call. |
| Third-party `cocotb-test` (also considered) | [`cocotb-test`](https://pypi.org/project/cocotb-test/) (BSD, by an independent maintainer, `pip show` confirms `Requires: cocotb, find_libpython, pytest`) is an older pytest-plugin wrapper that predates cocotb 2.0's own `Runner` API and does substantially the same job (drive build+test from Python instead of `make`). Since cocotb now ships this capability itself, `cocotb-test` is a redundant extra dependency for `klt` to pull in — noted here because the issue body itself asks for it by name, but **not recommended**: the first-party `cocotb_tools.runner` is the same idea with no extra package and no extra pytest coupling. |

### Verilator (simulator backend candidate)

| Property | Finding |
| -------- | ------- |
| Upstream | [`verilator/verilator`](https://github.com/verilator/verilator) — verified live: `pushed_at: 2026-08-03`, not archived. GitHub's license detector reports `NOASSERTION` because Verilator is **dual-licensed**; its own `README.rst` states this explicitly in an SPDX header: `SPDX-License-Identifier: LGPL-3.0-only OR Artistic-2.0`. Either license is permissive enough for `klt` to invoke as a subprocess/shared-library dependency without imposing terms on `klt`'s own MIT code (same posture this repo already takes toward KLayout's GPL-3.0 as an invoked dependency, per `docs/cli/drc.md` → "Engine"). |
| Local version | `Verilator 5.050 2026-07-01` (Homebrew bottle, verified via `verilator --version`). |
| What it is | A cycle-accurate Verilog/SystemVerilog **compiler**, not an interpreter: it translates RTL into C++ (or SystemC), which is then compiled into a native executable/shared library that cocotb's VPI bridge loads. This compile step is the source of both its speed advantage (near-native simulation speed once built) and its CI-cost tradeoff (a real `g++`/`clang++` build on every fresh invocation — see the measured numbers below). |
| Coverage | **Real, structured, and machine-parseable — verified live in §6.** `--coverage` (build arg) instruments the compiled model; a run emits `coverage.dat`. `verilator_coverage --report summary <file>` gives line/toggle/branch/expr percentages; `verilator_coverage --write-info merged.info <file>` emits a standard **lcov `.info`** file — a well-known, widely-tooled format (the `lcov`/`genhtml` ecosystem, and multiple existing Python parsers), not a Verilator-proprietary one. This is the strongest single differentiator from Icarus for the coverage half of this survey's mandate. |
| Measured build cost | `python run_via_python_api.py verilator` (build `--coverage --trace` + run all 3 testcases) took **8.19 s wall** (`user 23.15s`, multi-threaded compile) on this machine, vs. Icarus's **0.72 s wall** for the equivalent build+run — see "CI-friendliness tradeoff" below. |

### Icarus Verilog (simulator backend candidate)

| Property | Finding |
| -------- | ------- |
| Upstream | [`steveicarus/iverilog`](https://github.com/steveicarus/iverilog) — verified live: `pushed_at: 2026-08-03`, not archived, license `GPL-2.0` (GitHub detector, matching the project's own `COPYING` file). GPL-2.0 is fine to invoke as a separate subprocess (same posture this repo already takes toward netgen/magic-class GPL tools discussed as *oracles*, not runtime-linked code, in [docs/design/lvs-extraction-spike.md](lvs-extraction-spike.md) §1) — it is never linked into `klt`'s own process. |
| Local version | `Icarus Verilog version 13.0 (stable) (v13_0)` (Homebrew bottle, verified via `iverilog -V`). |
| What it is | An **interpreter**: `iverilog` compiles Verilog into a bytecode-like intermediate (`.vvp`) that `vvp` interprets at runtime — no native compile step. This is the direct inverse of Verilator's tradeoff: no build-time cost, but slower simulation throughput for anything long-running. |
| Coverage | **None, through this flow — verified by absence.** A full cocotb run against Icarus (`sim_build_icarus/`) produces no coverage artifact of any kind; `iverilog -h` has no coverage-related flag; there is no bundled Icarus-native coverage tool comparable to `verilator_coverage`. (A separate third-party tool, `covered`, has historically paired with Icarus for coverage, but it is a distinct, unmaintained-looking project outside this survey's scope — not evaluated here because Verilator's built-in path already satisfies the coverage requirement without a second tool.) |
| Measured cost | `python run_via_python_api.py icarus` (build + run all 3 testcases): **0.72 s wall** — no native compile step, so "build" is closer to instantaneous than Verilator's. |

### Also considered and set aside

- **cocotb-coverage** (separate pip package, `pip index versions` confirms it
  exists at `2.0`) — a constrained-random *functional*-coverage model
  library (coverage bins/crosses defined in the testbench itself), orthogonal
  to Verilator's *structural* (line/toggle/branch) coverage. Genuinely
  useful for a later, richer verification story, but out of scope for this
  spike's minimal contract — noted as a forward-compatible addition, not
  evaluated further.
- **UVM-Python / pyuvm** — a Python reimplementation of UVM's structure
  (sequences, agents, scoreboards) on top of cocotb. Real, but a much
  heavier methodology commitment than a single canary block needs; the
  right call *if* the epic's block library grows into something needing
  UVM-style reuse across many testbenches, not for the GCD/RSA modexp
  canary this spike must ground its worked example in.
- **Commercial simulators** (Xcelium, Questa, VCS) — `cocotb_tools.runner`
  supports them (`Xcelium`, `Questa`, `Vcs` classes exist in the module's own
  surface, per `dir(cocotb_tools.runner)`), but proprietary licensing puts
  them out by this repo's open-PDK/open-tooling rule (CLAUDE.md → "Open PDKs
  only," extended by convention to open tooling) — the same reasoning that
  excluded Calibre/HSPICE/Spectre-class tools from the LVS and SPICE spikes.

### Recommendation: both backends, selectable, Icarus as the default for CI cost

**Neither backend wins outright — this is a genuine "both, with stated
tradeoffs" case**, matching the shape the epic's own Phase 1 goal invites
("recommend one, or note when each applies"):

- **Icarus is the CI-friendly default.** No native-compile step means a
  fresh worked-example run costs ~0.7 s end to end (measured above) — for
  the small, cycle-cheap canary blocks this epic's Phase 3 targets first
  (GCD, RSA modexp on sky130), that is fast enough that Verilator's raw
  simulation-speed advantage never gets a chance to matter: the *build* cost
  dominates total wall time at this design size, and Icarus has none.
- **Verilator earns its keep once coverage is required, or once designs get
  big enough that compiled execution speed matters.** Its `--coverage` path
  is the only one of the two that produces line/toggle/branch data at all
  (§6 demonstrates this concretely), and its near-native execution speed
  wins decisively once a design runs enough cycles that Icarus's per-cycle
  interpretation overhead outweighs Verilator's one-time compile cost —
  which is exactly the regime a *design-space exploration* (many seeded P&R
  candidates × verification reruns, per #391's own "Elastic compute"
  framing) will eventually reach.
- **The CI-cost tradeoff, stated plainly per the curator's guidance**: a
  `klt`-driven CI running the worked example once per PR pays Icarus's ~0.7 s
  or Verilator's ~8 s — a real but small difference at this design size.
  The tradeoff **compounds** the moment functional verification runs
  *inside* a design-space-exploration loop (many candidates, per #391's
  Phase 6 framing) rather than once per PR: Verilator's fixed ~8 s per-
  candidate compile tax, multiplied across N candidates, is the kind of
  build-time-scales-with-N risk this repo's own builder guidance calls out
  explicitly — a real cost a later Phase 3/6 implementation issue must
  budget for, not dismiss as "only 8 seconds."

**Contract implication:** the proposed request shape in §7 carries an
explicit `engine: "icarus" | "verilator"` selector (mirroring `klt lvs`'s
`engine` field, [docs/design/lvs-extraction-spike.md](lvs-extraction-spike.md)
§2b) with `"icarus"` as the documented default, and a separate opt-in
`coverage: true` flag that **requires** `engine: "verilator"` (Icarus has no
coverage path to request) — so a caller wanting coverage gets an explicit,
checkable requirement rather than a silent no-op.

## 2. Harness shape

A cocotb testbench is an ordinary Python module containing one or more
`async def` coroutines decorated `@cocotb.test()`. The shape used in §6's
worked example (`test_gcd.py`) is representative of the minimum viable
harness for any small synchronous block:

- **Clock generation**: `cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())`
  — a free-running background coroutine.
- **Reset sequencing**: an `async def reset(dut)` helper that drives
  `rst_n`/other control inputs to their reset values, waits a fixed number
  of `ClockCycles`, then deasserts reset synchronized to a `RisingEdge`.
- **Stimulus + response**: ordinary Python control flow (loops, `assert`)
  driving `dut.<signal>.value = <int>` and reading `dut.<signal>.value`
  back, synchronized via `await RisingEdge(dut.clk)` — no special DSL, no
  waveform-description language. This is the single biggest structural
  difference from a Verilog-only testbench: stimulus generation, the golden
  model, and the assertion all live in the same Python process and can call
  arbitrary Python (§6 checks the DUT's output against `math.gcd` directly).
- **Assertions are plain Python `assert`** (or a raised exception) inside the
  test coroutine — cocotb's regression manager catches whatever the
  coroutine raises and records that test as failed; there is no separate
  "assertion" object model to learn. `cocotb.result.TestFailure` — the
  exception type cocotb 1.x testbenches historically raised — **no longer
  exists in cocotb 2.0.1** (an `AttributeError: module 'cocotb' has no
  attribute 'result'` was hit live while developing §6's testbench before
  switching to a plain `assert`/`RuntimeError`); a klt-generated testbench
  template must not rely on now-removed cocotb 1.x APIs.
- **Multiple independent `@cocotb.test()` functions per module** are
  cocotb's native way to express "several test cases" — the regression
  manager runs each as its own labeled scenario in one simulator session,
  each with its own pass/fail/skip outcome in the results report (§4).
  `klt`'s contract does not need to invent a "test case" concept; cocotb
  already has one, and it is exactly the granularity the contract in §7
  reports per-entry.

## 3. Invocation surface

Two distinct invocation surfaces exist; **this spike recommends the newer
one**, not the one the issue names first:

### 3a. The classic Makefile flow (`make`-driven)

cocotb ships `Makefile.sim` fragments (`cocotb-config --makefiles`) that a
project's own `Makefile` `include`s, setting `SIM`, `VERILOG_SOURCES`,
`TOPLEVEL`, `MODULE`, `TESTCASE` as Make variables. Invocation is
`make SIM=icarus` (or `SIM=verilator`). This is what most cocotb tutorials
and existing open-source testbenches use today, and is what the issue body's
"typically via a Makefile-driven flow" describes. **Working, but not
recommended for `klt` to generate and template**: it means `klt` owns a
generated `Makefile` (a second artifact to keep in sync with cocotb's own
`Makefile.sim` internals across cocotb version bumps), and — observed
live in §6 — **the wrapping `make` process's own exit code is not a
reliable pass/fail signal** (see §4).

### 3b. The Python `Runner` API (`cocotb_tools.runner`) — recommended

cocotb 2.0 ships a first-party, programmatic alternative:

```python
from pathlib import Path
from cocotb_tools.runner import get_results, get_runner

runner = get_runner("icarus")  # or "verilator"
runner.build(
    verilog_sources=[Path("gcd.v")],
    hdl_toplevel="gcd",
    build_dir=Path("sim_build"),
    always=True,
    timescale=("1ns", "1ps"),  # required for icarus -- see the gotcha below
)
results_xml = runner.test(
    test_module="test_gcd",
    hdl_toplevel="gcd",
    hdl_toplevel_lang="verilog",
    testcase=None,  # None = run every @cocotb.test in the module
    build_dir=Path("sim_build"),
    results_xml="results.xml",
    timescale=("1ns", "1ps"),
)
num_tests, num_failures = get_results(results_xml)
```

This is the invocation surface a `klt functional-verification` verb should
actually call — the same posture `klt drc`/`klt render`/`klt extract`
already take toward `klayout.db` (call the library in-process, don't shell
out and re-parse a subprocess's stdout): `Runner.build()`/`Runner.test()`
are ordinary Python calls, `get_results()` returns structured data (a
2-tuple), and there is no generated Makefile for `klt` to own or keep in
sync. What `klt` still needs to generate/template for a caller is:

- The **testbench module** itself (`test_module`) — this is the one piece
  cocotb cannot synthesize; §7's contract takes it as an input path (a
  human- or generator-authored Python file), not something `klt` writes.
- The **build/test parameter set** (`hdl_toplevel`, `verilog_sources`,
  `testcase` filter, `timescale`, `build_args`) — a thin, mechanical mapping
  from the request JSON's fields (§7) onto `Runner.build()`/`.test()`
  keyword arguments. No code generation of Verilog or Python is required;
  this is parameter marshalling, the same kind of thin adapter `klt sim`
  already writes for its own per-corner ngspice wrapper decks (spice-corner-
  runner-spike.md).

**One invocation gotcha, hit live and worth recording**: `Runner.test()`'s
`timescale` parameter alone is not sufficient for Icarus — the *build* step
also needs `timescale` passed to `Runner.build()`, or elaboration fails with
`ValueError: Bad \`period\`: Unable to accurately represent 10(ns) with the
simulator precision of 1e0` the moment the testbench's `Clock(..., unit="ns")`
tries to construct a 10 ns period against an unset (default 1 s) simulator
precision. A `klt`-generated invocation must set `timescale` on **both**
calls, not just `.test()`.

## 4. Pass/fail extraction

### The authoritative artifact: `results.xml`

Every cocotb run (Makefile or Runner-API path) emits a JUnit-like
`results.xml`. Two real, captured examples from §6:

**All tests passing:**

```xml
<testsuites name="results">
  <testsuite name="all" package="all">
    <property name="random_seed" value="1785780800" />
    <testcase name="test_gcd_known_pairs" classname="test_gcd" file="/private/tmp/gcd-cocotb-spike/test_gcd.py" lineno="45" time="0.005598783493041992" sim_time_ns="520.0" ratio_time="92877.31891155304" />
    <testcase name="test_gcd_random_pairs" classname="test_gcd" file="/private/tmp/gcd-cocotb-spike/test_gcd.py" lineno="58" time="0.03472900390625" sim_time_ns="4720.0" ratio_time="135909.45518453428" />
    <testcase name="test_gcd_deliberately_wrong_expectation" classname="test_gcd" file="/private/tmp/gcd-cocotb-spike/test_gcd.py" lineno="79" time="0" sim_time_ns="0" ratio_time="0">
      <skipped />
    </testcase>
  </testsuite>
</testsuites>
```

**With a deliberate failure** (`test_gcd_deliberately_wrong_expectation`
asserts `gcd(48, 18) == 999`, which the correctly-behaving RTL cannot
satisfy — see §6):

```xml
<testcase name="test_gcd_deliberately_wrong_expectation" classname="test_gcd" file="/private/tmp/gcd-cocotb-spike/test_gcd.py" lineno="79" time="0.0025670528411865234" sim_time_ns="120.0" ratio_time="46746.21342992477">
  <failure error_type="AssertionError" error_msg="gcd(48, 18): got 6, want 999 (deliberate failure)&#10;assert 6 == 999" />
</testcase>
```

This is the load-bearing structure for a `klt` contract: **a `<testcase>`
with no child is a pass; a `<failure>` child is a failure; a `<skipped>`
child is a skip** — cocotb's own recommended machine-readable output, no
log-text scraping required.

### `get_results()` is a convenience, not the full picture — verified live

`cocotb_tools.runner.get_results(results_xml) -> (num_tests, num_failures)`
parses exactly this file, but **`num_tests` counts every `<testcase>`
element, including skipped ones** — observed live running only
`test_gcd_known_pairs,test_gcd_random_pairs` (2 of the module's 3 tests):
`num_tests=3 num_failures=0`, not `num_tests=2`. A `klt` wrapper must parse
`results.xml` itself for a three-way passed/failed/skipped breakdown (§7's
`tests[]`) rather than relying on this 2-tuple as the authoritative count —
`get_results()` is fine as a fast valid/invalid gate (`num_failures == 0`),
not as the source of the per-test report.

### Exit codes are not a reliable signal on their own — verified live

Three invocation paths were run against the *same* deliberately-failing test
and produced three different raw exit codes:

| Invocation | Observed exit code | Why |
| ---------- | ------------------- | --- |
| `make SIM=icarus fail` (nested `make` target calling `make TESTCASE=...`) | `2` | Make's own convention: `make: *** [fail] Error 2` wraps a child `make`'s nonzero exit as its own distinct code — nesting depth changes the number. |
| `make -f Makefile results.xml TESTCASE=...` (single-level `make`) | `2` | `make: *** [results.xml] Error 1` — the *underlying* `vvp` process exited `1`; `make` itself reports `2`. |
| `python run_via_python_api.py icarus` (this spike's own wrapper script, exits `1` deliberately when `num_failures != 0`) | `1` | This script's own explicit `sys.exit(0 if num_failures == 0 else 1)` — i.e., the *only* reliable exit code in this table is the one a caller computes itself from parsed `results.xml`/`get_results()`, not one it trusts from the underlying `make`/simulator process. |

**Conclusion, stated as a contract requirement**: `klt`'s
functional-verification verb must **never treat a raw subprocess/`make`
exit code as ground truth**. It must always invoke via the Python `Runner`
API (§3b) or, if a Makefile path is ever kept as a fallback, always parse
the resulting `results.xml` itself and compute exit codes deterministically
from that structured data — exactly as `klt`'s own `run_eval()`
(`src/klayout_tools/eval.py`, PR #403) never forwards an underlying tool's
exit code either; it inspects each check's own structured result and derives
`valid`/exit code itself.

## 5. Coverage extraction

Verified live in §6 (Verilator backend only — Icarus has no coverage path
through this flow, per §1):

- **Raw artifact**: `--coverage` (a `Runner.build()` `build_args` entry)
  produces `coverage.dat` next to the compiled model — a Verilator-internal
  binary-ish text format, not meant to be parsed directly.
- **Structured summary**: `verilator_coverage --report summary coverage.dat`
  prints per-category percentages:

  ```
  Coverage Summary:
    line      : 100.0% (  6/  6)
    toggle    : 60.0% (102/170)
    branch    : 100.0% (  4/  4)
    expr      : 100.0% (  5/  5)
    fsm_state : 0.0% (  0/  0)
    fsm_arc   : 0.0% (  0/  0)
  ```

  This is directly usable as a JSON `coverage` block's numeric fields
  (§7) with no further parsing invention needed — `verilator_coverage` has
  already done the aggregation; `klt` just needs to run this command and
  capture its stdout (or better, its `--write-info` output below, which is
  more robust to future summary-format changes than scraping this text
  table).
- **Portable artifact**: `verilator_coverage --write-info merged.info
  coverage.dat` emits a standard **lcov `.info`** file (`SF:`/`DA:`/`BRDA:`
  records — the exact format the widely-used `lcov`/`genhtml` toolchain
  consumes). This is the artifact §7's contract references by path
  (`coverage.info_path`) — a well-known, tool-agnostic format rather than a
  bespoke `klt` coverage schema, matching this repo's "artifacts are paths,
  not inlined blobs" discipline already established for `klt sim`'s
  waveforms and `klt extract`'s netlists.
- **Functional coverage (out of scope for this contract)**: the
  `cocotb-coverage` package (§1, "also considered") gives constrained-random
  functional-coverage bins defined *in the testbench*, a fundamentally
  different (and testbench-author-driven) kind of coverage than Verilator's
  structural line/toggle/branch data. Nothing in §7's contract precludes a
  future `coverage.functional` block reporting it; this spike does not
  design that block, matching the demand-driven "don't design speculative
  contract surface" discipline the LVS spike's parasitics addendum applied
  to its own out-of-scope items.

## 6. Worked example: a minimal cocotb testbench for a GCD core

Chosen because it is the *literal* forcing function
(marketing#56: "GCD/RSA modexp on sky130") — a GCD core is small enough to
verify in seconds and structurally representative of the kind of tiny
synchronous datapath this epic's canary targets first.

### The RTL (`gcd.v`) — an iterative-subtractor GCD core

```verilog
// Minimal iterative-subtractor GCD core.
// Handshake: assert start with a,b valid for one cycle; done pulses
// high for one cycle when result is valid.
module gcd #(
    parameter WIDTH = 16
) (
    input  wire             clk,
    input  wire             rst_n,
    input  wire             start,
    input  wire [WIDTH-1:0] a_in,
    input  wire [WIDTH-1:0] b_in,
    output reg              done,
    output reg  [WIDTH-1:0] result
);

  reg [WIDTH-1:0] a, b;
  reg busy;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      a      <= {WIDTH{1'b0}};
      b      <= {WIDTH{1'b0}};
      busy   <= 1'b0;
      done   <= 1'b0;
      result <= {WIDTH{1'b0}};
    end else begin
      done <= 1'b0;
      if (start && !busy) begin
        a    <= a_in;
        b    <= b_in;
        busy <= 1'b1;
      end else if (busy) begin
        if (a == {WIDTH{1'b0}}) begin
          result <= b;
          done   <= 1'b1;
          busy   <= 1'b0;
        end else if (b == {WIDTH{1'b0}}) begin
          result <= a;
          done   <= 1'b1;
          busy   <= 1'b0;
        end else if (a > b) begin
          a <= a - b;
        end else begin
          b <= b - a;
        end
      end
    end
  end

endmodule
```

**A real RTL bug, found and fixed while building this example**: the first
draft only checked `b == 0` for the exit condition (the textbook subtractive
Euclidean algorithm as usually stated). Run live against `cocotb` with an
`(a=0, b=7)` input, it **hung** — the `a == 0` branch is also required,
because when `a` is `0`, `a > b` is always false, so the RTL takes the
"`b <= b - a`" branch forever (`b - 0 == b`, an infinite no-op). This was
caught immediately by the worked example's own timeout guard (`RuntimeError:
gcd(0, 7) did not assert done within 200 cycles`) — a live, concrete
demonstration of exactly the kind of bug functional verification exists to
catch, not a hypothetical.

### The testbench (`test_gcd.py`)

```python
import math
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles


async def reset(dut):
    dut.rst_n.value = 0
    dut.start.value = 0
    dut.a_in.value = 0
    dut.b_in.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


async def run_gcd(dut, a, b, timeout_cycles=200):
    """Drive one start/done handshake; return the core's result."""
    await RisingEdge(dut.clk)
    dut.a_in.value = a
    dut.b_in.value = b
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0

    for _ in range(timeout_cycles):
        await RisingEdge(dut.clk)
        if dut.done.value == 1:
            return int(dut.result.value)
    raise RuntimeError(
        f"gcd({a}, {b}) did not assert done within {timeout_cycles} cycles"
    )


@cocotb.test()
async def test_gcd_known_pairs(dut):
    """Happy path: several (a, b) pairs checked against math.gcd."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)

    pairs = [(48, 18), (1071, 462), (17, 5), (100, 100), (0, 7), (7, 0)]
    for a, b in pairs:
        got = await run_gcd(dut, a, b)
        want = math.gcd(a, b)
        assert got == want, f"gcd({a}, {b}): got {got}, want {want}"


@cocotb.test()
async def test_gcd_random_pairs(dut):
    """Randomized cross-check against math.gcd, fixed seed for reproducibility."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)

    # Range is deliberately narrower than the 16-bit datapath: a subtractive
    # (non-modulo) GCD core takes O(max(a, b)) cycles worst case (e.g.
    # gcd(N, 1)), so a full 16-bit sweep needs a much larger per-call cycle
    # budget than this example's timeout_cycles -- itself a real finding
    # about this RTL style, not a testbench shortcut.
    rng = random.Random(0)
    for _ in range(20):
        a = rng.randint(0, 500)
        b = rng.randint(0, 500)
        got = await run_gcd(dut, a, b)
        want = math.gcd(a, b)
        assert got == want, f"gcd({a}, {b}): got {got}, want {want}"


@cocotb.test()
async def test_gcd_deliberately_wrong_expectation(dut):
    """Deliberately-failing case: asserts an expectation the RTL cannot meet.

    Exists to give this survey a real, captured *failing* run (see the
    Exit-codes / results.xml discussion above) -- gcd(48, 18) is genuinely 6;
    this test insists on 999 and is expected to fail every time.
    """
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)

    got = await run_gcd(dut, 48, 18)
    want = 999  # deliberately wrong -- true gcd(48, 18) is 6
    assert got == want, f"gcd(48, 18): got {got}, want {want} (deliberate failure)"
```

### Raw captured output — happy path (Icarus, via the Python `Runner` API)

```
     0.00ns INFO     cocotb                             Running on Icarus Verilog version 13.0 (stable)
     0.00ns INFO     cocotb                             Initialized cocotb v2.0.1 from .../site-packages/cocotb
     0.00ns INFO     cocotb.regression                  running test_gcd.test_gcd_known_pairs (1/2)
   520.00ns INFO     cocotb.regression                  test_gcd.test_gcd_known_pairs passed
   520.00ns INFO     cocotb.regression                  running test_gcd.test_gcd_random_pairs (2/2)
  5240.00ns INFO     cocotb.regression                  test_gcd.test_gcd_random_pairs passed
  5240.00ns INFO     cocotb.regression                  ****************************************************************************************
                                                        ** TEST                            STATUS  SIM TIME (ns)  REAL TIME (s)  RATIO (ns/s) **
                                                        ****************************************************************************************
                                                        ** test_gcd.test_gcd_known_pairs    PASS         520.00           0.01      92877.32  **
                                                        ** test_gcd.test_gcd_random_pairs   PASS        4720.00           0.03     135909.46  **
                                                        ****************************************************************************************
                                                        ** TESTS=2 PASS=2 FAIL=0 SKIP=0                 5240.00           0.04     123956.08  **
                                                        ****************************************************************************************
results_xml=/private/tmp/gcd-cocotb-spike/sim_build_icarus/results_icarus.xml
num_tests=3 num_failures=0
```

Process exit code (this spike's own `sys.exit(0 if num_failures == 0 else
1)` wrapper): **`0`**.

### Raw captured output — deliberate failure (Icarus)

```
   120.00ns WARNING  ..d_deliberately_wrong_expectation gcd(48, 18): got 6, want 999 (deliberate failure)
                                                        assert 6 == 999
                                                        Traceback (most recent call last):
                                                          File "test_gcd.py", line 95, in test_gcd_deliberately_wrong_expectation
                                                            assert got == want, f"gcd(48, 18): got {got}, want {want} (deliberate failure)"
                                                        AssertionError: gcd(48, 18): got 6, want 999 (deliberate failure)
                                                        assert 6 == 999
   120.00ns WARNING  cocotb.regression                  test_gcd.test_gcd_deliberately_wrong_expectation failed
   120.00ns INFO     cocotb.regression                  **********************************************************************************************************
                                                        ** TEST                                              STATUS  SIM TIME (ns)  REAL TIME (s)  RATIO (ns/s) **
                                                        **********************************************************************************************************
                                                        ** test_gcd.test_gcd_known_pairs                      PASS         520.00           0.00     105216.75  **
                                                        ** test_gcd.test_gcd_random_pairs                     PASS        4720.00           0.03     140084.17  **
                                                        ** test_gcd.test_gcd_deliberately_wrong_expectation   FAIL         130.00           0.00      64896.40  **
                                                        **********************************************************************************************************
                                                        ** TESTS=3 PASS=2 FAIL=1 SKIP=0                                   5370.00           0.05     106870.16  **
                                                        **********************************************************************************************************
results_xml=/private/tmp/gcd-cocotb-spike/sim_build_icarus/results_icarus.xml
num_tests=3 num_failures=1
```

Process exit code: **`1`**.

### Verilator backend — same testbench, unmodified, both outcomes reproduced

Running the identical `test_gcd.py` against `engine="verilator"` (build
args `["--coverage", "--trace"]`) reproduced the same pass/fail behaviour
byte-for-byte in substance (`TESTS=3 PASS=2 FAIL=1 SKIP=0`, same assertion
text) — confirming the testbench is genuinely simulator-agnostic, the whole
point of cocotb as the harness layer. Coverage output (Verilator only, per
§5):

```
Coverage Summary:
  line      : 100.0% (  6/  6)
  toggle    : 60.0% (102/170)
  branch    : 100.0% (  4/  4)
  expr      : 100.0% (  5/  5)
  fsm_state : 0.0% (  0/  0)
  fsm_arc   : 0.0% (  0/  0)
```

(Toggle coverage at 60% is expected and correct for this testbench: the
random test only exercises `a_in`/`b_in` values up to 500, far short of the
full 16-bit range, so many high-order bits of `a`/`b` never toggle — a real,
meaningful signal a designer would act on, not a testbench defect.)

### Measured build/run cost (the CI-friendliness tradeoff, §1)

| Backend | Wall time (build + run, 3 tests) |
| ------- | --------------------------------- |
| Icarus | 0.72 s |
| Verilator (with `--coverage --trace`) | 8.19 s |

## 7. Proposed JSON contract

Documented in the field-table style [docs/design/lvs-extraction-spike.md](lvs-extraction-spike.md)
§2 and [docs/cli/sim.md](../cli/sim.md) established. This is a **proposed**
shape for #399 to fold in alongside the synthesis (#396) and place-and-route
(#397) contracts — no `klt` subcommand, dependency, or code is added by this
spike.

```
klt functional-verification <request.json> [--format text|json]
```

Takes a request document, like `klt sim`/`klt lvs` — a testbench module path
plus RTL sources is richer than a flag line carries cleanly, and the
scope (which testcases to run, coverage on/off, which backend) is exactly
the kind of structured input `klt sim`'s `request.json` already models for
an analogous "run some sources through an engine, report structured
results" shape.

### Request

```json
{
  "schema": "klt.functional_verification.request/1",
  "engine": "icarus",
  "sources": ["gcd.v"],
  "hdl_toplevel": "gcd",
  "testbench": { "module": "test_gcd", "testcase": null },
  "options": {
    "coverage": false,
    "timescale": ["1ns", "1ps"]
  }
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema` | string | Request contract identifier + major version (matches the `klt lvs`/`klt gen`/`klt sim` request-side convention). |
| `engine` | string | `"icarus"` (default) or `"verilator"` — §1's recommendation, present from day one as data rather than a code path. |
| `sources` | array\<string\> | RTL source file paths passed to `Runner.build(verilog_sources=...)`. |
| `hdl_toplevel` | string | The DUT module name (`Runner.build`/`.test`'s `hdl_toplevel`). |
| `testbench.module` | string | The Python test module name (`Runner.test`'s `test_module`) — a human- or generator-authored file, not synthesized by this verb (§3b). |
| `testbench.testcase` | string \| array\<string\> \| null | Optional testcase-name filter (`Runner.test`'s `testcase`); `null` runs every `@cocotb.test()` in the module. |
| `options.coverage` | boolean | Requests Verilator's `--coverage`/`--trace` build args and the `verilator_coverage` post-processing pass (§5). **Requires `engine: "verilator"`** — an `error` (exit `1`) if set with `engine: "icarus"`, per §1's recommendation. |
| `options.timescale` | `[string, string]` | `[unit, precision]` passed to **both** `Runner.build()` and `Runner.test()` (the gotcha in §3b) — defaults to `["1ns", "1ps"]`. |

### Response

```json
{
  "schema_version": 1,
  "engine": "icarus",
  "hdl_toplevel": "gcd",
  "testbench": "test_gcd",
  "status": "pass",
  "test_count": 2,
  "passed_count": 2,
  "failed_count": 0,
  "skipped_count": 0,
  "tests": [
    { "name": "test_gcd_known_pairs", "status": "passed", "sim_time_ns": 520.0, "real_time_s": 0.0056 },
    { "name": "test_gcd_random_pairs", "status": "passed", "sim_time_ns": 4720.0, "real_time_s": 0.0347 }
  ],
  "coverage": null,
  "environment": {
    "engine": "icarus",
    "engine_version": "13.0",
    "cocotb_version": "2.0.1",
    "results_xml": ".klt/functional-verification/results.xml"
  }
}
```

On a run with a failure (`status: "fail"`, mirroring `klt lvs`'s
`"match"`/`"mismatch"` two-outcome split rather than inventing a third
"partial pass" state — see §8):

```json
{
  "schema_version": 1,
  "engine": "icarus",
  "hdl_toplevel": "gcd",
  "testbench": "test_gcd",
  "status": "fail",
  "test_count": 3,
  "passed_count": 2,
  "failed_count": 1,
  "skipped_count": 0,
  "tests": [
    { "name": "test_gcd_known_pairs", "status": "passed", "sim_time_ns": 520.0, "real_time_s": 0.0 },
    { "name": "test_gcd_random_pairs", "status": "passed", "sim_time_ns": 4720.0, "real_time_s": 0.0 },
    {
      "name": "test_gcd_deliberately_wrong_expectation",
      "status": "failed",
      "sim_time_ns": 130.0,
      "real_time_s": 0.0,
      "error_type": "AssertionError",
      "error_message": "gcd(48, 18): got 6, want 999 (deliberate failure)"
    }
  ],
  "coverage": null,
  "environment": {
    "engine": "icarus",
    "engine_version": "13.0",
    "cocotb_version": "2.0.1",
    "results_xml": ".klt/functional-verification/results.xml"
  }
}
```

With `options.coverage: true` (Verilator only), `coverage` is populated
instead of `null`:

```json
"coverage": {
  "line_pct": 100.0,
  "toggle_pct": 60.0,
  "branch_pct": 100.0,
  "expr_pct": 100.0,
  "info_path": ".klt/functional-verification/coverage.info"
}
```

##### Top-level fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema_version` | integer | Per-command version (per `docs/json-contract.md`). |
| `engine` | string | Echo of the request's `engine`. |
| `hdl_toplevel` / `testbench` | string | Echo of the request's DUT/testbench identifiers. |
| `status` | string | `"pass"` (`failed_count == 0`) or `"fail"` (`failed_count > 0`) — never `"error"` in-band (a failed-to-run request emits no envelope; see Exit codes). |
| `test_count` / `passed_count` / `failed_count` / `skipped_count` | integer | Derived directly from `results.xml`'s own `<testcase>`/`<failure>`/`<skipped>` structure (§4) — **not** `get_results()`'s raw 2-tuple, since that undercounts the passed/skipped distinction (§4). |
| `tests` | array\<object\> | One entry per cocotb `@cocotb.test()`, in the order cocotb ran them (cocotb's own run order is stable given a fixed `random_seed`, itself worth echoing in `environment` for reproducibility — an open question below). `status` is `"passed"`/`"failed"`/`"skipped"`; `error_type`/`error_message` present only when `status == "failed"`, taken verbatim from `results.xml`'s `<failure>` attributes. |
| `coverage` | object \| null | `null` when `options.coverage` was not requested (or the engine doesn't support it); populated per §5 when requested. `info_path` is a **file reference**, matching the "artifacts are paths, not inlined blobs" rule `klt sim`'s waveforms and `klt extract`'s netlists already follow. |
| `environment` | object | Reproducibility block (mirrors `klt lvs`'s `environment`): engine + cocotb + simulator versions, and the path to the raw `results.xml` this report was derived from — so a stored verdict can be re-checked against its own evidence. |

### Exit codes — deliberately reusing the `klt lvs`/`klt eval` two-outcome split

| Code | Meaning |
| ---- | ------- |
| `0` | All tests passed (`status: "pass"`). |
| `1` | Failed to run — bad request, missing/unparseable RTL sources, build/elaboration error (e.g. the timescale gotcha in §3b), simulator crash, no `results.xml` produced. |
| `2` | Usage error (missing argument, bad `--format`) — from argparse. |
| `3` | Ran successfully; at least one test failed (`status: "fail"`). |

This is the **same** trichotomy `klt lvs` uses (§8 explains why, in detail)
— not `klt sim`'s four-way split. A cocotb regression run has no analogue
of "this corner's *simulator itself* errored but others in the batch are
still trustworthy" (`klt sim`'s exit `4`): either the whole build+run
pipeline completed and produced a `results.xml` cocotb can report from (exit
`0` or `3`), or it didn't (exit `1`) — there is no partial-batch state to
distinguish, matching `klt lvs`'s reasoning for omitting a fourth code (its
own "no such third success state" note, lvs-extraction-spike.md §2b).

## 8. The pass/fail contract boundary, stated explicitly (AC #3)

**A single failing test fails the whole run.** `status` is `"pass"` only
when `failed_count == 0`; any nonzero `failed_count` collapses to
`status: "fail"`, exit `3`. There is no per-test partial-credit state at the
gate boundary — the `tests[]` array is where per-test granularity lives (an
agent debugging *why* a run failed reads `tests[]`), but the single field
that gates anything downstream is `status`/exit code, exactly the way
`klt drc`'s `status: "clean"|"violations"` and `klt lvs`'s
`status: "match"|"mismatch"` already work.

**This is a hard, explicit design constraint from PR #403's `klt eval`,
cited by number per the curator's guidance**: `klt eval`
(`src/klayout_tools/eval.py`, `src/klayout_tools/cli/eval_cmd.py`) already
generalizes drc/lvs/sim/layout-metrics into one descriptor-driven scored
gate with a fixed `0`/`1`/`3` exit-code convention — `0` = ran, `valid:
true`; `3` = ran, `valid: false`; `1` = failed to run at all. Its own
`eval_cmd.py` docstring states the discipline in exactly these terms: *"an
optimizer must never mistake exit 1 (crash) for exit 3 (a real, if bad,
score)."* This survey's proposed `functional-verification` contract (§7)
is built to satisfy that constraint without modification: `status: "pass"`
→ exit `0` → `valid: true` at the `klt eval` boundary; `status: "fail"` →
exit `3` → `valid: false`; anything that never produces a `results.xml` at
all → exit `1`, no envelope, `valid` is simply never reached (the run
itself is untrustworthy, not merely "bad"). A partial-pass-per-test-case
result (2 of 3 tests passing, as in §6's worked example) still collapses to
one boolean the moment it crosses into `klt eval`'s `gates[]` — `klt eval`
would report that gate as `status: "fail"`, contributing one `false` to the
overall `valid` computation, with the `tests[]` detail available in the
underlying `functional-verification` report for a human or agent that wants
to know *which* test failed, but never surfaced as a richer taxonomy at the
gate itself. This is the same collapsing discipline `klt lvs`'s
`mismatch_count`/`status` pair already applies to a list of structured
mismatches, and `klt sim`'s aggregate `status: "fail"` applies to a list of
per-corner measurement failures — this contract adds no new pattern, it
reuses one already shipped three times over.

## 9. Wrap or build?

Scored against [docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "Rewrite rule"'s
three-criteria test, the same way the LVS and generator spikes scored their
own engine choices:

1. **Bottleneck or ceiling — fails.** No `klt`-native functional-
   verification capability exists today; there is nothing yet to be a
   ceiling of. The friction recorded on marketing#56 and #391 is "no wrapped
   verb exists," not "cocotb/Verilator/Icarus are too slow" — the same
   "build the wrapper, not a rewrite" signal the LVS spike found for
   KLayout's extractor/comparer.
2. **Oracle exists — holds, trivially.** cocotb testbenches are inherently
   cross-checked against a reference the moment the testbench author writes
   one (§6 checks the RTL against Python's own `math.gcd` — a golden model
   in the same language as the harness, no separate oracle project needed).
   Running the *same* testbench against both Icarus and Verilator (§6) is
   itself a working differential oracle for "did I implement the contract's
   result-parsing correctly," independent of the RTL's own correctness.
3. **Unlock — fails today.** Nothing this contract requires is structurally
   impossible through cocotb + either wrapped simulator. A credible future
   counter-case is incremental/interactive verification inside an agent's
   edit loop (single-cycle waveform inspection, live signal probing) — a
   different capability than batch pass/fail + coverage, unmeasured today.

One of three (oracle only) — **recommendation: wrap cocotb + Verilator +
Icarus, exactly as named**, invoked through the Python `Runner` API (§3b),
never a generated Makefile. As with the LVS and SPICE spikes, "wrap" is the
partial answer: the engines (cocotb's coroutine scheduler, Verilator's
compiler, Icarus's interpreter) are the wrapped, unmodified dependency;
**the contract, the request/response marshalling onto `Runner.build()`/
`.test()`, the `results.xml` → structured `tests[]` parsing, the coverage
`.info` extraction, and the collapse into `klt eval`'s `valid` boundary are
the deliverable this repo builds and owns.**

## Out of scope for this spike

No dependency was added to `pyproject.toml`, no `klt` subcommand was added,
no verification code was written, and no MCP surface was touched. Phase 3 of
Epic #391 carries the build, gated on these findings. The worked example in
§6 lives entirely in this document (embedded source, not committed files) —
nothing under `src/klayout_tools/` or `examples/` changed as part of this
spike, per its own scope.

## Open questions for a follow-up implementation issue

- **Testbench provenance.** This spike takes `testbench.module` as a given
  input (§7) — Phase 3 (or a later canary-bring-up issue) must decide who
  authors it: hand-written per block, or eventually generator-assisted the
  way `klt gen` assists layout. Out of scope here; §3b only fixes that `klt`
  does not need to *synthesize* Verilog/Python to drive cocotb, which is the
  narrower question this spike had to answer.
- **`random_seed` reproducibility.** cocotb's own regression manager logs a
  `random_seed` (visible in every `results.xml` captured in §6,
  e.g. `1785780800`) but does not accept it back in as an input by default
  through the `Runner` API surveyed here — worth resolving whether `klt`'s
  request should pin `cocotb.seed` explicitly (via `extra_env={"COCOTB_RANDOM_SEED": ...}`,
  which `Runner.test()`'s own `seed` parameter exposes) so a stored
  verification result is exactly reproducible, the same reproducibility
  argument `klt sim`'s Monte-Carlo seeding and `klt lvs`'s `environment`
  hashes already make.
- **cocotb's Python-version ceiling (2.0.1 → Python ≤3.13, §1).** Needs a
  decision once Phase 3 pins `klt`'s own dependency bounds: an explicit
  upper pin coordinated with cocotb's own support window, or tracking
  cocotb's 3.14 support as it lands.
- **CI provisioning.** Like the sibling Yosys/OpenROAD surveys, this spike
  ran against locally-installed tools (Homebrew `verilator`/`icarus-verilog`,
  `pip install cocotb`); a reproducible CI install path (pinned apt/conda
  packages, or a vendored build) is a Phase 3 concern, not resolved here.
- **Coverage-driven test generation.** §5's Verilator toggle-coverage number
  (60%, correctly reflecting an intentionally narrow random-stimulus range)
  is a real signal an agent could act on to widen stimulus ranges
  automatically — a design-space-exploration use of coverage data this
  spike's contract exposes but does not itself close the loop on.
- **The GCD RTL bug found while building §6** (the missing `a == 0` exit
  condition) is a testbench-fixture artifact of this spike, not a shipped
  `klt` deck or example — it is not tracked as a separate bug, since the
  fixed version is what §6 documents; flagged here only so a reader
  comparing against an older draft of this file understands why the exit
  condition looks the way it does.

## Related

- #391 parent epic
- #396 Yosys invocation survey (sibling Phase 1 issue)
- #397 OpenROAD invocation survey (sibling Phase 1 issue)
- #399 Phase 1 contract-synthesis issue (this document's findings feed it)
- #387 single scored gate — the `valid`/exit-code discipline this contract
  is built to satisfy (PR #403 shipped it)
- marketing#56 — GCD/RSA modexp canary, this spike's worked-example forcing
  function
