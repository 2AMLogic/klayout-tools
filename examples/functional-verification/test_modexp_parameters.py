"""cocotb testbench demonstrating `request.parameters` overriding
`modexp.v`'s `WIDTH` parameter at elaboration time (issue #610).

Reuses `test_modexp.py`'s own helpers (`reset`, `run_modexp`, `_width`) --
this file is not a copy of that testbench, only a companion that adds one
extra test asserting the override actually reached the simulator (not just
the request/response envelope), then re-runs the same width-adaptive
randomized cross-check `test_modexp.py`'s own `test_modexp_random` performs.

This file is *input* to `klt functional-verification`
(`request-modexp-parameters.json`'s `testbench.module`), not a pytest module
-- pytest never collects it (see `pyproject.toml`'s `testpaths = ["tests"]`),
since it takes a cocotb-injected `dut` argument and only runs inside a
simulator process.
"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge
from test_modexp import _width, reset, run_modexp

#: The `WIDTH` value `request-modexp-parameters.json`'s `parameters` field
#: requests -- `modexp.v`'s own RTL default is 16, so an elaboration at this
#: width is proof the override reached the simulator, not merely echoed back
#: unread in the response envelope.
EXPECTED_WIDTH = 8


@cocotb.test()
async def test_modexp_parameter_override_elaborates_at_the_requested_width(dut):
    """`request.parameters: {"WIDTH": 8}` must reach elaboration: the DUT's
    own `result` port width reflects the override, not `modexp.v`'s
    `WIDTH=16` RTL default -- the acceptance criterion issue #610 itself
    calls out ("confirm the elaborated design reflects the override")."""
    actual_width = _width(dut)
    assert actual_width == EXPECTED_WIDTH, (
        f"expected an elaboration at WIDTH={EXPECTED_WIDTH} (per "
        f"request.parameters), got {actual_width} bits -- the parameter "
        "override did not reach the simulator"
    )

    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    await RisingEdge(dut.clk)

    # The same width-adaptive randomized cross-check `test_modexp.py`'s own
    # `test_modexp_random` performs, bit-exact against Python `pow`, now
    # exercised at the overridden width rather than the RTL default.
    hi = (1 << actual_width) - 1
    rng = random.Random(0)
    for _ in range(10):
        mod = rng.randint(2, hi)
        base = rng.randint(0, mod - 1)
        exp = rng.randint(0, hi)
        got = await run_modexp(dut, base, exp, mod)
        want = pow(base, exp, mod)
        assert got == want, f"modexp({base},{exp},{mod}): got {got}, want {want}"
