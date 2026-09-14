# Spike: a comparator core that clears the sky130A `ss`/`-40C`/`1.62V`
headroom ceiling (issue #1813)

**Status:** investigation record, closing out issue #1813's own scope
(Recommended next steps 1-3). Next step 4 — assembling this core into a
full device-level relaxation oscillator and resuming #1795's originally
scoped regenerative-feedback / PTAT-CTAT bias-generator work — is
deliberately **not** attempted here; it is filed as its own follow-on issue
(linked from #1813 once opened) per #1813's own next-step-4 framing
("*only once* a comparator core resolves reliably... should the
regenerative-feedback... work resume").

## Chain

`#1789` (device-level oscillator core, 18-corner period sweep, huge PVT
spread) -> `#1795` (comparator/bias-generator redesign attempt, ruled out
"raise bias current" and a passive Vgs/Vsg bias generator) -> `#1813`
(this doc's predecessor: discovered that a *single* comparator spanning
both thresholds hits a headroom/saturation ceiling roughly 0.6-0.7 V from
either rail at `ss`/`-40C`/`1.62V`, independent of bias current or input
polarity) -> this spike.

See #1813's own issue body for the full evidence table behind that
ceiling; it is not reproduced here.

## The fix: stop asking one comparator to do both jobs

#1813's own "Recommended next steps" listed two alternatives: (1) a
properly-biased 2-stage/cascoded comparator, or (2) *"a fundamentally
different comparator architecture less sensitive to this headroom
ceiling... an architecture that never asks a single differential stage to
resolve an input that's simultaneously far from both its own tail rail and
its threshold's rail at once."*

This spike pursues (2) directly rather than iterating further on a 2-stage
design, because the ceiling #1813 measured is a description of exactly the
situation clause (2) names: a single comparator whose input range must
span both the low threshold (near the negative rail, where only an
NMOS-input pair still has headroom) and the high threshold (near the
positive rail, where only a PMOS-input pair still has headroom) is, by
construction, asked to be accurate somewhere that is far from *both*
kinds of headroom at once. Splitting the job removes the ceiling instead
of trying to push through it:

- `cmp_n` — NMOS-input pair, PMOS mirror load — owns the **upper**
  threshold `vth_hi`, where the PMOS load still has tail headroom.
- `cmp_p` — PMOS-input pair, NMOS mirror load — owns the **lower**
  threshold `vth_lo`, where the PMOS input pair still has headroom.

Each comparator only has to be *accurate* near its own threshold. Away
from its own threshold each comparator is slow (its tail device sits in
triode) but its output polarity still rails correctly — which is all a
relaxation oscillator's edge-detection ever needs from the "wrong-side"
comparator, since the oscillator's actual edge always comes from whichever
comparator owns the threshold currently being crossed.

The other sizing change that matters (see `comparator_core.spice`'s own
header for the full derivation): the PMOS mirror load's own `|Vsg|` — not
the input pair's `gm` — is what was eating the headroom budget in #1813's
single-comparator attempts. Widening the load geometry roughly 13x (`L=0.5
W=40 nf=2` vs. the baseline 5T-OTA's `L=1 W=6`) at a *lower* bias current
(5 uA vs. 20 uA) buys back the input common-mode range directly, because a
wider/lower-current diode-connected load needs less `|Vsg|` to pass the
same current. This is also why #1813's "raise `icmp_bias`" sweep made
things worse: more current raises the *existing* load's own `|Vsg|`,
spending the same fixed voltage budget it was trying to free.

Netlist: [`../../benchmarks/design-agent/reference/rc-relaxation-oscillator/comparator-core/comparator_core.spice`](../../benchmarks/design-agent/reference/rc-relaxation-oscillator/comparator-core/comparator_core.spice).
Testbench: the same file's `sim_request.json` sibling — a triangular input
ramp (1 V/us, matching the actual `dv/dt` the timing capacitor sees in the
behavioral reference solution) crossing both thresholds once rising and
once falling, so one 3 us transient yields each comparator's DC trip point
(average of its rise/fall crossing, delay cancels) and propagation delay
(half their difference) per corner.

## Evidence (real sky130A, standalone `ngspice-46` via `klt sim`, full
18-corner sky130-style matrix — `tt`/`ss`/`ff` x 1.62/1.98 V x
-40/27/125 C, `2026-09-15`)

`klt sim benchmarks/design-agent/reference/rc-relaxation-oscillator/comparator-core/sim_request.json`
— **18/18 corners pass**, including the `ss`/`-40C`/`1.62V` corner #1813
identified as the worst case:

| corner | `vtrip_lo` (V) | `vtrip_hi` (V) | `dv_eff` (V) | `tpd_lo` (ns) | `tpd_hi` (ns) |
|---|---|---|---|---|---|
| tt/1.62V/-40C | 0.3526 | 1.1506 | 0.7981 | 16.74 | 14.54 |
| tt/1.62V/27C | 0.3554 | 1.1480 | 0.7926 | 17.39 | 15.01 |
| tt/1.62V/125C | 0.3606 | 1.1449 | 0.7843 | 16.73 | 15.84 |
| tt/1.98V/-40C | 0.3559 | 1.1468 | 0.7908 | 15.72 | 15.23 |
| tt/1.98V/27C | 0.3592 | 1.1446 | 0.7855 | 16.56 | 16.50 |
| tt/1.98V/125C | 0.3652 | 1.1419 | 0.7768 | 15.53 | 17.99 |
| **ss/1.62V/-40C** (worst corner) | **0.3515** | **1.1511** | **0.7996** | **19.38** | **17.51** |
| ss/1.62V/27C | 0.3545 | 1.1485 | 0.7941 | 19.21 | 16.79 |
| ss/1.62V/125C | 0.3602 | 1.1458 | 0.7857 | 18.46 | 17.62 |
| ss/1.98V/-40C | 0.3550 | 1.1469 | 0.7919 | 16.70 | 17.01 |
| ss/1.98V/27C | 0.3583 | 1.1452 | 0.7868 | 17.66 | 18.25 |
| ss/1.98V/125C | 0.3648 | 1.1430 | 0.7783 | 16.99 | 19.88 |
| ff/1.62V/-40C | 0.3536 | 1.1510 | 0.7974 | 14.99 | 12.80 |
| ff/1.62V/27C | 0.3564 | 1.1480 | 0.7916 | 15.88 | 13.35 |
| ff/1.62V/125C | 0.3616 | 1.1440 | 0.7824 | 15.34 | 14.08 |
| ff/1.98V/-40C | 0.3569 | 1.1466 | 0.7898 | 14.10 | 13.77 |
| ff/1.98V/27C | 0.3602 | 1.1442 | 0.7840 | 15.22 | 14.98 |
| ff/1.98V/125C | 0.3661 | 1.1407 | 0.7746 | 14.45 | 16.15 |

Both output rails swing full-scale at every corner (`volo`/`vohi` high
>= 1.55 V, low <= 0.05 V — well inside the sim request's own gates), and
both comparators cleanly resolve their own threshold at the worst corner:
`ss`/`-40C`/`1.62V` shows `dv_eff = 0.7996 V`, the *largest* value in the
whole matrix, not the smallest — the opposite of #1813's single-comparator
finding, where that exact corner was the one that failed to resolve at
all. Propagation delay stays in a tight 12.8-19.9 ns band everywhere,
comfortably under the sim request's own 40 ns ceiling.

## Re-deriving the achievable `dV` (next step 3)

#1813 speculated that a realistic ceiling might be "on the order of a few
hundred mV, not `vref`-scale" — i.e. that resolving a working comparator
core might force a much smaller threshold spacing than the behavioral
reference solution assumed (`dV = vref = 0.9 V`, thresholds at `0.5x`/
`1.5x` `vref` with `vref_nom = 0.9 V`, i.e. `0.45 V`/`1.35 V`).

That speculation does **not** hold for the split-polarity topology: with
thresholds placed at `vth_lo = 0.35 V` / `vth_hi = 1.15 V` (deliberately
close to the behavioral design's own `0.45 V`/`1.35 V`, pulled in slightly
toward mid-supply to leave the sizing headroom this topology still needs
near each rail), the achieved `dv_eff` is **0.7746-0.7996 V across all 18
corners** — within about 11-14% of the original `dV = 0.9 V` assumption,
not an order of magnitude smaller. The `dv_eff` spread across the full PVT
matrix is also tight (about 3%, 0.775-0.800 V), which is a good sign for
the oscillator's eventual PVT-delay-insensitivity vs. offset-sensitivity
tradeoff (#1813's next-step-3 concern): a `dV` this close to the original
design intent does not obviously force a harsher offset-sensitivity
tradeoff than the behavioral reference already assumed. This is evidence,
not a proof — offset/mismatch was not modeled in this DC-sweep spike (see
"What is deliberately still missing" below) — but it means the concern
should not, by itself, block moving forward with this topology.

## Op-lint caveat (not a design defect)

`klt sim --op-lint` on this netlist reports two `floating_node` warnings
for `ohi`/`olo` — these are false positives from the current `--op-lint`
implementation not flattening `.subckt` hierarchy: both nodes have three
element terminals once `cmp_n`/`cmp_p`'s internal output-buffer stage is
expanded (the output capacitor plus both output-inverter drains), and the
full transient sweep above converges cleanly at every one of the 18
corners with no singular-matrix/gmin-stepping diagnostics. Worth knowing
if `--op-lint` is run against this netlist again, not worth chasing as a
netlist bug.

## What is deliberately still missing

This is a comparator *core* on its own testbench, not a full oscillator:

- No charge/discharge current source (`Biosc`-equivalent) or timing
  capacitor — the DC/transient trip points measured here are against a
  clean triangular ramp, not the timing capacitor's own actual RC/current
  waveform.
- No output edge-combination logic (e.g. an SR-latch-equivalent) to turn
  `ohi`/`olo`'s two independent comparator outputs into the single clock
  edge a relaxation oscillator's charge/discharge switch needs.
- No device-level reference generator for `vth_lo`/`vth_hi` — both are
  ideal DC voltage sources here, standing in for the bandgap-derived
  reference `kb/entries/sky130-bandgap-reference.json` describes (same
  stand-in convention the behavioral `oscillator.spice` reference solution
  already documents for its own `vref_nom`).
- #1795's originally scoped regenerative-feedback investigation (positive
  feedback to fix the `1/sqrt(I)` delay-vs-bias-current scaling #1789
  measured) has not been revisited; this spike only closes the *headroom/
  resolution* blocker #1813 found ahead of that step, per #1813's own
  next-step-4 ordering.

These are the shape of the follow-on issue this spike hands off to.
