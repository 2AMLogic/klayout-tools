# `native/wave/tests/fixtures/`

Two small, committed FST fixtures exercised by `tests/query_ops.rs` (issue
#1599's own acceptance criterion: "unit tests over a committed small FST
fixture... cover every query op"). Regenerate both with:

```
cargo run --example gen_fixtures
```

from `native/wave/` (see `examples/gen_fixtures.rs`). Neither fixture is
produced by an actual `klt wave build` run (that verb is Phase 2b, issue
#1599's own sibling, not yet landed) -- they are hand-built directly with
the `fst-writer` crate to pin a known, checkable timeline.

## Shared timeline (`tb.clk`, `tb.rst_n`)

- `tb.clk`: period-10-tick square wave (rising edges at ticks 5, 15, 25, ...
  through 95), `1ns` timescale.
- `tb.rst_n`: asserted (`0`, active-low) from tick 0, deasserts (`1`) at
  tick 22.
- Cycle `0` anchor (contract section 3: "the first active edge of `clock`
  at or after `reset`'s release point") is therefore **tick 25** -- the
  first `clk` rising edge at or after tick 22.

## `gcd_like_a.fst`

- `tb.dut.o_valid`: `0` until tick 45 (**cycle 2**), `1` through tick 65
  (cycle 4), `0` through tick 85 (cycle 6), `1` through the end of the
  trace (tick 100) -- one "stuck high" tail run of 15 ticks.
- `tb.dut.data[7:0]`: `0x00` until tick 35, `0x01` until tick 55, `0x02`
  until tick 75, `0x03` through the end of the trace.

## `gcd_like_b.fst`

Identical `tb.clk`/`tb.rst_n`/`tb.dut.data` timeline; `tb.dut.o_valid`
instead asserts at tick 65 (cycle 4) and deasserts at tick 85 (cycle 6),
staying low through the end of the trace -- used only by the `diff` op
test as a second store that genuinely diverges from `gcd_like_a.fst`.
