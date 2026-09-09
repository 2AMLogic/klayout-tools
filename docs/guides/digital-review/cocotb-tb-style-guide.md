# Cocotb testbench style guide

> **Ported content, Apache-2.0.** Ported from
> [boldaxolotl/booley](https://github.com/boldaxolotl/booley)'s
> `src/booley/data/refs/cocotb_tb_style_guide.md`
> ([Apache License 2.0](https://github.com/boldaxolotl/booley/blob/main/LICENSE)),
> commit [`1fdc706e`](https://github.com/boldaxolotl/booley/commit/1fdc706e2c54c3b9f80ee4a9472e2889a83d722f),
> fetched 2026-09-09 — see [`NOTICE`](NOTICE) for the full attribution
> record. Reworded from booley's own Flow/Target/Ticket/Specialist
> vocabulary, `.core`/`tests.toml` file plumbing, and its pinned-sandbox
> assumptions into this repo's `klt` verbs and request/response fields; the
> style content carries over unchanged in substance. See
> [`README.md`](README.md) for when this guide applies.

Canonical style guide for cocotb (Python) testbenches that
`klt functional-verification` runs (see
[`docs/cli/functional-verification.md`](../../cli/functional-verification.md)):
`request.testbench.module` is the test module; `request.hdl_toplevel` is
whatever the testbench attaches to — the DUT itself for a simple design,
with no HDL wrapper; or a thin HDL wrapper that instantiates the DUT when
its ports are SystemVerilog interfaces, since cocotb's bus interfaces bind
to interface *instances* and something has to instantiate them. Either way
the verdict comes from the run's `results_<engine>.xml`, never from
printed sentinels — see "Never trusting an exit code" in the
functional-verification doc.

A canary's own `CLAUDE.md` may layer additional project-specific overlays
on top of this guide. [`tb-style-guide.md`](tb-style-guide.md) (the
SystemVerilog guide) does not apply to Python testbenches.

---

## 1. Module layout

One test module per request (`request.testbench.module`, resolved per
`docs/cli/functional-verification.md`'s "testbench.module" /
"testbench.search_path"). One test = one named `@cocotb.test()` function;
if a canary maintains its own test-selection list (in CI config, or via
`request.testbench.testcase`), it should list exactly those function
names.

```python
# tb/test_counter.py
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from helpers.model import expected_after  # multi-file helpers: a package
# dir resolved via testbench.search_path


async def init(dut):
    """Shared bring-up: clock, reset, defaults."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.en.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


@cocotb.test()
async def test_reset(dut):
    """One behavior per test: reset clears the counter."""
    await init(dut)
    assert int(dut.count.value) == 0
```

- **One test per behavior.** Small, named `@cocotb.test()` coroutines —
  the test name is the report unit (`tests[].name` in the JSON response),
  the selection unit (`request.testbench.testcase`), and the criterion
  evidence. No mega-test that checks everything.
- **`async def` + `@cocotb.test()` only.** Dynamically-generated test
  names (e.g. via cocotb's `TestFactory`) drift from any explicit
  `testbench.testcase` filter list and break selection — see
  [`cocotb-tb-review.md`](cocotb-tb-review.md) check #1.
- **The `dut` handle IS the toplevel.** Signals are `dut.<port>`; internal
  hierarchy is `dut.<instance>.<signal>`. Keep hierarchy reaches shallow —
  deep pokes couple the TB to implementation detail.

## 2. Verdicts — assert, never print

The run's `results_<engine>.xml` is the verdict source (see "Never
trusting an exit code" in
[`docs/cli/functional-verification.md`](../../cli/functional-verification.md)).
A test passes when its coroutine returns, fails when it raises (an
`assert` is the idiomatic raise).

- **No sentinel prints.** `[SIM_RESULT] PASSED` / `ALL TESTS PASSED`
  strings do nothing under `klt functional-verification` (only
  `results.xml` is read) and mislead readers.
- **Assert with context**: `assert got == want, f"count={got}, expected
  {want}"` — the message lands verbatim in the per-test
  `tests[].error_message` field of the JSON response.
- RTL-side `$error`/`$fatal`/SVA failures are still counted from the sim
  output and fail the batch even when every Python test passed — don't
  duplicate RTL assertions in Python.

## 3. Clock, reset, and timing

- Start clocks with `cocotb.start_soon(Clock(...).start())` in a shared
  `init()`; never hand-toggle in a loop.
- Reset synchronously and wait a settle edge before driving stimulus (see
  the layout example) so every test starts from a known state — a
  `request.testbench.testcase` selection of multiple tests still shares
  one sim process (batched execution), and tests MUST NOT depend on
  running order or residual state.
- **Await sim-time triggers, never wall-clock sleeps.** `time.sleep()` /
  blocking I/O stalls the whole simulator; use `Timer`, `ClockCycles`,
  `RisingEdge`, `with_timeout`.
- **Bound every wait.** An unbounded `await` on a condition that never
  comes burns the whole wall-clock budget; wrap handshakes in
  `cocotb.triggers.with_timeout(trigger, n, "ns")`.

## 4. Bus functional models — use `cocotbext-*`, don't hand-roll

Prefer the widely-used `cocotbext-axi`/`cocotbext-uart` packages over
hand-rolled drivers/monitors when the canary's Python environment
provides them:

```python
from cocotbext.axi import AxiLiteBus, AxiLiteMaster

axil = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "s_axil"), dut.clk, dut.rst)
await axil.write_dword(0x0000, 0x1234_5678)
```

- A testbench can only import what the canary's Python environment
  provides or vendors — there is no network access assumption baked into
  `klt functional-verification` itself. As of this port (2026-09-09) there
  is no cocotb-2.x-compatible `cocotbext-spi` release — vendor an SPI BFM
  in the canary's own tree when an SPI interface needs one, rather than
  pinning to an unmaintained pre-2.x release.
- **Vendored-model layout:** place vendored Python (BFMs, golden models)
  under `testbench.search_path` (see
  [`docs/cli/functional-verification.md`](../../cli/functional-verification.md))
  alongside the test module, preserving normal Python package layout
  (e.g. `spi_bfm/__init__.py`) so it imports the same way any other
  package on `sys.path` would.

## 5. Golden references and comparison discipline

Same bar as the SV guide: every stimulus needs a checked expectation.

- Derive expected values from an *independent* model (a Python function, a
  numpy computation, a vendored golden model) — never from the DUT signal
  being checked.
- Compare on every transaction/sample, not only at end-of-test; accumulate
  and assert per item so the failure names the first mismatch.
- Convert handle values explicitly (`int(dut.count.value)`) before
  comparing — `LogicArray` equality with Python ints has resolution
  pitfalls around `X`/`Z`; deciding how `X` should compare is part of the
  test's contract.

## 6. Tracing

`klt functional-verification` does not yet expose a standalone waveform
flag outside of `options.coverage: true`, which adds Verilator's
`--trace` as a side effect of its coverage recipe (see "Coverage" in
[`docs/cli/functional-verification.md`](../../cli/functional-verification.md));
a future `klt wave` build step
([`docs/design/waveform-query-contract-spike.md`](../../design/waveform-query-contract-spike.md),
epic #1585) is the intended general-purpose path once it ships. Until
then: **never call `$dumpvars`-equivalents or write VCDs from Python
yourself** — let whichever mechanism owns the run's trace lifecycle own
it, so a TB-authored dump does not collide with it.

## 7. Test selection: `testbench.testcase`

Every `@cocotb.test()` meant to gate a criterion must be reachable by its
exact function name. If a canary restricts which tests gate a result via
`request.testbench.testcase` (a string or array of test names — see
"Request" in
[`docs/cli/functional-verification.md`](../../cli/functional-verification.md)),
every name listed there must match an actual `@cocotb.test()` function; a
filtered-out test still appears in the report as `"skipped"` (not
silently dropped), but a misspelled name in the filter list never runs
and never appears as `"passed"` either — this is the klt-side analogue of
the false-pass risk [`cocotb-tb-review.md`](cocotb-tb-review.md) check #1
names.

```json
{
  "testbench": {
    "module": "test_gcd",
    "testcase": ["test_reset", "test_count", "test_overflow"]
  }
}
```

Omitting `testcase` (or setting it `null`) runs every `@cocotb.test()` in
the module — the safer default when there is no reason to restrict the
set.
