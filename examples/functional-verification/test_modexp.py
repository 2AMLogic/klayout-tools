"""cocotb testbench for `modexp.v` -- a randomized, bit-exact cross-check of
the RSA-style modular-exponentiation accelerator against Python's own
`pow(base, exp, mod)`.

Unlike `test_gcd.py` (which ships a deliberately-failing case to reproduce a
`TESTS=3 PASS=2 FAIL=1` transcript), every test here is expected to PASS: this
is the trustworthy correctness net Phase 1 of Epic marketing#56 calls for
before any gate-count optimization begins.

The testbench is **width-adaptive**: it reads the DUT's operand width from
`len(dut.result)` and constrains every random stimulus to that width, so the
identical module verifies a 4-, 6-, 8-, or 16-bit elaboration of `modexp`
without edits. (The `klt functional-verification` request does not yet plumb a
Verilog parameter override, so the checked-in request elaborates the default
`WIDTH=16`; the same design's bit-exactness at WIDTH=4/6/8/16 is additionally
pinned by the deterministic Icarus cross-check recorded in
`docs/design/rsa-modexp-baseline.md`.)

This file is *input* to `klt functional-verification` (the testbench module
named by `request.testbench.module`), not a pytest module -- pytest never
collects it, since it takes a cocotb-injected `dut` argument and only runs
inside a simulator process.
"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge


def _width(dut):
    """Operand width of this elaboration, read from the result port."""
    return len(dut.result)


async def reset(dut):
    dut.rst_n.value = 0
    dut.start.value = 0
    dut.base_in.value = 0
    dut.exp_in.value = 0
    dut.mod_in.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


async def run_modexp(dut, base, exp, mod, timeout_cycles=20000):
    """Drive one start/done handshake; return the core's result.

    The cycle budget is generous: the core is O(WIDTH^2) -- WIDTH exponent
    steps, each up to two WIDTH-bit interleaved modular multiplies -- so a
    16-bit operand needs a few hundred cycles worst case.
    """
    await RisingEdge(dut.clk)
    dut.base_in.value = base
    dut.exp_in.value = exp
    dut.mod_in.value = mod
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0

    for _ in range(timeout_cycles):
        await RisingEdge(dut.clk)
        if dut.done.value == 1:
            return int(dut.result.value)
    raise RuntimeError(
        f"modexp({base}, {exp}, {mod}) did not assert done within "
        f"{timeout_cycles} cycles"
    )


@cocotb.test()
async def test_modexp_known_vectors(dut):
    """Directed vectors, including the exponent/base edge cases."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)

    # (base, exp, mod) -- all satisfy the design precondition base < mod, mod > 1.
    vectors = [
        (2, 10, 1000),  # 1024 mod 1000 == 24
        (3, 7, 100),  # 2187 mod 100 == 87
        (0, 0, 7),  # base**0 == 1
        (5, 0, 7),  # anything**0 == 1
        (0, 5, 7),  # 0**5 == 0
        (7, 13, 11),  # classic small RSA-ish vector
        (6, 9, 13),
    ]
    for base, exp, mod in vectors:
        got = await run_modexp(dut, base, exp, mod)
        want = pow(base, exp, mod)
        assert got == want, f"modexp({base},{exp},{mod}): got {got}, want {want}"


@cocotb.test()
async def test_modexp_random(dut):
    """Randomized cross-check against Python `pow`, fixed seed for reproducibility.

    base is drawn in [0, mod-1] (the standard RSA message constraint the core
    assumes); mod in [2, 2**WIDTH-1]; exp across the full operand width.
    """
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)

    width = _width(dut)
    hi = (1 << width) - 1
    rng = random.Random(0)
    for _ in range(40):
        mod = rng.randint(2, hi)
        base = rng.randint(0, mod - 1)
        exp = rng.randint(0, hi)
        got = await run_modexp(dut, base, exp, mod)
        want = pow(base, exp, mod)
        assert got == want, f"modexp({base},{exp},{mod}): got {got}, want {want}"
