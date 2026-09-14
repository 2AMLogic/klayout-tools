# Device-level relaxation oscillator (issue #1814)

A full, working sky130A **device-level** RC relaxation oscillator,
assembled around the split-polarity dual-comparator core
[`../comparator-core/`](../comparator-core/) landed by
[#1813](https://github.com/2AMLogic/klayout-tools/issues/1813). It
oscillates and holds `benchmarks/design-agent/tasks/rc-relaxation-
oscillator.json`'s own 1.4-2.2 us period band across all 18 sky130A PVT
corners.

**This is not the task's shipped reference solution** — see
[Decision: ship alongside, do not replace](#decision-ship-alongside-do-not-replace)
below for the explicit call issue #1814 asked for. The file the task
actually scores against remains the behavioral
[`../oscillator.spice`](../oscillator.spice).

Reproduce:

```
klt sim benchmarks/design-agent/reference/rc-relaxation-oscillator/device-oscillator/sim_request.json
```

(~5 minutes wall on 4 workers; 18 corners x a 20 us transient of real
sky130A devices.)

## What got assembled

`docs/design/relaxation-oscillator-comparator-core-spike.md`'s "What is
deliberately still missing" section listed four pieces the comparator-core
spike did not have. All four are in
[`oscillator_device.spice`](oscillator_device.spice):

| Piece | Realisation |
|---|---|
| Charge/discharge current source + timing capacitor | `XMchg`/`XMdis` current mirrors (~1.95:1 off the same `pbias`/`nbias` rails the comparator core already uses) through complementary switches `XSWchg`/`XSWdis`, into `Ccap` = 10 pF |
| Output edge-combination logic | `NOT(olo)` inverter + cross-coupled CMOS NOR SR latch; `S = ohi`, `R = NOT(olo)`, `q` is the clock and also the single charge/discharge control |
| Device-level `vth_lo`/`vth_hi` reference generator | `XMref` mirrors `{ib}` into a real `sky130_fd_pr__res_xhigh_po` divider (`XRdiv1`/`XRdiv2`), with 0.5 pF `Crefl`/`Crefh` decoupling against comparator kickback — replaces the spike's ideal `Vrl`/`Vrh` DC sources |
| 18-corner oscillation validation | [`sim_request.json`](sim_request.json), results below |

The `cmp_n`/`cmp_p` `.subckt` bodies are copied **verbatim** from
`../comparator-core/comparator_core.spice` — nothing about the comparator
core itself was re-tuned for this assembly.

#1795's regenerative-feedback / PTAT-CTAT bias-generator work (scope item
5 of #1814) is deliberately **not** attempted here; #1814's own scope stops
at "the oscillator works". See
[Handoff](#handoff-what-the-follow-on-should-attack) below.

## Evidence

`klt sim .../device-oscillator/sim_request.json`, real sky130A devices,
standalone `ngspice-46`, full 18-corner matrix (`tt`/`ss`/`ff` x
1.62/1.98 V x -40/27/125 C) — **18/18 corners pass**, gated on the task's
own 1.4-2.2 us band:

| corner | `vth_lo` (V) | `vth_hi` (V) | `dv_eff` (V) | `period_avg` (us) | drift (%) | `vo` high/low (V) |
|---|---|---|---|---|---|---|
| tt/1.62V/-40C | 0.3965 | 1.3029 | 0.9064 | 1.9550 | -0.03 | 1.3532 / 0.3743 |
| tt/1.62V/27C | 0.3618 | 1.1889 | 0.8271 | 1.7072 | +0.07 | 1.2057 / 0.3425 |
| tt/1.62V/125C | 0.3231 | 1.0618 | 0.7387 | 1.4963 | +0.32 | 1.0770 / 0.3117 |
| tt/1.98V/-40C | 0.4039 | 1.3282 | 0.9243 | 1.8808 | +0.22 | 1.3437 / 0.3807 |
| tt/1.98V/27C | 0.3678 | 1.2082 | 0.8405 | 1.7010 | +0.11 | 1.2234 / 0.3513 |
| tt/1.98V/125C | 0.3277 | 1.0767 | 0.7491 | 1.5020 | -0.10 | 1.0984 / 0.3218 |
| **ss/1.62V/-40C** (slowest) | **0.3963** | **1.3025** | **0.9061** | **2.0542** | **+0.03** | **1.4009 / 0.3718** |
| ss/1.62V/27C | 0.3616 | 1.1883 | 0.8266 | 1.7124 | +0.16 | 1.2076 / 0.3393 |
| ss/1.62V/125C | 0.3228 | 1.0606 | 0.7378 | 1.5068 | +0.05 | 1.0783 / 0.3087 |
| ss/1.98V/-40C | 0.4044 | 1.3282 | 0.9238 | 1.8889 | +0.16 | 1.3454 / 0.3815 |
| ss/1.98V/27C | 0.3674 | 1.2072 | 0.8398 | 1.7076 | -0.03 | 1.2247 / 0.3491 |
| ss/1.98V/125C | 0.3272 | 1.0753 | 0.7481 | 1.5083 | +0.08 | 1.0998 / 0.3193 |
| ff/1.62V/-40C | 0.3965 | 1.3029 | 0.9064 | 1.9389 | -0.03 | 1.3479 / 0.3750 |
| ff/1.62V/27C | 0.3621 | 1.1897 | 0.8276 | 1.6957 | -0.16 | 1.2039 / 0.3450 |
| **ff/1.62V/125C** (fastest) | **0.3236** | **1.0631** | **0.7395** | **1.4876** | **-0.20** | **1.0758 / 0.3137** |
| ff/1.98V/-40C | 0.4041 | 1.3290 | 0.9249 | 1.8812 | -0.06 | 1.3433 / 0.3828 |
| ff/1.98V/27C | 0.3679 | 1.2090 | 0.8412 | 1.6921 | -0.12 | 1.2225 / 0.3541 |
| ff/1.98V/125C | 0.3282 | 1.0782 | 0.7501 | 1.4925 | +0.29 | 1.0971 / 0.3238 |

- **Period**: 1.4876-2.0542 us — inside the 1.4-2.2 us band at every
  corner. In ratio terms the fast end clears its limit by 6.3%
  (`1.4876/1.4`) and the slow end by 7.1% (`2.2/2.0542`). The design is
  close to optimally centred: a design with this same 38% spread, placed
  perfectly, would have 6.7% on each side.
- **The clock is stable, not just present.** `period_drift_pct` compares
  the first steady-state period against the 3-period average that follows
  it; it stays within +/-0.32% at every corner, well inside the request's
  own +/-1% gate.
- **`vo` stays between the thresholds** at every corner (gated `vo_high <=
  1.55 V`, `vo_low >= 0.15 V`) — i.e. the timing node really is ramping
  between two comparator trips, not railing to a supply with the clock
  coming from somewhere else.
- **Self-starting**, with no `Istart`-equivalent kick: `.op` does *not*
  resolve the latch (measured `v(q) = 0.5715 V` at t=0, mid-rail, at
  ss/1.62V/-40C) but it does leave `vo` at 1.3623 V, above that corner's
  1.3025 V `vth_hi`, so one comparator is unambiguously asserting and the
  latch's own positive feedback resolves within a few ns. First full edge
  at 0.24 us; all 18 corners reach 4 clean steady-state edges after the
  6 us measurement gate.

### Where the PVT spread comes from

The 38% total period spread is almost entirely **temperature**, and
almost entirely the **reference resistor's temperature coefficient**:

| axis | swing in `dv_eff` | note |
|---|---|---|
| Temperature (-40 -> 125 C) | -18.5% | `sky130_fd_pr__res_xhigh_po`'s `rbody` carries `tc1 = -1.47e-3`/C (`-4.3e-4`/C on the head segments) — about -1.1e-3/C blended over this geometry |
| Supply (1.62 -> 1.98 V) | +1.6% | `XMref`'s own channel-length modulation; its `\|Vds\|` is `vdd - vth_hi` |
| Process (`tt`/`ss`/`ff`) | ~0.1% | the divider is a current-into-resistor product; `sky130`'s `res_xhigh_po` process variation is a Monte-Carlo parameter, not a corner-card axis |

Period tracks `dv_eff` closely because `T = 2 * Cosc * (vo_high - vo_low)
/ iosc` and the mirror current is nearly temperature-flat: at
tt/1.62V/-40C that arithmetic gives `iosc = 10.0 uA` against a nominal
`1.95 * 5 uA = 9.75 uA`, and at tt/1.62V/125C it gives 10.2 uA.

The comparator + latch delay shows up as ~50 mV of overshoot past
`vth_hi` and ~22 mV of undershoot past `vth_lo` (~8% of `dV` in total) —
consistent with the 12.8-19.9 ns propagation delay the comparator-core
spike measured on its own testbench, and the reason the shipped sizing
was centred against measured `vo_high - vo_low` rather than against the
nominal `vth_hi - vth_lo`.

### Expected solver diagnostics

Most corners emit `Warning: Dynamic gmin stepping failed`, and about half
additionally emit `Warning: singular matrix: check node vth_lo`, during
the DC operating-point solve; source stepping then completes and the
transient runs cleanly everywhere. The singular-matrix note belongs to the
**sky130 resistor model**, not to this topology: swapping `XRdiv1`/
`XRdiv2` for ideal `R` elements of the same value makes it disappear with
everything else unchanged. `sky130_fd_pr__res_xhigh_po` expresses its body
resistance as a bias-dependent formula containing `abs(v(t1,t2))`, whose
Jacobian is discontinuous at exactly the zero bias that gmin/source
stepping starts from. The shipped netlist keeps the real resistor device.

## Reference generator: why not a bandgap

Scope item 3 of #1814 allowed "bandgap-derived per
`kb/entries/sky130-bandgap-reference.json`, or a documented simpler
stand-in, with the choice justified". This uses the simpler stand-in — a
mirrored current into a real poly-resistor divider — for three reasons:

1. **The KB entry's own artifact is itself behavioral.** `examples/kb/
   sky130-bandgap-reference/testbench.spice` models the PTAT/CTAT summing
   with ideal diodes and ideal current sources, not sky130's parasitic PNP
   devices — its own `measured.notes` says so. Copying it in would have
   added ideal elements to a netlist whose entire point is that it is
   device-level.
2. **Its output is 0.209 V**, a current-domain summing node, not the
   0.35 V / 1.15 V pair this oscillator's split-polarity comparator core
   needs. Using it would have required a gain stage on top of it anyway.
3. **A real, curvature-corrected Banba bandgap on sky130** (real
   `sky130_fd_pr__pnp_05v5` devices, trimmed PTAT/CTAT resistor ratios)
   does not exist anywhere in this repo and is a substantial standalone
   design in its own right — squarely the deferred scope item 5, not this
   increment.

The stand-in is still a genuine step past the spike: both thresholds are
now set by real MOSFET + real poly-resistor devices and carry real PVT
sensitivity (quantified above), where the spike used ideal DC sources. It
buys no first-order temperature cancellation, which is exactly why the
resistor tempco dominates the measured spread.

## Decision: ship alongside, do not replace

**This device-level design is shipped as an additional variant.
`../oscillator.spice` remains the reference solution
`../../../tasks/rc-relaxation-oscillator.json` scores against.**

Reasons, in order of weight:

1. **The behavioral reference's "runs anywhere `ngspice` runs" property is
   load-bearing for the benchmark.** `../oscillator.spice` has no
   `.model`/device cards at all; it needs no `$PDK_ROOT`. Making the
   scored reference solution PDK-resolving would put a sky130A install on
   the critical path of the benchmark's own reference gate, which
   `benchmarks/design-agent/README.md`'s "Known limitations" #2 records as
   a deliberate property of the oscillator/VCO/PLL family, not an
   oversight.
2. **Runtime.** The behavioral reference's 18-corner gate runs in seconds;
   this one takes ~5 minutes on 4 workers, and the reference-solution gate
   runs on every `validate` and on every uncached `run`.
3. **Margin.** The behavioral reference sits comfortably mid-band by
   construction; this one holds the band with ~6% margin on each side
   because its period carries the poly resistor's real tempco. Making it
   the scored reference would make the benchmark's own gate materially
   more fragile — for a task whose point is the `C*dV/I` timing relation,
   not silicon accuracy.
4. **Scope.** Swapping the scored reference also means replacing
   `eval_descriptor.json`'s gate and objective wiring and re-deriving the
   task's own target band. That is a benchmark-contract change, not the
   circuit-assembly increment #1814 scoped.

This decision is reversible and should be revisited if the follow-on work
below lands: a device-level oscillator with, say, 20%+ band margin and a
sub-minute corner sweep would remove reasons 2 and 3, leaving only the
PDK-dependency question.

## Handoff: what the follow-on should attack

The single highest-value change is **compensating the reference
resistor's temperature coefficient**, which owns essentially all of the
38% spread:

- A composite divider (series negative-tempco poly + positive-tempco
  diffusion/n-well resistor) is the standard zero-TC construction and
  needs no new active circuitry.
- Or #1795's originally scoped PTAT/CTAT bias generator, making `iosc`
  track `dV`'s temperature dependence so the ratio — and therefore the
  period — cancels.

Both are scope item 5 of #1814, explicitly deferred. Secondary items, in
rough order: the ~8% delay-induced overshoot on `dV` (regenerative
feedback in the comparator, per #1789's `1/sqrt(I)` finding); offset and
mismatch, which no testbench in this chain has modelled yet; and
`XMref`'s headroom at the 1.62 V/-40C corners, where its `|Vds|` is the
smallest in the matrix at `1.62 - 1.3029 = 0.317 V` (still comfortably
above a 5 uA, W/L = 80 PMOS's sub-100 mV subthreshold `|Vdsat|`, but the
axis to watch if the thresholds are ever pushed further apart).
