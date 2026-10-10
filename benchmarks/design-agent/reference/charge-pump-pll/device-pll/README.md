# Device-level sky130A charge-pump PLL (unscored)

Issue #2744, part of #2736 and epic #1779. **Not scored, not selected by the
benchmark harness, not a replacement** for the behavioral reference
`../pll.spice`. It follows the ship-alongside precedent of
`../../relaxation-vco/device-vco/` (#2735) and this directory's own switched
charge pump (`README-pump.md`, #2743). The scored `charge-pump-pll` gate,
`../sim_request.json`, `../eval_descriptor.json` and `../models.lib` stay
PDK-free and unchanged.

Files: `pll_device.spice` (closed loop + testbench), `sim_request.json`
(independent 18-corner request), this README. `charge_pump_device.spice`,
`README-pump.md` and `sim_request_pump.json` are the delivered pump and are
not modified.

## What is real, what is behavioral

```
refclk --> [PFD] --UP/DN--> cp_dev --> loop filter --> vco_dev --> out
 (ideal)     ^  (XSPICE)    (sky130)   C2 || (R1+C1)   (sky130)     |
             +----------------- [/4] <-------------------------------+
                                (XSPICE)
```

| Element | Realization |
|---|---|
| VCO | `vco_dev`, real sky130 devices (delivered #2735 block, unchanged) |
| Charge pump | `cp_dev`, real sky130 devices (delivered #2743 block, unchanged) |
| Pump / VCO bias diodes | real sky130 diode-connected devices at the contracted sizes: pump `W=8 L=1 nf=1` NMOS/PMOS fed with `icp` = 20 uA; VCO NMOS `W=10 L=1`, PMOS `W=40 nf=2 L=1` fed with 5 uA |
| Loop filter | passive: `C2` (100 pF) in parallel with `R1` (30 kohm) + `C1` (1 nF), ideal R and C elements (see Design) |
| Tuning path `cp_dev.out -> vctrl -> vco_dev.vctrl` | **no behavioral element** (only `cp_dev`, `R1`, `C1`, `C2`, `vco_dev` touch it; guarded by `tests/test_device_pll_reference.py`) |
| Pump-current path `Icp* -> diodes -> cp_dev mirrors` | **no behavioral element** besides the ideal reference currents |

### Behavioral boundary (explicit)

Everything below is ideal or XSPICE, testbench-level or digital-side only:

| Element | Role |
|---|---|
| `apfd_up`, `apfd_dn`, `aand` (XSPICE `d_dff`/`d_and`, 0.2 ns delays) | tri-state PFD with AND-gate reset, same topology as `../pll.spice` |
| `adiv1`, `adiv2` (XSPICE toggle `d_dff`) | /4 feedback divider |
| `a_ref`, `a_vco`, `a_hi` (`adc_bridge`), `a_dup`/`a_ddn`/`a_ddiv` (`dac_bridge`, 1 ns edges, normalized 0/1) | analog/digital interfaces. VCO edges are sampled at 0.8 V (the VCO README's own measurement threshold) |
| `Bup`, `Bdn` | UP/DN drivers: `v(vdd) * dac * en`. They stand in for the PFD's output buffers and make the logic-high level the **actual swept supply**, as `cp_dev`'s contract requires (`corners.supply_v.vdd` alters the source `Vdd`, not the `{vdd}` parameter) |
| `Bvg` | `v(vout) * en`: the VCO-to-divider sample (input of `a_vco`), not a load on `vco_dev.out` |
| `Ven` | 0 during the operating point, 1 from 20 ns. Holds both pump switches open and the divider input at 0 while ngspice solves the OP, so the analog OP cannot chatter against the event-driven state |
| `Vref` | ideal 150 kHz reference clock, first edge at 0.1 us |
| `Ibn`/`Ibp` (5 uA), `Icpn`/`Icpp` (20 uA) | ideal bias reference currents into the real diodes |
| `Vdd` | supply, swept by `corners.supply_v.vdd` |
| `.ic` | startup control voltages, see below |

## Reuse of the delivered blocks

`vco_cmp_n`, `vco_cmp_p`, `vco_dev` (`../../relaxation-vco/device-vco/vco_device.spice`)
and `cp_dev` (`charge_pump_device.spice`) are **byte-identical copies** of the
delivered reusable regions, bracketed by `BEGIN/END verbatim copy` banners.
The delivered files cannot be `.include`d whole: each carries its own
standalone testbench with top-level `Vdd`/`Iref` sources that would collide
and add three VCOs and six pumps to the run. `tests/test_device_pll_reference.py`
fails if either delivered block, any `.param` the VCO block reads, or the pump
`icp` drifts from the copy here, so a contract change cannot go unnoticed.
Pin contracts are used as delivered:

- `vco_dev vctrl out vdd vss nbias pbias`: `vctrl` gate-only, validated
  0.3-0.9 V.
- `cp_dev up dn out vdd vss nbias pbias`: active-high UP/DN at the actual
  supply, positive current out of `out`, `out` validated 0.3-0.9 V, `icp`
  20 uA only.

Both blocks are validated over the same 0.3-0.9 V window, so the PLL's
headroom check is that `vctrl` stays inside it.

## Design

- `fref` = 150 kHz, N = 4: the VCO locks at 600 kHz, the behavioral task's
  own frequency plan. From the VCO README (f(0.6 V) 528-554 kHz,
  856-911 kHz/V) the lock point is ~0.62-0.70 V, mid-window at every corner.
- Loop filter, `C2 || (R1 + C1)`: `C1` = 1 nF, `R1` = 30 kohm, `C2` = 100 pF.
  With `Kvco` ~ 880 kHz/V, `icp` = 20 uA, N = 4:
  `wn = sqrt(icp * Kvco / (N * C1))` ~ 6.6e4 rad/s (~10.6 kHz),
  `zeta = R1 * C1 * wn / 2` ~ 1.0, zero 3.3e4 rad/s, third pole
  `(C1 + C2) / (R1 C1 C2)` ~ 3.7e5 rad/s, versus `w_ref` = 9.4e5 rad/s.
- Two identical loops in one transient share the reference and the bias
  rails: `Xpll_lo` starts at `vctrl` = 0.5 V (VCO ~25 % slow, acquisition
  is UP-dominated) and `Xpll_hi` at 0.8 V (~20 % fast, DOWN-dominated), so
  both pump directions do real acquisition work at every corner.
- Startup: `.ic v(vctrl_lo)=0.5 v(vctrl_hi)=0.8` (operating point solved with
  those nodes held, then released); the reference starts at 0.1 us; the loop
  runs through the VCO's own V-to-I settling (no wait).

## Measurement and limits

One transient (`5n 300u`) per corner with both loops. Reference edges fall at
`tref0 + k/fref`; the two late windows start half a reference period before
edge 28 (window A, 183.4 us) and edge 40 (window B, 263.4 us), each four
reference periods long, the same half-period-offset convention as
`../sim_request.json`. Per loop (`_lo`, `_hi`):

| Measurement | Definition | Limit |
|---|---|---|
| `t_first_div` | first divided-clock rising edge (divider and VCO alive) | <= 5 us |
| `up_acq` / `dn_acq` | UP / DOWN on-time (us) integrated over 0 .. window A | `_lo`: UP >= 4, DOWN <= 1; `_hi`: DOWN >= 3, UP <= 1 |
| `period_div_a/_b` | mean of 4 divided-clock periods from each window start | 6.6333-6.7 us (6.6667 us +/-0.5 %) |
| `phase_err_a/_b` | reference edge to the next divided edge, from each window start (positive = feedback lags) | +/-30 ns |
| `vctrl_avg_a/_b` | mean `vctrl` over each window (lock point) | 0.35-0.85 V (validated 0.3-0.9 V less 50 mV) |
| `vctrl_drift_mv` | window B mean minus window A mean | +/-5 mV |
| `vctrl_ripple_mv` | `vctrl` peak-to-peak in window B | <= 10 mV |
| `up_lock` / `dn_lock` | UP / DOWN on-time (ns) in window B (4 cycles) | 2-100 ns: both directions still pulse every window (no dead PFD/pump), and neither is stuck on |
| `vctrl_min` / `vctrl_max` | `vctrl` extrema over the **whole** run after release (0.1 us to the end), acquisition overshoot included | >= 0.3 V / <= 0.9 V (the VCO's and pump's validated range) |
| `vctrl_lock_match_mv` | window-B lock point, `_lo` minus `_hi` (both loops must find the same point) | +/-5 mV |

Every quantity is a `.meas`; a missing edge or sample is `status: error` and
fails the request, so a loop that stops toggling, a divider that loses edges,
or a run that ends early cannot pass.

**How the limits were set.** Measured behavior first, then margin: the
loop-filter choice, start points and windows came from exploratory
single-corner runs (8 corner points: tt at 1.8 V/27 C, 1.62 V/-40 C and
1.98 V/27 C, ss/1.62 V at -40/125 C, ff at 1.62/1.98 V/-40 C and
1.98 V/125 C; see "Design iteration"), where the loops locked within ~115 us
(phase error inside 20 ns), the lock point sat at 0.654-0.679 V, the worst
late phase error was 10.6 ns, peak acquisition overshoot reached 0.839 V and
the lowest dip was 0.485 V. Windows A/B start >= 70 us after the latest
observed lock; the phase band is ~3x the worst observed error; the period
band is +/-0.5 % (measured spread was +/-0.05 %); the acquisition on-time
floors are about half of the measured minima. The headroom limits are not
fitted at all: they are the delivered blocks' validated 0.3-0.9 V range.
The limits were then frozen and the 18-corner run below is the first full
run against them. The behavioral eval descriptor and its +/-500 ns /
6.467-6.867 us gate are not touched.

## Reproduce

```
klt sim benchmarks/design-agent/reference/charge-pump-pll/device-pll/sim_request.json \
    --backend local-parallel --max-workers 6 --format json
uv run pytest -q tests/test_device_pll_reference.py   # verbatim-copy / request guards, no simulation
git diff --stat origin/main -- benchmarks/design-agent/tasks \
    benchmarks/design-agent/reference/charge-pump-pll/{models.lib,pll.spice,sim_request.json,eval_descriptor.json}
```

The last command must print nothing (scored assets untouched).
`options.timeout_s` is 3600 per corner and `keep_artifacts` false. On a host
with `KLT_SIM_BACKEND=batch` the plain `klt sim` call submits to the batch
fleet; for the final run below that submission was refused
(`8 instance(s) already running + 1 requested exceeds
BATCH_MAX_CONCURRENT_INSTANCES=8`), so the run used `local-parallel`. Do not
put this in default scoring CI: it is ~10-18 CPU-minutes per corner.

Provenance (final run, 2026-10-10): `klt` 0.7.0 (`b65fd38b` plus this
change), KLayout 0.30.12, ngspice 46, `sky130A` open_pdks
`c6d73a35f524070e85faff4a6a9eef49553ebc2b` (`~/.volare`),
`sky130.lib.spice` sha256 `17c208a6...`, netlist sha256
`aafa293292d5992e19631883388b6caeb18bc0f75a735fe1c6cae32955d3afeb`. Backend
`local-parallel`, 6 workers on a shared macOS workstation (load average
~20-35 from other jobs); wall time 42 min for 18 corners; per-corner
`runtime_s` 570-1083 s (load-dependent).

## Results: 18/18 corners pass

Lock = window-B mean `vctrl` (lo / hi loop); period = range over both loops
and both windows; phase = min / max over both loops and both windows;
ripple = worse loop; min / max = whole-run `vctrl` extrema over both loops;
acq = `up_acq_lo` / `dn_acq_hi`; wrong-way = max of `dn_acq_lo`,
`up_acq_hi`; lock pulses = range of the four `up_lock`/`dn_lock` values.

| Corner | lock V | period us | phase ns | ripple mV | min / max V | acq us | wrong-way us | lock pulses ns | runtime s |
|---|---|---|---|---|---|---|---|---|---|
| tt/1.620V/-40C | 0.673 / 0.673 | 6.6642-6.6661 | 4.0 / 7.6 | 3.04 | 0.492 / 0.832 | 10.37 / 7.13 | 0.09 | 7.7-25.0 | 888 |
| tt/1.620V/27C | 0.655 / 0.655 | 6.6660-6.6670 | 1.6 / 5.8 | 0.16 | 0.486 / 0.807 | 9.13 / 8.16 | 0.11 | 5.6-8.2 | 783 |
| tt/1.620V/125C | 0.660 / 0.660 | 6.6668-6.6668 | 2.0 / 2.0 | 0.13 | 0.485 / 0.818 | 9.32 / 7.81 | 0.15 | 5.6-7.1 | 689 |
| tt/1.980V/-40C | 0.672 / 0.672 | 6.6640-6.6690 | -4.1 / 5.4 | 1.45 | 0.485 / 0.839 | 9.90 / 7.15 | 0.16 | 5.6-13.8 | 1001 |
| tt/1.980V/27C | 0.655 / 0.655 | 6.6667-6.6673 | -0.5 / 2.4 | 0.37 | 0.485 / 0.816 | 9.04 / 8.07 | 0.29 | 5.6-8.3 | 792 |
| tt/1.980V/125C | 0.666 / 0.666 | 6.6666-6.6669 | 0.5 / 2.3 | 0.28 | 0.485 / 0.833 | 9.48 / 7.44 | 0.19 | 5.6-7.8 | 785 |
| ss/1.620V/-40C | 0.679 / 0.679 | 6.6639-6.6676 | 1.3 / 10.3 | 1.78 | 0.496 / 0.839 | 11.09 / 6.77 | 0.07 | 7.0-19.1 | 1083 |
| ss/1.620V/27C | 0.662 / 0.662 | 6.6666-6.6667 | 2.3 / 2.6 | 0.18 | 0.500 / 0.814 | 9.66 / 7.73 | 0.04 | 5.6-8.7 | 954 |
| ss/1.620V/125C | 0.665 / 0.665 | 6.6667-6.6667 | 2.2 / 2.3 | 0.19 | 0.485 / 0.825 | 9.73 / 7.51 | 0.14 | 5.6-7.9 | 932 |
| ss/1.980V/-40C | 0.677 / 0.677 | 6.6661-6.6691 | -2.7 / 1.5 | 0.69 | 0.490 / 0.844 | 10.18 / 6.86 | 0.11 | 5.6-10.8 | 1073 |
| ss/1.980V/27C | 0.661 / 0.661 | 6.6643-6.6679 | -0.5 / 5.7 | 1.31 | 0.485 / 0.824 | 9.35 / 7.75 | 0.23 | 5.6-12.8 | 992 |
| ss/1.980V/125C | 0.669 / 0.669 | 6.6666-6.6669 | 0.8 / 2.4 | 0.22 | 0.485 / 0.838 | 9.74 / 7.27 | 0.23 | 5.6-8.2 | 905 |
| ff/1.620V/-40C | 0.666 / 0.666 | 6.6665-6.6669 | 2.0 / 2.9 | 0.22 | 0.486 / 0.825 | 9.84 / 7.47 | 0.11 | 5.7-6.6 | 726 |
| ff/1.620V/27C | 0.650 / 0.650 | 6.6653-6.6669 | 1.8 / 4.9 | 0.85 | 0.485 / 0.800 | 8.80 / 8.35 | 0.12 | 5.6-15.2 | 638 |
| ff/1.620V/125C | 0.654 / 0.654 | 6.6665-6.6668 | 2.0 / 2.5 | 0.15 | 0.485 / 0.810 | 8.95 / 8.12 | 0.17 | 5.6-7.9 | 570 |
| ff/1.980V/-40C | 0.668 / 0.668 | 6.6668-6.6669 | 1.8 / 2.3 | 0.42 | 0.485 / 0.832 | 9.62 / 7.37 | 0.14 | 5.6-9.4 | 661 |
| ff/1.980V/27C | 0.652 / 0.652 | 6.6666-6.6668 | 1.9 / 2.5 | 0.29 | 0.485 / 0.810 | 8.81 / 8.20 | 0.23 | 5.6-8.5 | 575 |
| ff/1.980V/125C | 0.663 / 0.663 | 6.6666-6.6669 | 0.7 / 2.5 | 0.28 | 0.487 / 0.827 | 9.25 / 7.58 | 0.14 | 5.6-8.0 | 576 |

Worst-case margins (corner in parentheses):

| Measurement | Worst value | Limit | Margin |
|---|---|---|---|
| `vctrl_max` | 0.844 V (ss/1.98 V/-40 C, `_lo`) | <= 0.9 V | 56 mV |
| `vctrl_min` | 0.485 V (ff/1.62 V/125 C, `_lo`) | >= 0.3 V | 185 mV |
| lock point | 0.679 V (ss/1.62 V/-40 C) | 0.35-0.85 V | 171 mV |
| `period_div` | 6.6639 us (ss/1.62 V/-40 C, `_hi`, window B) | 6.6333-6.7 us | 30.6 ns |
| `phase_err` | 10.3 ns (ss/1.62 V/-40 C, `_hi`, window B) | +/-30 ns | 19.7 ns |
| `vctrl_ripple` | 3.04 mV (tt/1.62 V/-40 C) | <= 10 mV | 7.0 mV |
| `vctrl_drift` | 0.42 mV (ss/1.98 V/-40 C) | +/-5 mV | 4.6 mV |
| `vctrl_lock_match` | 0.40 mV (ss/1.98 V/-40 C) | +/-5 mV | 4.6 mV |
| `up_acq_lo` | 8.80 us (ff/1.62 V/27 C) | >= 4 us | 4.8 us |
| `dn_acq_hi` | 6.77 us (ss/1.62 V/-40 C) | >= 3 us | 3.8 us |
| wrong-way acquisition | 0.29 us (tt/1.98 V/27 C, `dn_acq_lo`) | <= 1 us | 0.71 us |
| `up_lock`/`dn_lock` | 5.59 ns (several) / 25.0 ns (tt/1.62 V/-40 C) | 2-100 ns | 3.6 ns / 75 ns |
| `t_first_div` | 0.10 us (ss/1.62 V/27 C) | <= 5 us | 4.9 us |

Observations: the lock point tracks the VCO's own corner spread
(0.650-0.679 V), the same in both loops to within 0.4 mV, so the pump's
UP/DOWN mismatch (up to ~10 % at ss/1.62 V/-40 C per `README-pump.md`) only
shows up as a few-ns static phase offset (typically +2 ns, feedback lagging).
The DOWN on-time floor of 5.6 ns per 4 cycles is the PFD reset overlap
(~1.4 ns per cycle); the UP side adds the phase offset. The cold corners at
1.62 V (tt and ss, -40 C) are the noisiest: ripple 1.8-3.0 mV and phase
error up to 10 ns, with the VCO's own V-to-I loop at its slowest there.
Acquisition overshoot (the `_lo` loop, starting 25 % slow) is the binding
headroom term: 0.80-0.84 V against the 0.9 V validated edge.

### Negative checks

Two single-corner (tt/1.98 V/27 C) variants were run to confirm the failure
paths (not committed):

- **Missing edges**: the same request with the transient cut to 200 us (window
  B never happens) returned `status: error` (exit 4): `period_div_b`,
  `phase_err_b` and the window-A `t5_div` edge are `error`, and the
  window-B averages/pulse integrals `fail`.
- **Out-of-range lock / saturation**: `fref` raised to 250 kHz (the VCO must
  run at 1 MHz, outside what 0.3-0.9 V delivers) returned `status: fail`
  (exit 3): both loops still lock, but at `vctrl` = 1.106 V with excursions
  to 1.32 V, so `vctrl_avg`, `vctrl_max` and the period checks fail; the
  `_hi` loop's acquisition flips to UP and its direction checks fail too.
  The headroom checks are what reject a lock the delivered blocks never
  validated.

## Design iteration (not hidden)

- The behavioral reference's loop filter (`C2` = 0.5 nF, `C1` = 1 nF, `R1` =
  20 kohm) was tried first at tt/1.8 V/27 C. With `C2/C1` = 0.5 it is
  under-damped: the phase error was still swinging +/-140-180 ns and `vctrl`
  +/-9 mV with a ~100 us period at 290 us. Four variants were compared at the
  same corner (`C2`/`C1`/`R1` = 0.1n/1n/20k, 0.1n/1n/30k, 0.1n/2n/15k,
  0.05n/0.5n/30k). 0.1n/1n/30k (zeta ~ 1) locked fastest within the
  0.3-0.9 V window; 0.05n/0.5n/30k was faster still but overshot to 0.98 V.
- Start points 0.45/0.85 V gave dips/overshoots within 45 mV of the window
  edges; 0.5/0.8 V keep 50+ mV.
- The first ff/1.62 V/-40 C attempt failed before the transient ("too many
  analog/event-driven solution alternations" in the operating point): the
  VCO output sat near the divider's 0.8 V sampling threshold and the
  event-driven state fed back into the analog OP through the pump. Gating
  the UP/DN drivers and the divider's VCO sample with `Ven` (0 during the OP,
  1 from 20 ns) fixed it; no limit was changed.

## Caveats

- Transient only; no Monte Carlo mismatch, no noise or jitter analysis. The
  static phase offset and lock-point agreement are systematic, not random.
- The PFD, divider and UP/DN drivers are ideal XSPICE/B elements with 0.2 ns
  gate delays and 1 ns edges; a real sky130 standard-cell PFD has different
  reset width and edge rates, which move the dead-zone pulses and the
  overlap current (`cp_dev`'s `iov` <= 2 uA at the tested overlap). Its
  supply current and kickback are not modeled.
- One reference frequency (150 kHz), one divider ratio (/4), `icp` = 20 uA
  only (the only characterized pump current), two start points; lock range
  and cycle-slip behavior outside them are not characterized.
- The loop-filter R and C are ideal; sky130 MIM/poly realizations (and
  their leakage and voltage coefficient) are not modeled.
- The `.ic` start is an idealized initial condition, not a power-on reset.
