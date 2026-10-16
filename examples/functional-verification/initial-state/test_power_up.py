"""cocotb testbench for the `options.initial_state` worked example.

"From the reset pin alone, with every flop starting at an arbitrary value,
does the design reach a defined state?" The testbench asserts reset from time
0 and never touches the flops' power-up value itself -- `klt
functional-verification` drives it (X, or a recorded seed) before this runs.
Input to the verb, not a pytest module.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles


async def start(dut):
    dut.rst_n.value = 0
    dut.d_rst.value = 0
    dut.d_free.value = 0
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await ClockCycles(dut.clk, 4)


@cocotb.test()
async def reset_alone_defines_the_reset_flop(dut):
    await start(dut)
    assert dut.q_rst.value.is_resolvable, "q_rst still unknown under reset"
    assert dut.q_rst.value == 0


@cocotb.test()
async def unreset_flop_needs_clocking_to_become_defined(dut):
    await start(dut)
    dut.d_free.value = 1
    await ClockCycles(dut.clk, 2)
    assert dut.q_free.value.is_resolvable
    assert dut.q_free.value == 1
