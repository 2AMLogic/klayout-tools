# `options.initial_state` — gate-level power-up state

A tiny flat gate-level netlist (`netlist.v`: one async-reset flop, one flop
with no reset) over sky130-shaped cell models with real UDP state
(`cell_models.v`), and a cocotb testbench (`test_power_up.py`) asking *"from
the reset pin alone, with every flop starting at an arbitrary value, does the
design reach a defined state?"*

```sh
klt functional-verification request-x.json       # every flop starts at X
klt functional-verification request-random.json  # one regression per seed 1, 2, 3
```

`environment.initial_state` echoes the mode, the effective seeds, the clock
net the shim used, and the per-seed runs; each test of a random run carries
`initial_state_seed`. See
[`docs/cli/functional-verification.md`](../../../docs/cli/functional-verification.md)
→ "Gate-level power-up state: `options.initial_state`". List which flops the
reset does *not* reach with
[`klt reset-coverage`](../../../docs/cli/reset-coverage.md).
