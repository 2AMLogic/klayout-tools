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

## `pfd-charge-pump-tri-state/`

Backs [`kb/entries/pfd-charge-pump-tri-state.json`](../../kb/entries/pfd-charge-pump-tri-state.json).

| File | What it is |
|---|---|
| `charge_pump.spice` | Transistor-level (real `sky130_fd_pr__pfet_01v8`/`nfet_01v8` devices) UP/DOWN current-source/sink stage only — the PFD's own flip-flop/AND-gate reset logic is standard-cell-portable digital logic (see the entry's `pdk_portability.notes`) and out of scope for this artifact. Originally authored for issue #1325's `klt size` reproduction test (`tests/test_size.py`); `request.json` wraps it for `klt sim` as this entry's own reference figures. |
| `charge_pump_recentered.spice` / `charge_pump.sizing.json` / `design-centering-validation/` | Related `klt size` sizing-reproduction artifacts, not consumed by `request.json` directly. |
| `request.json` | The `klt sim` request: a single `tt`, 27C transient measuring the UP/DOWN branch currents and their mismatch. |

```
uv run klt sim examples/kb/pfd-charge-pump-tri-state/request.json
```

Reproduces `icp_up_a` (19.7971 µA), `icp_down_a` (20.2665 µA), and
`icp_mismatch_pct` (-2.34316%) at `tt`, 27C, `vdd`=1.8V (fixed by the
netlist's own `.param`, not swept) — the UP/DOWN mirror-current match this
entry's `sizing_approach` calls out as the dominant spur mechanism. Unlike
this KB's other three `measured` entries at the time this section was
written (`sky130-bandgap-reference`, `ldo-pmos-pass-error-amp`,
`rc-relaxation-oscillator`, which use behavioral/ideal elements), this
testbench uses real sky130A device models throughout.

## `two-stage-miller-ota/`

Backs [`kb/entries/two-stage-miller-ota.json`](../../kb/entries/two-stage-miller-ota.json)
— `two_stage_miller.spice` is a from-scratch netlist (real
`sky130_fd_pr__nfet_01v8`/`pfet_01v8` devices, not behavioral) built per
the entry's own `sizing_approach`: stage 1 reuses the
`five-transistor-ota`'s NMOS-diff-pair/PMOS-mirror-load/NMOS-tail
structure; stage 2 adds a common-source NMOS gain device loaded by a
diode-mirrored PMOS current source; a nulling resistor in series with the
Miller capacitor bridges stage 2's output back to stage 1's output for
pole-splitting plus RHP-zero cancellation. See the netlist's own header for
why the AC drive/DC-feedback terminal assignment is swapped relative to
`five-transistor-ota/ota_5t.spice` — this two-stage amplifier's overall
input-to-output polarity is inverted relative to the single stage, so the
feedback network must close onto the opposite input terminal to stay
negative feedback (closing it onto the same terminal as the single-stage
case would be positive feedback and never settle to a valid bias point).

```
uv run klt sim examples/kb/two-stage-miller-ota/request.json
```

Reproduces `av_db` (75.4539 dB), `gbw_hz` (86.2085 MHz), and `pm_deg`
(69.1986°) at `tt`, 1.8V, 27C — `klt sim --op-lint` reports every device in
saturation at this corner (no findings). The DC gain is roughly two stages'
worth higher than `five-transistor-ota`'s single-stage 39.6 dB, and the
compensation network gives a healthy phase margin, matching the entry's
own "high-gain... at the cost of a slower-settling, more
compensation-sensitive loop" trade-off description.

## `strongarm-latch-comparator/`

Backs [`kb/entries/strongarm-latch-comparator.json`](../../kb/entries/strongarm-latch-comparator.json)
— `strongarm.spice` is a from-scratch netlist (real
`sky130_fd_pr__nfet_01v8`/`pfet_01v8` devices, not behavioral) realizing
the entry's topology as the standard "modern StrongARM" structure: a
clocked tail switch gates a differential input pair whose own drain nodes
(the entry's "internal drain nodes") are reset to VDD by clocked PMOS
pull-ups, and a cross-coupled NMOS/PMOS latch pair (independently reset to
VDD, to avoid reset-phase contention with the cross-coupled NMOS pair)
regenerates the resulting imbalance into a full-rail digital decision. See
the netlist's own header for the full node map and this specific wiring's
decision polarity.

```
uv run klt sim examples/kb/strongarm-latch-comparator/request.json
```

Reproduces `regen_delay_s` (111.61 ps from the clock's 50%-point crossing
to the losing output's decision), and the settled full-rail decision
(`voutp_final_v` ≈ 1.1 µV, `voutn_final_v` = 1.8 V) for a 20 mV
differential input at `tt`, 1.8V, 27C. `klt sim --op-lint`'s DC-biased
findings (every device reported off/triode) are expected, not a defect:
this is an inherently dynamic, clocked circuit with no meaningful static
operating point at `clk`=0 (fully in reset) — the transient regeneration
behavior this `request.json` measures is the correct verification method
for this topology, not a DC operating-point check.
