"""cocotb testbench for `gcd.v` -- the worked example from
`docs/design/cocotb-verification-spike.md` section 6, verbatim.

Three `@cocotb.test()` functions, one of them deliberately failing, so the
example reproduces that survey's own live-captured `TESTS=3 PASS=2 FAIL=1
SKIP=0` outcome against **both** `engine: "icarus"` and `engine:
"verilator"` -- see `docs/cli/functional-verification.md`.

This file is *input* to `klt functional-verification` (the testbench module
named by `request.testbench.module`), not a pytest module -- pytest never
collects it, since it takes a cocotb-injected `dut` argument and only runs
inside a simulator process. `tests/test_functional_verification.py` drives it
through the verb instead.
"""

import math
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge


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

    Exists so this example produces a real *failing* test alongside two
    passing ones -- gcd(48, 18) is genuinely 6; this test insists on 999 and
    is expected to fail every time, exercising the verb's `status: "fail"` /
    exit `3` path end to end.
    """
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)

    got = await run_gcd(dut, 48, 18)
    want = 999  # deliberately wrong -- true gcd(48, 18) is 6
    assert got == want, f"gcd(48, 18): got {got}, want {want} (deliberate failure)"
