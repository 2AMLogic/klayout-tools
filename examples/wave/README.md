# `examples/wave/`

Fixtures for [`docs/guides/waveform-first-debugging.md`](../../docs/guides/waveform-first-debugging.md)
(issue #1846, Epic #1585 Phase 3) — a real cocotb failure on the
`sky130-modexp` canary's `test_modexp.py`/`modexp.v` regression, diagnosed
with `klt wave query` alone. Mirrors the provenance-and-regeneration-recipe
convention `native/wave/tests/fixtures/README.md` already uses for that
crate's own test fixtures.

## `modexp-stuck-multiply.v` / `modexp-stuck-multiply.fst`

`modexp-stuck-multiply.v` is a **fixture-only** copy of
[`examples/functional-verification/modexp.v`](../functional-verification/modexp.v)
with one deliberate mutation (see the file's own header comment and the
`S_MM_RUN` state's inline comment): the interleaved modular-multiply loop's
terminal sentinel is changed from a `CW`-bit encoding of `1` to `{CW{1'b1}}`
(all-ones), so `j` must underflow past zero before the comparison matches.
Every square/multiply pass now runs `WIDTH + 2` cycles instead of `WIDTH` --
a believable, "wrong constant" class of RTL bug, not a hang. This is not a
bug in the real `modexp.v` the canary ships; it exists only to reproduce a
genuine cocotb failure for the guide.

`modexp-stuck-multiply.fst` is the **real** trace that mutation produces:
`test_modexp.py`'s `test_modexp_known_vectors` driven against
`modexp-stuck-multiply.v` (`WIDTH=16`) via cocotb's own `Runner` API
(Icarus, `waves=True`) -- the same engine `klt functional-verification`
uses, just invoked directly since `options.trace` (Epic #1585 Phase 3a,
issue #1845) has not landed yet. The regression fails on the very first
vector:

```
modexp(2,10,1000): got 376, want 24
assert 376 == 24
```

Regenerate with:

```sh
python3 - <<'PY'
import pathlib
from cocotb_tools.runner import get_runner

proj = pathlib.Path("examples/wave")
runner = get_runner("icarus")
runner.build(
    sources=[str(proj / "modexp-stuck-multiply.v")],
    hdl_toplevel="modexp",
    build_dir=str(proj / "sim_build"),
    timescale=("1ns", "1ps"),
    waves=True,
    parameters={"WIDTH": 16},
)
runner.test(
    test_module="test_modexp",
    hdl_toplevel="modexp",
    test_dir="examples/functional-verification",
    build_dir=str(proj / "sim_build"),
    timescale=("1ns", "1ps"),
    waves=True,
    testcase="test_modexp_known_vectors",
    results_xml="results.xml",
)
PY
cp examples/wave/sim_build/modexp.fst examples/wave/modexp-stuck-multiply.fst
```

(cocotb's toplevel here is `modexp` itself -- no separate testbench wrapper
module -- so every signal the guide queries is a bare `modexp.<name>` path,
e.g. `modexp.clk`, `modexp.state`, `modexp.result`, `modexp.done`.)

## `modexp-known-good.fst`

The same recipe, unmodified `examples/functional-verification/modexp.v`
(the canary's real RTL) as `sources`, everything else identical. All 7
`test_modexp_known_vectors` vectors pass; the first vector
(`modexp(2,10,1000) == 24`) is the guide's own baseline comparison for the
`stuck` op.

## `klt wave build`'s own summary of each trace

| | `modexp-stuck-multiply.fst` | `modexp-known-good.fst` |
| --- | --- | --- |
| `signal_count` | 34 | 34 |
| `value_change_count` | 2442 | 11261 (all 7 vectors) |
| `clock.period_ns` | 10.0 | 10.0 |
| `reset.release` | 20.0ns (cycle 0) | 20.0ns (cycle 0) |
| First `modexp.done` rise | cycle 380 (3820.0ns) | cycle 344 (3460.0ns) |

Both are small (a few KB) and committed directly -- no `.gitignore` entry
needed, same as `native/wave/tests/fixtures/modexp_canary.vcd`.
