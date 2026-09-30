# `examples/characterize/`

A runnable `klt characterize` worked example: characterize a synthetic
2-input NAND into an NLDM Liberty (`.lib`) timing and power model at one PVT
corner -- and, in batch mode, an inverter and the NAND into one combined
library.

```bash
klt characterize examples/characterize/request.json          # one cell
klt characterize examples/characterize/request-batch.json    # two cells, one .lib
```

Needs `ngspice` on `$PATH` and nothing else — no PDK, no Docker. The run is
a single transient (`4 input transitions x 4 output loads x 2 arcs = 32`
switching cell instances, plus `2**2 = 4` static leakage instances and one
capacitance probe per input pin, in one deck) and takes a few seconds.

## Files

| File | What it is |
|---|---|
| `request.json` | the `klt characterize` request — cell + pins/function, corner, grid, model library |
| `request-batch.json` | the batch-mode request — a `cells` array (`inv_demo`, `nand2_demo`) sharing one corner/grid, emitting one combined `.lib` |
| `cells.spice` | the cell netlist: `inv_demo` and `nand2_demo` `.subckt`s over bare `level=1` MOSFETs |
| `models.lib` | a two-section (`tt`/`ss`) synthetic model library |
| `generate.py` | regenerates all four (`uv run python3 examples/characterize/generate.py`) |

These are **not** real PDK data. CLAUDE.md's "open PDKs only" rule is about
design-rule/model data this repo *vendors*; a worked example only needs to
exercise the contract shape, which a square-law inverter does with no
external dependency — the same reasoning `examples/size/generate.py` and
`examples/sim/generate.py` use for their own synthetic fixtures. For a real
run against a real library see the `sg13g2_stdcell` recipe in
[`docs/cli/characterize.md`](../../docs/cli/characterize.md).

## What to look at in the output

Outputs land in `.klt/characterize/` next to the request (gitignored):

- `characterize_demo_tt_1p80V_25C.lib` — the emitted model. Both arcs
  (`A -> Y`, `B -> Y`) carry `cell_rise`/`cell_fall`/`rise_transition`/
  `fall_transition` and `timing_sense : negative_unate`, an
  `internal_power()` group with `rise_power`/`fall_power` per arc, and the
  cell carries `cell_leakage_power` plus one `leakage_power()` group per
  input state. Both inputs also carry a **measured** `capacitance` with its
  `rise_capacitance`/`fall_capacitance` split — neither is declared in
  `request.json`, which is the point: the number comes from this run's own
  simulation, not from somebody else's library. (`request-batch.json` does
  declare one, on `inv_demo`'s `A`, so the combined `.lib` shows the
  override path too: a declared value is emitted alone, with no split.)
- `testbench.spice` — the generated deck. Worth reading once: one `X`
  instance plus one `PWL` source plus one load cap per grid point, and the
  *other* input tied to `vdd` for each arc (the non-controlling value for a
  NAND, derived from `function : "!(A*B)"` rather than declared). Each
  instance reaches the rails through its own 0 V ammeter pair
  (`Vpd*`/`Vpg*`, integrated by the `*_qv*`/`*_qg*` `INTEG` cards); the
  `lk*` instances never switch -- they measure one input state's leakage
  each; and the `cp*` instances drive one input pin through its own 0 V
  ammeter (`Vcm*`, integrated by the `*_qcr`/`*_qcf` cards) to measure that
  pin's capacitance.
- `sim-request.json` / `sim-report.json` — the `klt sim` request this verb
  generated and the raw per-measurement report it read back, so every number
  in the `.lib` is traceable to a `.meas` card.

The batch request writes to `.klt/characterize-batch/`: one
`characterize_demo_batch_tt_1p80V_25C.lib` holding both cells, with each
cell's testbench and sim artifacts under `cells/<cell>/`.

The report's `liberty.roundtrip.status` is `pass` when the emitted file was
parsed back through `native/statime`'s own Liberty reader, and `skipped`
(with the reason) when the `klt_statime_native` extension is not installed —
never a fabricated pass.

## No committed golden output

Unlike `examples/drc/`, this example commits no expected JSON: the response
carries `runtime_s` (wall-clock) and `engine_version` (whatever `ngspice` is
installed), so a byte-exact fixture would be flaky by construction.
`tests/test_characterize.py` runs this example live (skipped without
`ngspice`) and asserts the documented *shape*.
