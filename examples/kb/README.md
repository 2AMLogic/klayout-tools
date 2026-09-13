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

## `folded-cascode-ota/`

Backs [`kb/entries/folded-cascode-ota.json`](../../kb/entries/folded-cascode-ota.json)
— `folded_cascode.spice` is a from-scratch netlist (real
`sky130_fd_pr__nfet_01v8`/`pfet_01v8` devices throughout, not behavioral)
built per the entry's own topology text: an NMOS input differential pair
folds its signal current into PMOS folding current sources + PMOS folding
cascode devices, combined at an NMOS cascode current mirror to produce a
single-ended output. The two cascode gate biases are generated by small
diode-connected replica stacks carrying a reference current close to each
folded branch's actual excess current, rather than a full wide-swing
bias-generation network (the entry's own `sizing_approach` names that
fuller generator as a further refinement, not a prerequisite for this
open-loop AC measurement) — see the netlist's own header for the full
node-by-node walkthrough and why a naive replica (matched to the full
folding current instead) drives the cascode gate to an unusable bias
point.

```
uv run klt sim examples/kb/folded-cascode-ota/request.json
```

Reproduces `av_db` (40.518 dB), `gbw_hz` (10.5296 MHz), and `pm_deg`
(67.2939°) at `tt`, 1.8V, 27C. `av_db` lands only modestly above
`five-transistor-ota`'s bare single-stage 39.6 dB, not the "roughly two
orders of magnitude higher" the entry's topology text describes in general
terms — see `measured.notes` on the entry itself for why (that headroom is
only realized with much longer cascode channel lengths and/or the cited
paper's auxiliary gain-boosting amplifiers, both out of scope for this
general-purpose reference stage).

## `cml-tx-line-driver/`

Backs [`kb/entries/cml-tx-line-driver.json`](../../kb/entries/cml-tx-line-driver.json)
— `cml_driver.spice` is a from-scratch netlist (real
`sky130_fd_pr__nfet_01v8` devices, not behavioral): an NMOS differential
pair with a diode-mirrored tail current sink (the same bias pattern as
`five-transistor-ota`) drives two plain resistor pull-ups. `Rterm`=200 ohm
at a 1 mA tail current, not a literal 50 ohm/several-mA line-matched
instance — that design point pushed the tail mirror into triode at these
device sizes, so both were scaled down together to preserve a realistic
~300 mV CML differential swing while keeping every device saturated; see
the netlist's own header for the full derivation.

```
uv run klt sim examples/kb/cml-tx-line-driver/request.json
```

Applies a single full-swing differential data transition and samples both
outputs before/after it. Reproduces `vswing_diff_v` (0.307355 V) — matching
this entry's own swing = (tail current) x (termination resistance) sizing
relation for the actual, headroom-limited tail current this netlist
achieves — plus the four individual `voutp`/`voutn` sample points at
`tt`, 1.8V, 27C.

## `rx-front-end-termination-buffer/`

Backs [`kb/entries/rx-front-end-termination-buffer.json`](../../kb/entries/rx-front-end-termination-buffer.json)
— `rx_frontend.spice` is a from-scratch netlist per the entry's own
topology text: a fixed on-die-termination (ODT) resistor leg plus a 2-bit
digitally-trimmed parallel resistor bank sits at the receive pads, feeding
a resistively-loaded differential input pair biased by a real
`sky130_fd_pr__nfet_01v8` current mirror (the same bias pattern as
`five-transistor-ota`). Each trim leg is switched by a CMOS transmission
gate (NMOS + PMOS in parallel), not a bare NMOS — at this receive common
mode a bare NMOS switch has only ~0.6V of overdrive and barely conducts,
collapsing the trim bank's whole range; see the netlist's own header for
the full derivation. The channel is modelled as a lumped 50-ohm/leg series
resistance rather than a distributed transmission line, and the
termination/trim/load resistors are ideal `R` elements rather than sky130
resistor-option models — both disclosed in `artifacts.notes` on the entry
itself, since a real reflection-domain (TDR) measurement and a
process-variation-accurate trim range both need a fuller model this
single-corner reference testbench does not build.

```
uv run klt sim examples/kb/rx-front-end-termination-buffer/request.json
```

Reproduces `pad_atten_db` (-6.02459 dB, matching the expected -6.02 dB for
a 100 ohm differential channel driving into a 100 ohm differential
termination) and the `zin_diff_ohm` it implies (99.9083 ohm, on the trim
bank's fixed code-11 target), plus the input buffer's own `av_db`
(14.9404 dB), `f3db_hz` (728.783 MHz), and `f_unity_hz` (1.93444 GHz) at
`tt`, 1.8V, 27C.

## `sram-power-up-puf/`

Backs [`kb/entries/sram-power-up-puf.json`](../../kb/entries/sram-power-up-puf.json)
— `sram_cell_powerup.spice` is a from-scratch netlist per the entry's own
topology text: one 6T bitcell (a pair of cross-coupled
`sky130_fd_pr__nfet_01v8`/`pfet_01v8` inverters plus access devices) read
across a 0-to-1.8V, 100ns power-up supply ramp, with the wordline held off
for the whole run (no write path, no read disturb) and the bitlines left
at high impedance. A noiseless single-corner SPICE run has no per-instance
random mismatch of its own, so this netlist injects one deterministic
asymmetry — one pull-down device drawn 5% wider than its twin — as a
stand-in for the real mismatch that decides a silicon cell's power-up
state; see the netlist's own header for exactly what that does and does
not let this testbench claim (no bit-error-rate, min-entropy, or
stable-cell-fraction figure — that needs Monte Carlo over sky130's
mismatch models, per the entry's own `pdk_portability`).

```
uv run klt sim examples/kb/sram-power-up-puf/request.json
```

Reproduces the cell's settled power-up decision — `vq_final_v` (1.8 V) vs.
`vqb_final_v` (~31 nV) — resolving within `t_resolve_s` (63.28 ns) of the
ramp's start, well before the supply ramp itself completes
(`vdd_at_resolve_v` = 1.13907 V, i.e. the cell decides mid-ramp) at `tt`,
27C. Flipping the injected mismatch's sign flips which node resolves high,
which is the point: the outcome tracks the mismatch, exactly as the
entry's topology describes.
