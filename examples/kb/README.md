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

## `beta-multiplier-bias-cell/`

Backs [`kb/entries/beta-multiplier-bias-cell.json`](../../kb/entries/beta-multiplier-bias-cell.json)
— `beta_multiplier.spice` is a from-scratch netlist per the entry's own
topology/sizing_approach text: a PMOS mirror over two NMOS branches with
K = 4 (four unit devices in parallel), degenerated by a real
`sky130_fd_pr__res_high_po` resistor rather than an ideal `R`, plus a
self-extinguishing start-up circuit and a downstream mirror leg that
turns the reference current back into a distributable bias voltage.

Two things in the netlist's header are worth reading before the numbers.
First, a **deliberate departure from the entry's prose**: the entry's
`topology` field places the degeneration resistor in the *unscaled*
branch, but that orientation has no non-zero solution — at equal branch
currents the K-times-wider device carries the *smaller* Vgs, so the
resistor has to sit in the source of the K-times-wider device for
`I*Rbias = Vgs(1x) - Vgs(Kx)` to be positive. That is the orientation
Allen & Holberg's Ch. 4 (this entry's own cited source) derives, and the
one built here. Second, an ngspice trap: the K = 4 parallel count rides on
`m=`, **not** on `mult=` — sky130's `.subckt` declares a `mult` parameter
but never passes it down to the enclosed BSIM device line, so `mult=4`
alone is silently a 1x device, which collapses this cell to its degenerate
near-zero-current state.

```
uv run klt sim examples/kb/beta-multiplier-bias-cell/request.json
```

Reproduces a `iref_a` of 5.78923 µA developed across `vr_v` = 57.2793 mV
of ΔVgs, mirrored out as `i_copy_a` = 5.79491 µA at `vbias_out_v` =
646.292 mV, at `tt`, 27C.

The headline pair is `line_sens_pct_per_v` (10.7585 %/V) against
`simple_bias_line_sens_pct_per_v` (81.794 %/V). Both are measured on the
same netlist over the same 1.2V–2.0V supply sweep at the same ~6 µA
operating current — the second is a plain resistor-to-diode bias leg
included purely as a baseline, so that the comparative claim in the
entry's own topology text ("far less sensitive to supply variation than a
simple resistor-divider bias") is **measured** here rather than asserted.
It is 7.6× less supply-sensitive. `i_startup_a` (741 fA) and `vsu_v`
(999 µV) together confirm the start-up circuit really has extinguished
itself at the operating point rather than sitting half-on and perturbing
the loop.

This is a *supply* sweep at a fixed 27C, so it substantiates the
supply-insensitivity half of the entry's claim and says nothing about the
resistor-TC-driven temperature drift its `pdk_portability` calls out. The
mirrors are plain rather than cascoded — the entry lists cascoding as an
optional supply-rejection improvement, so the figure above is the
plain-mirror baseline.

## `cmos-subthreshold-voltage-reference/`

Backs [`kb/entries/cmos-subthreshold-voltage-reference.json`](../../kb/entries/cmos-subthreshold-voltage-reference.json)
— `subthreshold_ref.spice` is a from-scratch netlist per the entry's own
topology/sizing_approach text, containing **no bipolar devices of any
kind**, which is the entry's whole point versus
[`kb/entries/sky130-bandgap-reference.json`](../../kb/entries/sky130-bandgap-reference.json). A self-biased
loop runs one unit NMOS against eight in parallel, both deliberately in
weak inversion (W/L = 20 devices at ~0.8 µA, about a quarter of the
~3.8 µA weak-inversion ceiling for that geometry — genuinely subthreshold
rather than merely "low current"); their ΔVgs is dropped across R1 to make
a PTAT current, which a PMOS mirror copies into an output branch of R2 in
series with a diode-connected NMOS, so `Vref = Vgs_ctat + I_ptat*R2`.
R2/R1 is the single first-order-cancellation knob, and because both
resistors are the same sky130 flavour their own TCs cancel in the ratio.
The resistors are real `sky130_fd_pr__res_xhigh_po` models — the
extra-high-sheet poly flavour rather than the `res_high_po` the sibling
entries use, because at these currents the summing resistor is hundreds
of kΩ.

The start-up circuit the entry demands is a single source-follower device
keyed off the PMOS gate rail, *not* the skewed-inverter detector
`beta-multiplier-bias-cell` uses. That difference is forced by weak
inversion and is spelled out in the netlist header: a weak-inversion loop
settles at `nbias ≈ Vth`, and an inverter's trip point is itself pinned
near Vth however hard its devices are skewed, so an inverter detector sits
permanently half-on and bleeds current into the very ΔVgs the reference is
built from.

```
uv run klt sim examples/kb/cmos-subthreshold-voltage-reference/request.json
```

Reproduces `vref_27c_v` = 709.757 mV with a `tc_ppm_per_c` of 61.4074
ppm/C by the box method over a -40C…125C sweep — inside the
"tens-of-ppm/C to low-hundreds-of-ppm/C range" the entry's own notes
predict for a BJT-free reference. The residual is curvature rather than
uncancelled first-order slope: `vref` traces a bowl with its minimum near
44C (`vref_min_v` = 709.511 mV) and its maximum at the -40C end
(`vref_max_v` = 716.702 mV), which is the signature of a correctly
weighted PTAT/CTAT sum. `iptat_ratio_125_m40` (2.17926) is the evidence
that the ΔVgs term really is PTAT, and `dvgs_v` (67.4289 mV), `vctat_v`
(608.573 mV) and `vptat_v` (101.184 mV) expose the two summed terms
separately so the cancellation can be checked by hand. `iq_a` (2.38765 µA)
is the low-static-current claim the entry's `spec_class` makes against a
bipolar bandgap; `i_startup_a` (7.13586 pA) confirms the start-up device
extinguishes itself.

This is a *temperature* sweep at a fixed 1.8V supply — the complement of
the supply sweep on `beta-multiplier-bias-cell` — so it says nothing about
line regulation or PSRR, and single-corner with no mismatch, so nothing
here supports a spread, trim-range, or absolute-accuracy claim.

## `cmos-ring-vco-current-starved/`

Backs [`kb/entries/cmos-ring-vco-current-starved.json`](../../kb/entries/cmos-ring-vco-current-starved.json)
— `ring_vco.spice` is a from-scratch netlist per the entry's own
topology/sizing_approach text: five single-ended CMOS inverter stages in a
ring, each with its pull-up and pull-down current limited by a series
PMOS/NMOS starving device, plus a bias mirror that turns the control
voltage into the two starving-gate rails. The single-ended variant is
built, not the pseudo-differential delay-cell variant the entry offers as
an alternative — that cell's distinguishing figures (supply/common-mode
rejection, even-order harmonic content) deserve their own testbench.

The whole tuning curve is swept inside **one** transient run: `vctrl` is a
staircase dwelling at 0.7/0.9/1.1/1.3 V for 600 ns each, and the request
counts 8 oscillation periods inside each dwell, 150 ns after the step. That
is deliberate — the entry's headline verification item is the tuning
curve's *shape*, which is a property of several control voltages together
rather than of any single operating point. The ring is kicked into
oscillation by an explicit `.ic` seeding one node per rail, because a
noiseless simulator will otherwise sit on the ring's symmetric DC solution
forever; that is a simulation device, not part of the circuit.

```
uv run klt sim examples/kb/cmos-ring-vco-current-starved/request.json
```

Reproduces the four-point tuning curve — `f_vctrl_0p7_hz` = 18.3413 MHz,
`f_vctrl_0p9_hz` = 114.454 MHz, `f_vctrl_1p1_hz` = 175.261 MHz,
`f_vctrl_1p3_hz` = 192.237 MHz — a `tuning_ratio` of 10.4811× (3.4
octaves), which is the "very wide tuning range spanning multiple octaves"
the entry's notes claim for this topology.

On the entry's own criterion ("verify the tuning curve stays monotonic and
reasonably linear") the measured answer is **monotonic yes, linear no**,
and the figures are picked to say so precisely. `step_lo_hz` /
`step_mid_hz` / `step_hi_hz` are the three successive frequency
increments, all positive — that is the monotonicity check.
`kvco_low_hz_per_v` (480.563 MHz/V) against `kvco_high_hz_per_v`
(84.8807 MHz/V) is the linearity check: Kvco falls 5.66×
(`kvco_spread_ratio`) across the control range, so no single Kvco number
is published as the headline. That compression is not a sizing defect but
what the bias topology does — the V-I converter is square-law in `vctrl`
to begin with, and the rising current drags the PMOS gate rail down until
the control device leaves saturation for triode and stops following
`vctrl` at all. A PLL closing a loop around this VCO has a loop bandwidth
that varies by that same 5.66× across its lock range, which is why real
designs linearise the V-I stage rather than driving the starving gates
from a bare square-law device.

`vpp_slow_v` (1.82506 V) is the start-up check the entry's
`sizing_approach` asks for: at the most-starved end of the control range
the ring still swings rail to rail, so current starving has not been
pushed past the point where oscillation stops. (The `vpp` figures exceed
the 1.8 V supply by the stages' own switching overshoot.)

**No phase-noise or jitter figure is claimed.** That is the spec a VCO is
usually judged on, and this testbench deliberately does not assert one:
phase noise needs a periodic-steady-state/noise analysis (ngspice has no
PSS/pnoise) and jitter needs either that or a transient-noise campaign. What
the entry's notes say about ring-vs-LC phase noise therefore remains
unverified here — see
[`kb/entries/sky130-lc-vco-cross-coupled.json`](../../kb/entries/sky130-lc-vco-cross-coupled.json)
for the LC counterpart. Single corner, so this establishes the nominal
tuning curve only, not the across-corners behaviour the entry asks for.

## `ro-puf/`

Backs [`kb/entries/ro-puf.json`](../../kb/entries/ro-puf.json) —
`ro_puf.spice` is a from-scratch netlist (real
`sky130_fd_pr__nfet_01v8`/`pfet_01v8` devices, not behavioral) built per
the entry's own topology text: two nominally identical, free-running
5-stage ring oscillators (the same building block as
`cmos-ring-vco-current-starved`, minus the current starving — a PUF wants
each ring's own natural frequency), with each ring's period measured
directly via SPICE `.meas` TRIG/TARG timing rather than the entry's own
counter+comparator digital back-end.

```
uv run klt sim examples/kb/ro-puf/request.json
```

A single deterministic SPICE run has no per-instance random mismatch, so
ring B's five stages are all drawn a deterministic 5% wider (both NMOS and
PMOS) than ring A's, as a stand-in for the random Vt/geometry mismatch a
real RO-PUF exploits — the same technique
`examples/kb/sram-power-up-puf/sram_cell_powerup.spice` uses for its own
injected asymmetry. Reproduces `freq_a_hz` (3.16132 GHz) and `freq_b_hz`
(3.21356 GHz), with `freq_diff_hz` (52.2213 MHz, +1.65187%) positive as
expected since ring B's wider devices carry more drive current. The
measured sign is a property of this injected asymmetry, not a prediction
of any real device pair's response — mirroring the asymmetry onto ring A
instead would flip it. No bit-error-rate, min-entropy, or
response-stability claim is made; that needs Monte Carlo over sky130's
mismatch models across temperature/voltage/aging, which this single
deterministic corner does not do — see the entry's own `artifacts.notes`.

## `metastability-trng/`

Backs [`kb/entries/metastability-trng.json`](../../kb/entries/metastability-trng.json)
— `metastability_latch.spice` is a from-scratch netlist (real
`sky130_fd_pr__nfet_01v8`/`pfet_01v8` devices, not behavioral) realizing
the entry's topology as a cross-coupled inverter pair held equalized near
its own switching threshold by a transmission gate shorting its two nodes
(`q`, `qb`) together, then released to resolve. Shorting the two nodes
together turns each inverter into a self-biased loop through the other, so
the cell settles near the metastable balance point with no manual `.ic`
needed to find it.

```
uv run klt sim examples/kb/metastability-trng/request.json
```

Because a single deterministic SPICE run has no thermal noise, one
inverter's pull-down NMOS is drawn a deterministic 5% wider than the
other's, standing in for the noise that decides a real cell's outcome.
Reproduces `vmeta_sep_v` (1.94535 mV — confirming the cell really is
sitting near its shared switching threshold, ~0.85 V, before release, not
merely claimed to be), `resolve_delay_s` (58.9162 ps from release to the
losing node crossing back through 0.9 V), and the settled full-rail
resolution (`vq_final_v` ≈ 0.52 µV, `vqb_final_v` = 1.8 V). The resolved
polarity is a property of the injected asymmetry's sign, not a prediction
of any real trial's outcome — on real silicon the outcome is
thermal-noise-driven and differs trial to trial. This testbench does NOT
implement the entry's own metastability-quality feedback/tracking loop
(the offset-cancellation or balancing-pulse-timing control the entry's
`sizing_approach` and notes call load-bearing for a robust design); it
demonstrates only the release-and-resolve mechanism the loop would operate
on — see the entry's own `artifacts.notes`.

## `ring-oscillator-jitter-trng/`

Backs [`kb/entries/ring-oscillator-jitter-trng.json`](../../kb/entries/ring-oscillator-jitter-trng.json)
— `ro_jitter_trng.spice` is a from-scratch netlist (real
`sky130_fd_pr__nfet_01v8`/`pfet_01v8` devices, not behavioral): a
free-running 5-stage ring oscillator, the same building block as
`ro-puf`'s ring A.

```
uv run klt sim examples/kb/ring-oscillator-jitter-trng/request.json
```

This testbench does not build a transistor-level D flip-flop sampler — an
earlier version tried a transmission-gate latch (input pass gate + a
two-inverter regenerative loop + a feedback pass gate) and found that, with
a track window narrow enough to sample a specific ring phase, the external
drive could not reliably overpower the already-latched feedback within the
window: a genuine flip-flop aperture/setup-time problem, not a netlist bug,
and a separate concern from what this entry's own topology is about.
Instead, this directory's `request.json` reads the ring's own `ro1` node
directly at 8 instants spaced 1.05 ns apart — deliberately not a whole
multiple of the ring's own `period_ro_s` (315.857 ps) — so each successive
sample lands at a different, walking phase of the ring's waveform. The
resulting sequence (1.68, 1.79, 0.006, 1.60, 1.91, 0.007, 1.45, 1.89 V,
with `toggle_sum_v` = 7.58424 V of cumulative movement across the walk)
visibly swings between near-0 V and near/above-VDD, demonstrating the
"small timing difference flips the sampled level" mechanism real jitter
exploits — without claiming this deterministic phase walk IS entropy, or
that ngspice measured any jitter/phase-noise figure at all (it has no
periodic-steady-state/noise analysis) — see the entry's own
`artifacts.notes` for the full disclosure, including why the flip-flop
sampler itself is not modeled here.
