# Device-level relaxation oscillator (issues #1814, #1795)

A full, working sky130A **device-level** RC relaxation oscillator,
assembled around the split-polarity dual-comparator core
[`../comparator-core/`](../comparator-core/) landed by
[#1813](https://github.com/2AMLogic/klayout-tools/issues/1813). It
oscillates and holds `benchmarks/design-agent/tasks/rc-relaxation-
oscillator.json`'s own 1.4-2.2 us period band across all 18 sky130A PVT
corners.

[#1795](https://github.com/2AMLogic/klayout-tools/issues/1795) then
temperature-compensated the reference divider — the tempco this README's
own Handoff section had identified as owning essentially the whole
spread. The measured 18-corner period spread fell from **38.1% to 4.5%**
and band margin grew from **6.3%/7.1%** to **23.5%/21.8%**, past the 20%+
figure the Handoff section set as its target. Everything below is
re-measured against the compensated netlist; see
[Where the PVT spread comes from](#where-the-pvt-spread-comes-from) for
what is left.

**This is not the task's shipped reference solution** — see
[Decision: ship alongside, do not replace](#decision-ship-alongside-do-not-replace)
below for the explicit call issue #1814 asked for. The file the task
actually scores against remains the behavioral
[`../oscillator.spice`](../oscillator.spice).

[#2156](https://github.com/2AMLogic/klayout-tools/issues/2156) then
validated start-up against comparator offset and device mismatch — the
open item #1795 left behind, because compensation also shrank the `.op`
start-up margin ~10x. Both sweeps come back clean (**44/44** deterministic
offset cases, **128/128** mismatch Monte Carlo samples), and they explain
*why* the thin margin was never the risk it looked like; see
[Start-up under comparator offset and mismatch](#start-up-under-comparator-offset-and-mismatch).

Reproduce:

```
# the 18-corner PVT gate (unchanged by #2156)
klt sim benchmarks/design-agent/reference/rc-relaxation-oscillator/device-oscillator/sim_request.json

# start-up under a deterministic worst-case comparator offset (44 cases)
klt sim benchmarks/design-agent/reference/rc-relaxation-oscillator/device-oscillator/sim_request_startup_offset.json

# start-up under sky130 MOS device mismatch (4 corners x 32 samples)
klt sim benchmarks/design-agent/reference/rc-relaxation-oscillator/device-oscillator/sim_request_startup_mismatch.json
```

(18 corners x a 20 us transient of real sky130A devices. ~5 minutes wall
on 4 idle workers when #1814 measured it; the #1795 re-run reported
~740 s of CPU *per corner* because the host was running a saturated agent
fleet at the time — take the wall-clock figure as a quiet-machine number,
and the per-corner numbers in the report as load-dependent. The two
start-up sweeps are 26 us transients and are load-dependent the same
way: on 4 workers of a loaded 8-core host the 44 offset cases took
~63 CPU-minutes / ~16 minutes wall, and the 128 mismatch samples scale
from the same ~60-170 s per case. Expect roughly 11 min / 32 min on a
quiet machine and half again as long under a busy agent fleet.)

## What got assembled

`docs/design/relaxation-oscillator-comparator-core-spike.md`'s "What is
deliberately still missing" section listed four pieces the comparator-core
spike did not have. All four are in
[`oscillator_device.spice`](oscillator_device.spice), plus the start-up
validation #2156 added on top of them:

| Piece | Realisation |
|---|---|
| Charge/discharge current source + timing capacitor | `XMchg`/`XMdis` current mirrors (~1.95:1 off the same `pbias`/`nbias` rails the comparator core already uses) through complementary switches `XSWchg`/`XSWdis`, into `Ccap` = 10 pF |
| Output edge-combination logic | `NOT(olo)` inverter + cross-coupled CMOS NOR SR latch; `S = ohi`, `R = NOT(olo)`, `q` is the clock and also the single charge/discharge control |
| Device-level `vth_lo`/`vth_hi` reference generator | `XMref` mirrors `{ib}` into a **zero-TC composite divider** — each leg a series `sky130_fd_pr__res_xhigh_po` (negative tempco) + `sky130_fd_pr__res_generic_nd` (positive tempco) pair (`XRdiv1`/`XRdiv1n`, `XRdiv2`/`XRdiv2n`), with 0.5 pF `Crefl`/`Crefh` decoupling against comparator kickback — replaces the spike's ideal `Vrl`/`Vrh` DC sources |
| 18-corner oscillation validation | [`sim_request.json`](sim_request.json), results below |
| Start-up validation under comparator offset and device mismatch (#2156) | [`sim_request_startup_offset.json`](sim_request_startup_offset.json) (44 deterministic `Voff` cases) and [`sim_request_startup_mismatch.json`](sim_request_startup_mismatch.json) (4 corners x 32 MOS-mismatch samples), via the `Voffhi`/`Vofflo` injection sources; results in [Start-up under comparator offset and mismatch](#start-up-under-comparator-offset-and-mismatch) |

The `cmp_n`/`cmp_p` `.subckt` bodies are copied **verbatim** from
`../comparator-core/comparator_core.spice` — nothing about the comparator
core itself was re-tuned, by #1814's assembly or by #1795's compensation.
`{ib}`, the `XMchg`/`XMdis` mirror ratio, `Cosc`, the latch, and the
switches are all untouched by #1795 as well: the only netlist change that
issue made was splitting the two divider resistors into four and
re-centring their total. #2156's netlist change is smaller still and
electrically a no-op at its defaults — two 0 V series sources on the
comparator `vref` pins, there purely so a request can sweep them; see
[Start-up under comparator offset and mismatch](#start-up-under-comparator-offset-and-mismatch)
for the 18/18 re-run that confirms it.

The **bias generator** half of #1814's scope item 5 (#1795's originally
scoped PTAT/CTAT work) is still not here — and, as
[Reference generator](#reference-generator-why-not-a-bandgap) explains,
would not have helped. See
[Handoff](#handoff-what-the-follow-on-should-attack) below for what is
actually left.

## Evidence

`klt sim .../device-oscillator/sim_request.json`, real sky130A devices,
standalone `ngspice-46`, `sky130A` @ `f6eeac7`, full 18-corner matrix
(`tt`/`ss`/`ff` x 1.62/1.98 V x -40/27/125 C) — **18/18 corners pass**,
gated on the task's own 1.4-2.2 us band. Re-measured 2026-09-18 against
the compensated netlist; `sim_request.json` itself is byte-identical to
what #1814 shipped, so this is the same gate, not a relaxed one:

> **Known stale cross-reference.** `benchmarks/design-agent/README.md`'s
> "Known limitations" section still quotes the pre-compensation numbers
> (1.4876-2.0542 us, ~6-7% margin, "a real `sky130_fd_pr__res_xhigh_po`
> divider"). Issue #1795 put that file explicitly out of scope, so it was
> left alone rather than silently edited; this table is the current data.

| corner | `vth_lo` (V) | `vth_hi` (V) | `dv_eff` (V) | `period_avg` (us) | drift (%) | `vo` high/low (V) | overshoot (V) |
|---|---|---|---|---|---|---|---|
| tt/1.62V/-40C | 0.3642 | 1.2224 | 0.8582 | 1.7969 | -0.11 | 1.2425 / 0.3434 | 0.0409 |
| tt/1.62V/27C | 0.3622 | 1.2153 | 0.8531 | 1.7590 | -0.08 | 1.2320 / 0.3429 | 0.0360 |
| tt/1.62V/125C | 0.3654 | 1.2302 | 0.8648 | 1.7388 | -0.34 | 1.2433 / 0.3513 | 0.0273 |
| tt/1.98V/-40C | 0.3702 | 1.2432 | 0.8730 | 1.7849 | +0.23 | 1.2608 / 0.3475 | 0.0404 |
| tt/1.98V/27C | 0.3684 | 1.2358 | 0.8674 | 1.7526 | +0.05 | 1.2512 / 0.3515 | 0.0323 |
| tt/1.98V/125C | 0.3718 | 1.2516 | 0.8798 | 1.7476 | +0.04 | 1.2674 / 0.3619 | 0.0258 |
| **ss/1.62V/-40C** (slowest) | **0.3641** | **1.2221** | **0.8580** | **1.8067** | **+0.12** | **1.2462 / 0.3397** | **0.0485** |
| ss/1.62V/27C | 0.3620 | 1.2147 | 0.8527 | 1.7657 | +0.02 | 1.2338 / 0.3398 | 0.0412 |
| ss/1.62V/125C | 0.3651 | 1.2291 | 0.8640 | 1.7527 | -0.09 | 1.2452 / 0.3490 | 0.0322 |
| ss/1.98V/-40C | 0.3705 | 1.2432 | 0.8727 | 1.7890 | -0.11 | 1.2618 / 0.3490 | 0.0401 |
| ss/1.98V/27C | 0.3681 | 1.2348 | 0.8667 | 1.7620 | -0.24 | 1.2530 / 0.3486 | 0.0377 |
| ss/1.98V/125C | 0.3714 | 1.2501 | 0.8787 | 1.7518 | +0.19 | 1.2685 / 0.3595 | 0.0303 |
| ff/1.62V/-40C | 0.3643 | 1.2228 | 0.8585 | 1.7883 | -0.15 | 1.2418 / 0.3434 | 0.0398 |
| ff/1.62V/27C | 0.3624 | 1.2159 | 0.8535 | 1.7482 | -0.03 | 1.2311 / 0.3460 | 0.0316 |
| **ff/1.62V/125C** (fastest) | **0.3657** | **1.2313** | **0.8656** | **1.7289** | **+0.07** | **1.2410 / 0.3537** | **0.0217** |
| ff/1.98V/-40C | 0.3711 | 1.2448 | 0.8737 | 1.7832 | +0.23 | 1.2603 / 0.3503 | 0.0362 |
| ff/1.98V/27C | 0.3686 | 1.2368 | 0.8682 | 1.7470 | +0.08 | 1.2507 / 0.3545 | 0.0280 |
| ff/1.98V/125C | 0.3722 | 1.2530 | 0.8808 | 1.7337 | +0.02 | 1.2670 / 0.3645 | 0.0217 |

"overshoot" is `(vo_high - vth_hi) + (vth_lo - vo_low)`: how far past each
threshold the timing node coasts while the comparator and latch resolve.
It is derived from the four measured columns to its left, not separately
gated.

- **Period**: 1.7289-1.8067 us — inside the 1.4-2.2 us band at every
  corner, with the fast end clearing its limit by **23.5%**
  (`1.7289/1.4`) and the slow end by **21.8%** (`2.2/1.8067`). Total
  spread is 4.50%; a perfectly-centred design with that spread would get
  22.6% on each side, so the shipped centring is within 0.8 points of
  optimal and is not worth another sweep to chase.
  - Before #1795's compensation: 1.4876-2.0542 us, 38.1% spread,
    6.3%/7.1% margin. Every number in this table moved.
- **The clock is stable, not just present.** `period_drift_pct` compares
  the first steady-state period against the 3-period average that follows
  it; it stays within +/-0.35% at every corner, well inside the request's
  own +/-1% gate. Four consecutive steady-state edges were found at all
  18 corners — which is also the evidence that the compensated divider
  introduces no new bistable/lock-up state, though it introduces no
  regenerative element that could cause one either (see
  [Reference generator](#reference-generator-why-not-a-bandgap)).
- **`vo` stays between the thresholds** at every corner (gated `vo_high <=
  1.55 V`, `vo_low >= 0.15 V`; worst measured 1.2685 V / 0.3397 V) — i.e.
  the timing node really is ramping between two comparator trips, not
  railing to a supply with the clock coming from somewhere else.
- **`XMref`'s headroom improved, which was the corner most at risk.**
  Its `|Vds|` is `vdd - vth_hi`, smallest at 1.62 V. Flattening the
  divider pulls `vth_hi` down at cold and up at hot, so the worst case
  over the whole matrix is now **0.389 V** (`ff`/1.62V/125C) against
  **0.317 V** before (`tt`/1.62V/-40C, the corner #1814's Handoff note
  flagged). The compensation *relieved* that constraint rather than
  tightening it.
- **Self-starting**, with no `Istart`-equivalent kick: `.op` does *not*
  resolve the latch (measured `v(q) = 0.5726 V` at t=0, mid-rail, at
  ss/1.62V/-40C) but it does leave `vo` at 1.2276 V, above that corner's
  1.2217 V `vth_hi`, so one comparator is asserting and the latch's own
  positive feedback resolves it — measured first rising edge on `q` at
  **64.5 ns**. All 18 corners reach 4 clean steady-state edges after the
  6 us measurement gate, which is only possible if every one of them
  started.
  - **The `.op` start-up margin got much thinner, and that is worth
    knowing.** The `vo - vth_hi` gap is now 5.9 mV at ss/1.62V/-40C and
    1.2-2.3 mV at 1.62V/27C (measured `tt`/`ss`/`ff`), where the all-poly
    divider left 59.8 mV: both `vo`'s DC balance point and `vth_hi` moved
    down with compensation, and not by the same amount.
  - This does **not** mean start-up now hangs on a few millivolts, and
    that is now **measured rather than argued** (#2156). The gap is a
    feedback-loop DC residual, not a headroom: an input-referred
    comparator offset moves `vo` and the effective threshold *together*,
    so ±50 mV of injected offset — 8-40x the margin — changes the margin
    by about half a millivolt. Every one of 44 deterministic offset cases
    and 128 mismatch Monte Carlo samples starts and sustains
    oscillation. Full result:
    [Start-up under comparator offset and mismatch](#start-up-under-comparator-offset-and-mismatch).

### Start-up under comparator offset and mismatch

Issue [#2156](https://github.com/2AMLogic/klayout-tools/issues/2156).
Before this, nothing in this chain — not `../comparator-core/`, not this
directory — modelled comparator input offset or device mismatch at all.
That was defensible while `.op` left ~60 mV between `vo` and `vth_hi`;
at 1.2-5.9 mV it was not, because a realistic sky130 comparator's
input-referred offset is the same order as the measured margin.

**How the offset is injected.** `oscillator_device.spice` gains two DC
sources, `Voffhi`/`Vofflo`, in series with each comparator's `vref` pin
(`Xhi` now sees `vth_hi_in = vth_hi + voffhi`, `Xlo` sees
`vth_lo_in = vth_lo + vofflo`). Injecting at `vref` rather than at `in`
is the input-referred equivalent of a real input-pair Vth/beta mismatch
and leaves the timing node `vo` undisturbed. Both default to **exactly
0 V**, which is an ideal short, and they follow the `.param name / DC
{name}` convention `docs/cli/sim.md` documents for `corners.supply_v`
keys — so a request sweeps them the same way it sweeps `vdd`.

**The default really is a no-op**, checked rather than asserted:
`sim_request.json`'s full 18-corner sweep re-run against the modified
netlist is **18/18 pass** with every `period_avg` within **0.41%** of
the table above (worst `tt`/1.62 V/27 C, -0.410%; the residual is the
sky130A version difference, not this change), and a standalone `.op`
against the modified netlist returns **bit-identical** node voltages to
the same `.op` against the pre-#2156 one — which are in turn #1795's
published figures to their published precision (ss/1.62V/-40C:
`vo` 1.227632 V, `vth_hi` 1.221691 V, `q` 0.5725938 V).
`sim_request.json` itself is untouched.

#### The headline: the margin is a loop residual, not a headroom

The intuition the issue was filed on — "only 1.2 mV of margin, so a few
mV of offset could close it" — is wrong, and the reason is structural.
At the metastable `.op`, `cmp_n` sits in its linear region and the loop
`cmp_n -> latch -> charge/discharge switches -> vo` holds `vo` wherever
`ohi` lands mid-rail. **Move the effective threshold and `vo` moves with
it.** The sweep's own `startup_margin_mv`
(`1000 * (v(vo) - v(vth_hi_in))` at t=0), in mV — read off the 44 cases
below, so it is reproducible from the committed request rather than from
a side deck:

| `voffhi` (mV) | -50 | -25 | -10 | 0 | +10 | +25 | +50 |
|---|---|---|---|---|---|---|---|
| tt/27C | 1.481 | 1.557 | 1.612 | **1.654** | 1.701 | 1.783 | 1.961 |
| ss/27C | 2.034 | 2.141 | 2.219 | **2.279** | 2.346 | 2.464 | 2.727 |
| ff/27C | 1.102 | 1.161 | 1.203 | **1.236** | 1.272 | 1.334 | 1.472 |
| ss/-40C | 4.350 | 4.906 | 5.425 | *resolved* | 6.579 † | *resolved* | 14.900 |

Across 100 mV of `voffhi` the margin moves by **0.48 mV** at `tt`/27C,
0.69 mV at `ss`/27C and 0.37 mV at `ff`/27C — the loop attenuates
input-referred offset by **208x / 144x / 271x** respectively, and the
margin never changes sign. At those three corners the margin is a
function of `voffhi` *alone*, to six digits: the `+10/-10` and `+10/+10`
rows agree exactly, as do the two `±50` pairs.

`ss`/-40C is the loose one — 10.55 mV of margin movement across the same
100 mV, only **9.5x** attenuation — and it is also the only corner where
the t=0 solve ever lands somewhere other than the knife edge.
*resolved* marks the three of the 44 cases (all at `ss`/-40C: `0/0`,
`+10/+10`, `+25/-25`) where it settled on a **fully resolved latch
state** instead — `q` at the upper rail, margin -264 to -671 mV. That is
a *better* start condition, not a worse one: it is the non-marginal
case, and all three still oscillate. († the `+10` cell shows the
`+10/-10` row; its `+10/+10` sibling is one of those three.)

**Which side of the knife edge a DC solver lands on is numerically
fragile, and that is itself the point.** At `ss`/1.62 V/-40 C a
standalone `.op` deck lands on the metastable root — `vo` 1.227632 V,
`vth_hi` 1.221691 V, `q` 0.5725938 V, i.e. #1795's published figures to
their published precision, and *bit-identical against the pre-#2156
netlist*, which is the tightest available check that the two new sources
change nothing. `klt sim`'s transient deck for the same corner starts
from a resolved latch instead. Both are valid solutions of a degenerate
DC problem, and the difference is the solve path, not the circuit. A
start-up argument that depended on which root a solver picked would be
worthless; this one does not, because the transient starts and sustains
from either.

#### Deterministic worst-case offset sweep — 44/44 pass

`sim_request_startup_offset.json`: 11 `(voffhi, vofflo)` rows — 0,
±10 mV, ±25 mV, ±50 mV in both differential and common-mode
combinations — across the four thinnest-margin corners the issue named
(1.62 V/27 C at `tt`/`ss`/`ff`, plus `ss`/1.62 V/-40 C). ±50 mV is a
deliberately generous bracket — 25-40x the `.op` margin at the 27 C
corners — so this sweep *brackets* rather than samples; the Monte Carlo
below is the one that samples.

| quantity | range over all 44 cases | gate |
|---|---|---|
| `t_startup` (first mid-band transit of `vo`) | 153 - 1011 ns | < 3 us |
| `q_first_edge` (latch resolves out of its t=0 state) | 4.3 - 594 ns | < 2 us |
| `period_avg` (6 steady-state periods) | 1.5547 - 2.0344 us | 1.4 - 2.2 us |
| `period_drift_pct` (late 3 periods vs. early 3) | -0.33 / +0.37 % | ±2 % |
| `dv_eff` (effective threshold spacing) | 0.7526 - 0.9576 V | — |
| `vo` high / low | 1.1812 - 1.3088 / 0.2892 - 0.3958 V | ≤1.55 / ≥0.15 |

`dv_eff` tracking the injected differential offset one-for-one
(0.8531 V nominal, ±0.100 V at ±50 mV differential) is the check that
the injection is doing what it claims.

**Start-up latency is the thing that moves, and it moves a little.**
That is what the issue predicted. At `ss`/1.62 V/-40 C `t_startup` goes
from 702 ns with no offset to 153-841 ns across the offset rows; over
the whole sweep the worst case is 1011 ns, about half a period, against
a 1.5-2.0 us period. Nothing approaches a failure to start.

#### Monte Carlo over sky130 MOS mismatch — 128/128 pass

`sim_request_startup_mismatch.json`: the same four corners on their
`*_mm` mismatch-enabled `.lib` sections, 32 samples each,
`seed: 20260920`, with `Voffhi`/`Vofflo` left at 0 so the only
perturbation is the PDK's own per-instance device variation.

| corner | `t_startup` min/med/max (ns) | `period_avg` min/med/max (us) | `dv_eff` min/max (V) |
|---|---|---|---|
| `tt_mm`/27C | 399 / 510 / 1047 | 1.6986 / 1.7659 / 1.8907 | 0.7986 - 0.8949 |
| `ss_mm`/27C | 416 / 486 / 666 | 1.6918 / 1.7577 / 1.8702 | 0.8111 - 0.8867 |
| `ff_mm`/27C | 346 / 495 / 1116 | 1.6373 / 1.7416 / 1.8564 | 0.8087 - 0.9014 |
| `ss_mm`/-40C | 32 / 468 / 792 | 1.7163 / 1.8130 / 1.9666 | 0.8066 - 0.9039 |

All 128 samples reach sustained oscillation inside the 1.4-2.2 us band
(worst 1.6373 / 1.9666 us — clearing the fast limit by **16.9%** and
the slow one by **11.9%**, on the same `value/limit` convention the
Evidence table above uses), with `period_drift_pct` inside ±1.37% and
`q_first_edge` never later than 919 ns. `startup_margin_mv` ranges
-809.8 to +33.8 mV over the 128 samples: mismatch happily drives the
t=0 state to either side of, or well away from, the balance point, and
start-up is indifferent to which.

**What this sweep does *not* cover.** `klt sim`'s own per-family
mismatch-activity report (`monte_carlo.family_mismatch` in the response)
records `mosfet: active = true` and `resistor: active = null` for
sky130 — MOS per-instance mismatch is real here, but the composite
divider's `res_xhigh_po`/`res_generic_nd` sections are **not** being
varied, so divider-resistor mismatch remains unmodelled. That gap is
narrower than it sounds for *start-up* (the deterministic sweep already
walks the threshold pair ±50 mV, a far larger excursion than resistor
mismatch would produce), but it does mean the `dv_eff` spread reported
above is a MOS-only figure.

The one number worth carrying forward: **MOS mismatch alone moves
`dv_eff` by -6.4%/+6.0%** around nominal, against **+3.29%** for the
entire 18-corner PVT matrix. Mismatch, not PVT, is now the larger term
in the threshold spacing — see
[Handoff](#handoff-what-the-follow-on-should-attack).

#### Why these two requests measure edges on `vo`, not on `q`

Both start-up requests take their steady-state edges from
`WHEN v(vo)=0.8 RISE=n TD=10u` rather than from `v(q)` the way
`sim_request.json` does, and gate at 10 us rather than 6 us. Both
changes are forced by measurement robustness, and both were found the
hard way:

- **`q` can cross its 0.75 V level more than once on a single
  transition.** At `ss`/1.62 V/+10 mV/+10 mV/27 C, `sim_request.json`'s
  own `q`-based `RISE`-indexed measurements counted two crossings of one
  edge and reported a **-47.9%** period drift on a circuit that was
  oscillating perfectly. This is the same timestep-dependent
  crossing-count fragility the netlist header already documents for
  start-up edges; offset re-exposes it in steady state. `vo` is a 10 pF
  node ramping between two thresholds and has no such glitch.
  (Reproduce: that corner's deck with `TD=6u`, `q`-based edges →
  `drift_pct = -47.86`.)
- **Offset lengthens the start-up transient past the 6 us gate.** The
  same corner and the same `q`-based measurement settle to **-0.25%**
  drift at a 10 us gate, against -47.9% at 6 us.
- **`period_drift_pct` compares two 3-period averages** (`t_edge7` vs
  `t_edge4` vs `t_edge1`) rather than one period against an average,
  so that single-cycle jitter under `_mm` mismatch cannot turn into a
  false failure. The ±2% gate is set for that; the deterministic sweep
  lands inside ±0.37% and the 128 mismatch samples inside ±1.37%.

`q_edge1`/`q_edge4`/`q_period_avg` are still measured, ungated, so the
*output clock* (not just the timing node) is still shown to produce four
clean steady-state edges in every case.

### Where the PVT spread comes from

Before #1795, the 38% total period spread was almost entirely
**temperature**, and almost entirely the **reference resistor's
temperature coefficient**: `dv_eff` swung **-18.5%** over -40 -> 125 C,
because `sky130_fd_pr__res_xhigh_po`'s `rbody` carries
`tc1 = -1.47e-3`/C (`-4.3e-4`/C on the head segments), about -1.1e-3/C
blended over that geometry. Supply contributed +1.6% and process ~0.1%.

With the composite divider that term is essentially gone, and the
remaining spread is a different, much smaller set of things. Measured
per-axis, holding the other two fixed:

| axis | swing in `dv_eff` | swing in `period_avg` | note |
|---|---|---|---|
| Temperature (-40 -> 125 C, `tt`/1.62V) | **+0.77%** | **-3.23%** | was -18.5% in `dv_eff`. What is left in the period is *not* the divider — see the decomposition below |
| Supply (1.62 -> 1.98 V, `tt`/27C) | +1.68% | -0.37% | `XMref`'s own channel-length modulation; its `\|Vds\|` is `vdd - vth_hi`. Untouched by this change, and it largely cancels in the period because `iosc` rises with supply too |
| Process (`tt`/`ss`/`ff`, 1.62V/27C) | +0.09% | +1.03% (at -40C) | the divider is a current-into-resistor product and both resistor models are process-corner-invariant in `sky130.lib`; the period's process term is the comparator delay, not the reference |

Over the whole 18-corner matrix: `dv_eff` 0.8527-0.8808 V (+3.29%),
`vo_high - vo_low` +3.19%, `iosc` 10.01-10.41 uA (+4.04%), period +4.50%.

**What now dominates.** Decomposing the largest single axis,
`tt`/1.62V from -40 C to 125 C, through
`T = 2 * Cosc * (vo_high - vo_low) / iosc`:

| term | contribution to the -3.23% | |
|---|---|---|
| `iosc` (the `XMchg`/`XMdis` mirror current) | -2.52% | 10.008 -> 10.260 uA |
| comparator + latch delay (the overshoot term) | -0.78% | 40.9 -> 27.3 mV, shrinking faster than `dv_eff` grows |
| `dv_eff` (the compensated divider) | +0.77% | the +1.17% residual bow of the composite, diluted |

So the reference divider has gone from owning ~38% of the spread to
being the *smallest* of the three terms, and the new leader is the
current mirror's own temperature drift — a term the old design also had
but which was invisible underneath the resistor tempco.

The comparator + latch delay still shows up as overshoot past each
threshold, but it is much smaller and much flatter than before:
21.7-48.5 mV total (2.5-5.7% of `dV`) against 22.6-123 mV (2.9-13.6%)
on the all-poly divider. Most of that improvement is a second-order
benefit of the compensation rather than anything done to the comparator:
`vth_hi` no longer climbs to 1.30 V at -40 C, so `cmp_n`'s input
common-mode stays ~390 mV below a 1.62 V rail instead of ~317 mV, and
the 1.62V/-40C corners no longer starve. The sizing is still centred
against measured `vo_high - vo_low` rather than nominal
`vth_hi - vth_lo`, for the same reason as before.

### Expected solver diagnostics

16 of 18 corners emit `Warning: Dynamic gmin stepping failed` (and
`Warning: True gmin stepping failed`) during the DC operating-point
solve; source stepping then completes and the transient runs cleanly
everywhere. The two start-up sweeps emit the same warning on every
case (44/44 and 128/128) and likewise run clean — it is the degenerate
DC problem, not the `Voff` sources, and it predates #2156.

All 18 corners emit `Warning: sky130_fd_pr__diode_pw2nd_05v5: IKR too
small - model effect disabled!` — the high-injection knee-current
parameter of the parasitic p-well/n-diffusion junction diodes inside
`sky130_fd_pr__res_generic_nd`. Those diodes are reverse-biased by
construction here, so the disabled high-injection term is irrelevant to
this circuit; the warning is a property of the shipped model card, not of
this netlist.

`Warning: singular matrix: check node vth_lo`, which about half the
corners emitted before #1795, is **gone at all 18 corners**. That note
belonged to the `sky130_fd_pr__res_xhigh_po` model — it expresses its
body resistance as a bias-dependent formula containing `abs(v(t1,t2))`,
whose Jacobian is discontinuous at exactly the zero bias gmin/source
stepping starts from — and the composite divider now puts a plain,
bias-independent `res_generic_nd` section in series with each poly
section, which is enough to keep the node conditioned through the zero
crossing. Pleasant side effect, not a design goal.

## Reference generator: why not a bandgap

Scope item 3 of #1814 allowed "bandgap-derived per
`kb/entries/sky130-bandgap-reference.json`, or a documented simpler
stand-in, with the choice justified". This uses the simpler stand-in — a
mirrored current into a real resistor divider — for three reasons:

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
now set by real MOSFET + real resistor devices and carry real PVT
sensitivity (quantified above), where the spike used ideal DC sources.

### Why the divider is a composite, and why a PTAT/CTAT bias is not the fix

#1795 was originally scoped as a **PTAT/CTAT bias generator** making
`iosc` track `dV`'s temperature dependence. That would not have worked,
and the algebra says why. `Iref`/`Irefp` are ideal DC sources (inherited
verbatim from `../comparator-core/comparator_core.spice`), and `{ib}`
cancels out of the period rather than setting it — both the threshold
spacing and the charge current are mirrored from the same `{ib}`:

```
dV   = ib * r2
iosc = k * ib                 (k = XMchg/XMdis mirror ratio, ~1.95)
T    = 2 * Cosc * dV / iosc = 2 * Cosc * r2 / k
```

`{ib}` is absent from the final expression. Shaping it PTAT or CTAT moves
the period only through second-order terms; **`r2` alone survives the
cancellation**, so `r2`'s tempco is the only first-order lever. This
README's earlier Handoff section listed both options; measuring the
algebra settled it in favour of the passive one, which also needs no new
active circuitry (and therefore introduces no new feedback loop to
re-qualify).

Each leg is therefore a series composite of a negative-tempco poly
resistor and a positive-tempco n-diffusion resistor — the standard
zero-TC construction:

| device | rsheet | `tc1` | `tc2` |
|---|---|---|---|
| `sky130_fd_pr__res_xhigh_po` (`rbody`) | 2000 ohm/sq | -1.47e-3 /C | +2.7e-6 /C^2 |
| `sky130_fd_pr__res_generic_nd` | 120 ohm/sq | +1.422e-3 /C | +6.57e-7 /C^2 |

**Why `res_generic_nd` and not something with more sheet resistance.**
`res_iso_pw` is the obvious candidate — 3816 ohm/sq and +3.53e-3 /C would
have made the compensating sections ~20x shorter — but it is **not
reachable from this benchmark's model path**: `sky130A` only includes it
from `libs.tech/ngspice/all.spice`, never from the `tt`/`ss`/`ff` `.lib`
sections `sim_request.json` selects (verified by include-tree inspection
on `sky130A` @ `f6eeac7`, 2026-09-18). Of what *is* reachable,
`res_generic_pd` (197 ohm/sq, `tc1` +1.259e-3, `tc2` +2.204e-6) would be
~37% shorter for the same resistance but leaves a ~1.6% residual bow
instead of ~1.2%, and needs its n-well body tied to `vdd`;
`res_generic_nd`'s body is the p-well, i.e. the same ground the poly
device's `sub` terminal already uses. A three-way `po`+`nd`+`pd` blend
that would cancel the curvature exactly has no non-negative solution —
`nd` and `pd` are too similar in shape.

**The split ratio was measured, not assumed.** Forcing 5 uA through each
device standalone against the real `tt` models (ngspice-46, `sky130A`
@ `f6eeac7`) gives, normalised to 27 C:

| device | -40 C | 27 C | 125 C |
|---|---|---|---|
| `res_xhigh_po` | 1.11045 | 1.0 | 0.88185 |
| `res_generic_nd` | 0.90755 | 1.0 | 1.14846 |

Equalising the two endpoints of `p*po + (1-p)*nd` gives **p = 0.5131** —
poly carries 51.3% of each leg, n-diffusion 48.7% — for a +1.17%
residual bow (both endpoints high, 27 C low) against the 25.9% swing of
the all-poly divider. The shipped geometries land at p = 0.5129/0.5131:

| leg | poly | n-diffusion | total (27 C) | sets |
|---|---|---|---|---|
| `r1` | `XRdiv1`, W=1 L=18 -> 38.14k | `XRdiv1n`, w=1 l=308 -> 36.19k | 74.33k | `vth_lo` ~ 0.363 V |
| `r2` | `XRdiv2`, W=1 L=42.4 -> 89.83k | `XRdiv2n`, w=1 l=726 -> 85.30k | 175.13k | `dV` ~ 0.854 V |

Totals were re-centred at the same time: `dV` moves from 0.827 V to
0.854 V at 27 C so the now-nearly-flat period sits near the 1.4-2.2 us
band's geometric centre instead of at its old 27 C value, while `vth_lo`
is held at its old 27 C value so both comparators keep the common mode
they were validated at.

**The cost, stated plainly.** N-diffusion is ~17x less resistive per
square than xhigh poly, so the compensating sections are long — 308 um
and 726 um at W = 1 um, about 1030 um^2 of extra diffusion against the
60 um of poly they sit in series with. That is the price of this
construction, not an oversight. `res_generic_pd` would trade ~35% of it
against a worse residual bow. Raising `{ib}` while shrinking every
resistance *and* the `XMchg`/`XMdis` mirror ratio in proportion would
keep the period identical and shrink the diffusion by the same factor —
but it re-biases the comparator core at a point #1813 never signed off
on, so it was not taken here. Neither trade is closed; see
[Handoff](#handoff-what-the-follow-on-should-attack).

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
2. **Runtime.** Measured: the behavioral reference's whole
   `check_reference_solutions` gate for this task completes in **5.8 s**;
   this netlist's own 18-corner sweep takes **~5 minutes** on 4 workers.
   The reference-solution gate runs on every `validate` and every uncached
   `run`, so that is a ~50x cost increase on a hot path, for a task whose
   point is the `C*dV/I` timing relation rather than silicon accuracy.
3. ~~**Margin.**~~ **Retired by #1795.** This reason read: "this one
   holds the band with ~6% margin on each side because its period carries
   the poly resistor's real tempco". After compensation it holds the band
   with 23.5%/21.8%, which is not materially more fragile than the
   behavioral reference. This reason no longer supports the decision.
4. **Scope.** Swapping the scored reference also means replacing
   `eval_descriptor.json`'s gate and objective wiring and re-deriving the
   task's own target band. That is a benchmark-contract change, not the
   circuit-assembly increment #1814 scoped, nor the reference-generator
   increment #1795 scoped.

**The decision still stands, on reasons 1, 2 and 4.** #1795 explicitly
did not revisit it: `../oscillator.spice`, `eval_descriptor.json`,
`../sim_request.json`, `../models.lib` and
`../../../tasks/rc-relaxation-oscillator.json` are untouched by that
issue. It removed reason 3 and did nothing about reason 2 — the sweep is
still minutes, not seconds, because it is still 18 corners of a 20 us
transient over real device models. Anyone reopening this needs a runtime
answer *and* the PDK-dependency answer (reason 1); margin alone is no
longer the blocker it was.

## Handoff: what the follow-on should attack

The tempco item that headed this list — "compensating the reference
resistor's temperature coefficient, which owns essentially all of the 38%
spread" — **is done** (#1795, composite `po`+`nd` divider; see
[Where the PVT spread comes from](#where-the-pvt-spread-comes-from)). Of
the two options this section proposed, the composite divider was taken
and the PTAT/CTAT bias generator was ruled out on algebra rather than
deferred — `{ib}` cancels out of the period entirely, so shaping it
cannot be a first-order fix. What is left, in rough order of value:

1. **The `XMchg`/`XMdis` mirror current's own temperature drift**, now
   the largest single term: `iosc` moves +2.52% over -40 -> 125 C at
   `tt`/1.62V, more than either the comparator delay or the compensated
   divider. This is where #1795's PTAT/CTAT idea could still pay off, but
   pointed at `iosc` rather than at `{ib}` — the mirror ratio would have
   to acquire a deliberate temperature shape, not the bias current.
2. **Area of the compensating sections**: 1030 um^2 of n-diffusion for
   ~121 kohm. The two identified trades — `res_generic_pd` (~35% shorter,
   worse bow) and scaling `{ib}` up while scaling every resistance and
   the mirror ratio down (proportional shrink, but re-biases the
   comparator core) — are both still open. The second needs a
   comparator-core re-validation at the new bias, which is #1813's
   territory, not a divider change.
3. ~~**Offset, mismatch, and start-up under both.**~~ **Done (#2156).**
   This read: "No testbench in this chain models either... a Monte-Carlo
   start-up check is the natural next validation step." Both checks now
   exist and both are clean — 44/44 deterministic offset cases
   ([`sim_request_startup_offset.json`](sim_request_startup_offset.json))
   and 128/128 MOS-mismatch Monte Carlo samples
   ([`sim_request_startup_mismatch.json`](sim_request_startup_mismatch.json)),
   with the thin `.op` margin shown to be a feedback-loop residual rather
   than a headroom offset can consume. See
   [Start-up under comparator offset and mismatch](#start-up-under-comparator-offset-and-mismatch).
   Two things that sweep surfaced are *not* closed and belong on this
   list in its place:
   - **Divider-resistor mismatch is still unmodelled.** `klt sim`
     reports sky130's resistor-family mismatch activity as unverified
     (`resistor: active = null`), so the `*_mm` sections vary MOS
     devices only. Closing this needs either a verified resistor
     mismatch mechanism in the PDK path or a deterministic
     `r1`/`r2`-perturbation sweep analogous to the `Voff` one.
   - **MOS mismatch now dominates the threshold spacing.** It moves
     `dv_eff` by -6.4%/+6.0% against +3.29% for the whole 18-corner PVT
     matrix. Nothing about start-up cares, but anyone using this as a
     *frequency* reference should: the period's 1.6373-1.9666 us
     mismatch spread is **20.1%**, 4.5x the 4.50% PVT spread, and the
     obvious lever (a larger input pair in the comparator core) is
     #1813's territory, not this file's.
4. **The delay-induced overshoot on `dV`** (regenerative feedback in the
   comparator, per #1789's `1/sqrt(I)` finding) — now 2.5-5.7% of `dV`
   rather than up to 13.6%, so a much smaller prize than it was.
5. **`XMref`'s headroom** is no longer a near-term concern: compensation
   moved the matrix-worst `|Vds|` from 0.317 V to 0.389 V. Still the axis
   to watch if the thresholds are ever pushed further apart.
6. **Sweep runtime**, which is now the load-bearing half of the
   ship-alongside decision above.
