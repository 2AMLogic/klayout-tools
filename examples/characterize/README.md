# `examples/characterize/`

A runnable `klt characterize` worked example: characterize a synthetic
2-input NAND into an NLDM Liberty (`.lib`) timing and power model at one PVT
corner -- and, in batch mode, an inverter and the NAND into one combined
library.

```bash
klt characterize examples/characterize/request.json          # one cell
klt characterize examples/characterize/request-batch.json    # two cells, one .lib
klt characterize examples/characterize/request-dff.json      # edge-triggered flop
klt characterize examples/characterize/request-dlat.json     # level-sensitive latch
klt characterize examples/characterize/request-dffsr.json    # async set/reset flop
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
| `request-dff.json` / `request-dff-constraints.json` | the edge-triggered flop requests — `clock_pin`, and setup/hold on `D` against the clock edge |
| `dff.spice` | a synthetic positive-edge D flip-flop (transmission-gate master/slave) |
| `request-dlat.json` | the level-sensitive latch request (issue #5521) — `enable_pin`/`enable_level` plus `constrained_pins` |
| `dlat.spice` | a synthetic active-high D latch: `Q` tracks `D` while `E` is high, and holds once `E` falls |
| `request-dffsr.json` | the asynchronous set/reset request (issue #5520) — `async_pins` on an active-low clear (`R`) and preset (`S`), emitting `preset`/`clear` propagation arcs on `Q`/`QN` and `recovery_rising`/`removal_rising` constraints on each async pin |
| `dffsr.spice` | the same flop as `dff.spice` plus an active-low clear (`R`) and preset (`S`) (hand-maintained, not written by `generate.py`) |
| `generate.py` | regenerates `models.lib`, `cells.spice`, `request.json`, `request-batch.json`, `dlat.spice` and `request-dlat.json` (`uv run python3 examples/characterize/generate.py`) |

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

## The latch example

`request-dlat.json` is the one request that shows all three latch shapes in a
single `.lib`, and it is worth reading the output next to `request-dff.json`'s:

- `pin (Q)` carries **two** `timing()` groups, not one — a
  `timing_type : combinational` arc `related_pin : "D"` (the transparent path,
  measured with `E` held high) and a `timing_type : rising_edge` arc
  `related_pin : "E"` (the enable's *opening* edge). The transparent delay is
  the shorter of the two: the enable has to travel the cell's own buffers
  before the output can move.
- `pin (D)` carries `setup_falling` / `hold_falling`, not `*_rising`. An
  active-high enable **closes** on its falling edge, and that is the edge a
  latch's data is constrained against. (Reading these as "a falling-edge
  cell" would be backwards — the same cell's enable arc is `rising_edge`.)
- `constraints/*/testbench.spice` shows the latch probe: the enable goes
  active once, early, and closes at the reference edge, with each probe
  carrying a transparent-delay card and a `FIND v(...) AT=` card that reads
  whether the captured level survived the close. See
  [`docs/cli/characterize.md`](../../docs/cli/characterize.md)'s "The latch
  probe's pass criterion" for why a transparent cell needs the second one.

No `latch (IQ, IQN)` group is emitted, the same way no `ff` group is emitted
for `dff.spice` — read the absence as "this command did not model the state",
never as an assertion about the cell.

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
