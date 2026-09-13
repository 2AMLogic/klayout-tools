# KB artifacts

Runnable artifacts backing individual knowledge-base entries. A KB entry
under [`kb/entries/`](../../kb/entries) may carry an optional `artifacts`
block naming a netlist and/or a layout for the design it describes:

```json
"artifacts": {
  "netlist": "examples/kb/rc-relaxation-oscillator/oscillator.spice",
  "layout": "examples/kb/sky130-spiral-inductor/spiral.gds"
}
```

Paths are repo-relative, and `klt kb validate` fails an entry whose
`artifacts` paths are not present on disk (and rejects an absolute or
`..`-escaping path outright) — so a KB entry can never claim a verification
link that has been moved, renamed, or never committed. See
[`kb/README.md`](../../kb/README.md) for the full schema and
[`docs/cli/kb.md`](../../docs/cli/kb.md) for the CLI contract.

One directory per entry, named after the entry `id`.

## `sky130-bandgap-reference/`

Backs [`kb/entries/sky130-bandgap-reference.json`](../../kb/entries/sky130-bandgap-reference.json)
— `testbench.spice` (the circuit body) plus the `klt sim` `request.json`
that sweeps supply and temperature over it.

```
uv run klt sim examples/kb/sky130-bandgap-reference/request.json
```

## `ldo-pmos-pass-error-amp/`

Backs [`kb/entries/ldo-pmos-pass-error-amp.json`](../../kb/entries/ldo-pmos-pass-error-amp.json),
same shape as above.

```
uv run klt sim examples/kb/ldo-pmos-pass-error-amp/request.json
```

## `rc-relaxation-oscillator/`

Backs [`kb/entries/rc-relaxation-oscillator.json`](../../kb/entries/rc-relaxation-oscillator.json).

| File | What it is |
|---|---|
| `oscillator.spice` | Circuit body (no `.control`/`.end`, per [`docs/cli/sim.md`](../../docs/cli/sim.md)). A comparator-based relaxation oscillator built from behavioral elements only — no PDK device models — so it runs anywhere ngspice runs. |
| `corners.lib` | Self-contained `tt`/`ss`/`ff` process sections, scaling the timing capacitor and reference current (the two knobs that actually set the period). |
| `request.json` | The `klt sim` request: 3 process × 2 supply × 3 temperature corners, measuring the oscillation period against limits. |

```
uv run klt sim examples/kb/rc-relaxation-oscillator/request.json
```

All 18 corners pass, with the period landing near the `2 * C * dV / I`
value the entry's `sizing_approach` predicts. The charge current and both
comparator thresholds derive from one reference node, so the measured
period tracks that reference rather than the supply — which is the
compensation mechanism the entry describes.

## `sky130-spiral-inductor/`

Backs [`kb/entries/sky130-spiral-inductor.json`](../../kb/entries/sky130-spiral-inductor.json)
— the one entry whose artifact is a **layout** rather than a netlist.

| File | What it is |
|---|---|
| `generate.py` | Deterministic generator (KLayout's `klayout.db` in batch mode). |
| `spiral.gds` | Octagonal met5 winding, met4 underpass with a via4 landing pad, and a met1 patterned ground shield, at sky130 layer/datatype pairs. |

```
uv run klt drc examples/kb/sky130-spiral-inductor/spiral.gds --deck sky130
uv run python3 examples/kb/sky130-spiral-inductor/generate.py   # regenerate
```

Clean against every met1 rule in this repo's sky130 deck. The deck carries
no met4/met5 rules yet (see `src/klayout_tools/decks/sky130.py`, "Scope
guard"), so those layers are drawn for structural realism and are not
checked; `generate.py` documents that, and the one place the drawn shield
simplifies the radial slotting the entry's `layout_idioms` call for.

## `five-transistor-ota/`

Backs [`kb/entries/five-transistor-ota.json`](../../kb/entries/five-transistor-ota.json)
— `ota_5t.spice` is a byte-for-byte reuse of
[`examples/design-pipeline/ota_5t.spice`](../design-pipeline/ota_5t.spice)
(the Epic #105 Phase 3 staged-pipeline worked example, S10-simulation-verified
across a 20-corner process/supply/temperature sweep — see that directory's
`sim-ac.result.json`/`sim-op.result.json`). `request.json` re-runs it at one
named corner from that sweep (`tt`, 1.62V, -40C) rather than re-authoring a
new netlist, since this entry's own topology matches the reused block
exactly (real `sky130_fd_pr__nfet_01v8`/`pfet_01v8` devices, not behavioral).

```
uv run klt sim examples/kb/five-transistor-ota/request.json
```

Reproduces `av_db` (39.6352 dB), `gbw_hz` (30.6189 MHz), and `pm_deg`
(86.1776°) — the same values as the `tt/1.620V/-40C` row of the reused
block's own `sim-ac.result.json`, since it is the identical netlist run at
the identical corner. Open-loop gain/phase are measured with the DC
feedback network (`Lfb`/`Cfb`) documented in the netlist's own header,
which biases the output to the common-mode point at DC while opening the
loop at AC.

## `inverter-based-comparator/`

Backs [`kb/entries/inverter-based-comparator.json`](../../kb/entries/inverter-based-comparator.json)
— `inverter_ota.spice` is a single CMOS inverter (real
`sky130_fd_pr__nfet_01v8`/`pfet_01v8` devices, longer-than-minimum `L` for
gain), self-biased to its own switching threshold by tying its output back
to its input, adapting the same `Lfb`/`Cfb` DC-feedback-loop-opening
technique `five-transistor-ota/ota_5t.spice` uses (there, across a
differential pair's two gates; here, across this inverter's single gate
node) — see the netlist's own header for the node-by-node walkthrough.

```
uv run klt sim examples/kb/inverter-based-comparator/request.json
```

Reproduces `av_db` (46.6068 dB) and `gbw_hz` (22.9561 MHz) at `tt`, 1.8V,
27C — the open-loop small-signal AC response around the inverter's own
self-found DC trip point. No phase-margin figure is reported: this entry's
actual use (auto-zero, then open-loop comparator evaluation) has no
negative-feedback loop to be stable against, unlike `five-transistor-ota`,
so a `pm_deg`-style number here would not mean what it means for that
entry — see `measured.notes` on the KB entry itself.
