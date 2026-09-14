# A full device-level sky130A relaxation oscillator (issue #1814)

**Status:** investigation record + shipped artifact. Closes issue #1814's
own scope — assembling
[the split-polarity comparator core](relaxation-oscillator-comparator-core-spike.md)
into an oscillator that actually oscillates and holds the benchmark task's
period band across the full PVT matrix. Scope item 5 (#1795's
regenerative-feedback / PTAT-CTAT bias-generator work, i.e. *tightening*
the PVT spread rather than clearing the band) is deliberately **not**
attempted here, per #1814's own ordering.

## Chain

`#1789` (first device-level oscillator attempt; 18-corner period sweep,
huge PVT spread, netlist never committed) -> `#1795` (comparator/bias
redesign attempt; ruled out "raise the bias current" and a passive
Vgs/Vsg bias generator) -> `#1813` (found the headroom/saturation ceiling
that blocks *any single* comparator spanning both thresholds at
`ss`/`-40C`/`1.62V`) ->
[the comparator-core spike](relaxation-oscillator-comparator-core-spike.md)
(split-polarity dual comparator clears that ceiling, 18/18 corners, but
is still a standalone comparator on a triangular-ramp testbench) -> **this
document** (that core assembled into a working oscillator).

## What was missing, and what it became

The spike's "What is deliberately still missing" list, item by item:

| Missing piece (spike doc) | What this assembly uses |
|---|---|
| Charge/discharge current source (`Biosc`-equivalent) + timing capacitor | `XMchg` (PMOS) / `XMdis` (NMOS) mirrors at ~1.95:1 off the *same* `pbias`/`nbias` rails the comparator core already biases from, through complementary switches `XSWchg`/`XSWdis` both gated by `q`, into `Ccap` = 10 pF with `Rleak` = 1 Gohm for a DC path |
| Output edge-combination logic (SR-latch-equivalent) | An explicit `NOT(olo)` inverter feeding a cross-coupled CMOS NOR SR latch: `S = ohi`, `R = NOT(olo)`. `olo` pulses *low* on the lower-threshold crossing (the comparator core's `out` is high when `in > vref`), and a NOR latch needs active-high inputs — the inverter is that polarity fix, not decoration. `q` is simultaneously the clock output and the single charge/discharge control, which is what makes one control signal enough: a PMOS and an NMOS switch are on at opposite gate polarities |
| Device-level `vth_lo`/`vth_hi` reference generator | `XMref` mirrors `{ib}` into a real `sky130_fd_pr__res_xhigh_po` divider (`XRdiv1` sets `vth_lo = ib*r1`, `XRdiv2` sets the spacing `ib*r2`), with 0.5 pF decoupling on each threshold node. Not a bandgap — justification below |
| Full 18-corner oscillation validation | `benchmarks/design-agent/reference/rc-relaxation-oscillator/device-oscillator/sim_request.json`, gated on the task's own 1.4-2.2 us band |

Netlist:
[`benchmarks/design-agent/reference/rc-relaxation-oscillator/device-oscillator/oscillator_device.spice`](../../benchmarks/design-agent/reference/rc-relaxation-oscillator/device-oscillator/oscillator_device.spice).
Full per-corner result table and the "ship alongside, do not replace"
decision:
[that directory's `README.md`](../../benchmarks/design-agent/reference/rc-relaxation-oscillator/device-oscillator/README.md).

The `cmp_n`/`cmp_p` `.subckt` bodies are byte-identical to the spike's.
Nothing about the comparator core was re-tuned to make the oscillator
close.

## Result

**18/18 corners pass** (`tt`/`ss`/`ff` x 1.62/1.98 V x -40/27/125 C, real
sky130A devices, standalone `ngspice-46` via `klt sim`, `2026-09-14`):

- Period 1.4876-2.0542 us against the task's 1.4-2.2 us band — 6.3%
  ratio margin at the fast end, 7.1% at the slow end, against the 6.7%
  each side an optimally-centred design with this spread would get.
- Cycle-to-cycle stability within +/-0.32% at every corner.
- The timing node ramps between the two thresholds at every corner
  (gated `vo_high <= 1.55 V`, `vo_low >= 0.15 V`) rather than railing.
- Self-starting at every corner with no start-up kick source.

## Three things the assembly turned up that the spike could not have

### 1. The `.op` point does not resolve the latch — and that is not what starts it

The obvious story for a latch-based astable is "the latch is bistable, so
`.op` lands on one of the two states and the transient takes it from
there." That story is wrong here, and it was worth measuring rather than
assuming: at `ss`/`1.62V`/`-40C`, `v(q)` at `t=0` is **0.5715 V** —
mid-rail, neither state.

What actually starts it is the *timing node*. The same `.op` leaves `vo`
at **1.3623 V**, above that corner's 1.3025 V `vth_hi`, because with both
charge and discharge mirrors partially on and only `Rleak` to ground, the
DC balance point for `vo` is not inside the threshold band. So `cmp_n` is
unambiguously asserting at `t=0`, the latch's own positive feedback
resolves `q` within a few ns, and the first full edge lands at 0.24 us.

This makes the start-up argument a property of the charge/discharge
network's DC balance, not of the latch — a distinction that matters if
anyone later rebalances the mirrors (e.g. to re-centre the period) and
accidentally moves the `.op` value of `vo` *between* the thresholds.

### 2. Start-up edge counts are integration-timestep-dependent

The first version of this testbench measured the period between rising
edges 3 and 4 of `q`, counted from `t=0`. At `ss`/`1.62V`/`-40C` with a
5 ns maxstep that reported a 2.1378 us period; with a 2 ns maxstep the
same corner reported 2.06 us, because the coarser step saw two extra
`v(q) = 0.75` crossings inside the start-up transient and so "edge 3" was
a different edge in the two runs.

The fix in the shipped request is to `TD`-gate every edge measurement to
start at 6 us. A period is then the gap between two consecutive
steady-state edges regardless of what index they carry, and the result
stops depending on the integrator. The request also reports
`period_drift_pct` (first steady-state period vs. the 3-period average
after it) with a +/-1% gate, so a future regression that *does* disturb
the steady state fails the sweep instead of silently shifting a number.

**Generalisable point for this benchmark family:** a `RISE=<n>`-indexed
`.meas` on an oscillator is only as trustworthy as the assumption that
every corner has the same number of start-up crossings. It does not.

### 3. The high-impedance reference nodes needed decoupling, and the missing decoupling was hiding in a measurement

Both threshold nodes sit at ~70 kohm and ~230 kohm to ground. Each
comparator's input pair kicks charge onto its own threshold node through
`Cgd` exactly as it switches — that is, exactly at the moment that
threshold determines the edge.

Before `Crefl`/`Crefh` were added, the undecoupled kickback showed up as a
*measurement* artifact first: a `FIND v(vth_hi) AT=1u` snapshot at
`ff`/`1.62V`/`-40C` read 1.4224 V, against 1.3006 V for the same corner
once decoupled — a 122 mV instantaneous excursion that made `dv_eff` look
like a 1.00 V outlier in an otherwise 0.74-0.94 V matrix. Two changes,
both in the shipped version:

- 0.5 pF on each threshold node — ~15x the input pair's total gate
  capacitance at these geometries, and far more than its gate-drain
  overlap — which holds the threshold still across an edge while leaving
  ~35 ns / ~115 ns node time constants, settled long before the 6 us
  measurement gate;
- threshold measurements changed from an instantaneous `FIND ... AT=` to
  `AVG ... FROM=6u TO=18u`, so a residual excursion cannot be mistaken
  for the reference's value.

## Where the PVT spread actually comes from

The period is, to first order,

```
T = 2 * Cosc * dV / iosc      with  dV = ib*r2  and  iosc = k*ib
  = 2 * Cosc * r2 / k
```

so the bias current `{ib}` — still an ideal DC source here, inherited from
the spike — **cancels out of the period**. That is worth stating plainly
because it bounds how much the remaining ideal element can matter: a real
bias generator would move the period only through second-order effects,
not through its absolute value.

What does not cancel is `r2`'s temperature coefficient, and that turns out
to own essentially the whole spread:

| axis | swing in `dv_eff` |
|---|---|
| Temperature, -40 -> 125 C | **-18.5%** (`res_xhigh_po`'s `rbody` carries `tc1 = -1.47e-3`/C, head segments `-4.3e-4`/C) |
| Supply, 1.62 -> 1.98 V | +1.6% (`XMref` channel-length modulation; its `\|Vds\|` is `vdd - vth_hi`) |
| Process, `tt`/`ss`/`ff` | ~0.1% |

The measured 38% period spread is larger than the 18.5% `dV` swing because
the comparator + latch delay adds a roughly fixed overshoot — ~50 mV past
`vth_hi` and ~22 mV past `vth_lo`, about 8% of `dV` — which is a larger
*fraction* of the smaller high-temperature `dV`, and because the mirror
current drifts slightly the other way (10.0 uA at -40C, 10.2 uA at 125C
by `iosc = 2*Cosc*(vo_high - vo_low)/T`).

This is a clean handoff: the highest-value follow-on change is not in the
comparator, the latch, or the switches — it is compensating one resistor's
tempco, either with a composite negative-TC-poly + positive-TC-diffusion
divider (no new active circuitry) or with #1795's originally scoped
PTAT/CTAT bias generator making `iosc` track `dV`.

## Reference generator: why a divider and not a bandgap

Issue #1814's scope item 3 allowed "bandgap-derived per
`kb/entries/sky130-bandgap-reference.json`, or a documented simpler
stand-in, with the choice justified". This uses the stand-in:

1. **The KB entry's own artifact is itself behavioral.**
   `examples/kb/sky130-bandgap-reference/testbench.spice` models the
   PTAT/CTAT current summing with ideal diodes and ideal current sources,
   not sky130's parasitic PNP devices — its own `measured.notes` says so.
   Instantiating it would have put ideal elements back into a netlist
   whose entire purpose is being device-level.
2. **Its output is 0.209 V**, a current-domain summing node, not the
   0.35 V / 1.15 V pair the split-polarity comparator core needs; it would
   have needed a gain stage on top regardless.
3. **A real curvature-corrected Banba bandgap on sky130** (real
   `sky130_fd_pr__pnp_05v5` devices, trimmed resistor ratios) does not
   exist in this repo and is a standalone design effort — the same
   deferred territory as scope item 5.

The stand-in is still a real step past the spike's ideal `Vrl`/`Vrh` DC
sources: both thresholds are set by real MOSFET and real poly-resistor
devices and carry the real PVT sensitivity quantified above. It buys no
first-order temperature cancellation — which is precisely why the resistor
tempco dominates.

## Solver-diagnostic caveat (not a design defect)

Most corners emit `Warning: Dynamic gmin stepping failed`, and about half
additionally emit `Warning: singular matrix: check node vth_lo`, during
the DC operating-point solve; source stepping then completes and the
transient converges at all 18 corners.

The singular-matrix note is attributable to the **sky130 resistor model**,
not to this topology — verified directly, not inferred: replacing
`XRdiv1`/`XRdiv2` with ideal `R` elements of the same value removes it
with nothing else changed. `sky130_fd_pr__res_xhigh_po` expresses its body
resistance as a bias-dependent formula containing `abs(v(t1,t2))`, whose
Jacobian is discontinuous at exactly the zero bias gmin/source stepping
starts from. This is the assembly-level counterpart of the spike doc's own
`--op-lint` caveat: worth knowing before chasing it as a netlist bug.

A `.nodeset` on the threshold and timing nodes was tried as a mitigation
and does **not** remove the warning; the shipped netlist keeps the real
resistor device and no nodeset.

## Does this replace the shipped reference solution?

**No — it ships alongside.** `benchmarks/design-agent/reference/
rc-relaxation-oscillator/oscillator.spice` (behavioral, PDK-model-free)
remains what `tasks/rc-relaxation-oscillator.json` scores against. The
full reasoning — the benchmark's "runs anywhere `ngspice` runs" property,
the measured 5.8 s vs. ~5 min reference-gate runtime, the ~6% vs.
comfortable band margin, and the `eval_descriptor` contract change a swap
would imply — is
recorded in
[the artifact directory's `README.md`](../../benchmarks/design-agent/reference/rc-relaxation-oscillator/device-oscillator/README.md#decision-ship-alongside-do-not-replace),
along with the conditions under which the decision should be revisited.
`benchmarks/design-agent/README.md`'s "Known limitations" #2 is updated to
point at it.
