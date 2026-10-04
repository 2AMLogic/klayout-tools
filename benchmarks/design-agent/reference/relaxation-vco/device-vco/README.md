# Device-level sky130A relaxation VCO (unscored)

Issue #2735, part of epic #1779. **Not scored, not selected by the benchmark
harness, not a replacement** for the behavioral reference `../vco.spice`.
It follows the ship-alongside precedent of
`../../rc-relaxation-oscillator/device-oscillator/` (#1822/#2155): the scored
`relaxation-vco` gate stays PDK-free and fast; this directory is for circuit
development and is the prerequisite for the PLL follow-on (#2736).

Files: `vco_device.spice` (circuit body + reusable `vco_dev` subcircuit +
standalone testbench), `sim_request.json` (independent 18-corner request).

## Design

`vco_dev` reuses the device oscillator's dual comparator (`vco_cmp_n` /
`vco_cmp_p`), NOR SR latch and complementary charge/discharge switches, and
adds a sky130 voltage-to-current converter:

- `ictrl = vctrl / Rv` (`Rv` ~ 61 kohm zero-TC composite of
  `res_xhigh_po` + `res_generic_nd`): a 5T lvt-PMOS OTA plus a second stage
  forces `v(vs) = v(vctrl)`; the PMOS mirror copies `ictrl` 1:1 into the
  charge path, and via an NMOS diode mirror into the discharge path.
- `f ~ vctrl / (2 * Cosc * dV * Rv)` with `Cosc` = 10 pF.
- **Threshold spacing is independent of `vctrl`.** In the device oscillator
  the thresholds and the charge current both derive from one bias `ib`, so `ib`
  cancels out of the period. Reusing that sharing for the control current
  would let the control also move `dV` and cancel the tuning. Here `vth_lo` /
  `vth_hi` come from the fixed `pbias` rail through the same divider, and only
  the charge/discharge current follows `vctrl`.
- `vctrl` is a MOS gate only (no DC load), so a PLL loop filter can drive it
  directly.

### Ideal elements (testbench only; none inside `vco_dev`)

| Element | Role |
|---|---|
| `Vdd` | supply, swept by `corners.supply_v.vdd` |
| `Iref`, `Irefp` (5 uA) | explicit bias references, mirrored by real `XM5b`/`XM5c` diodes into `nbias`/`pbias` (same convention as the device oscillator) |
| `Vctrl_lo/_mid/_hi` | DC control voltages 0.3 / 0.6 / 0.9 V, one per VCO instance |
| `Cl_*` | 20 fF output load per instance |

Everything else (comparators, latch, switches, V-to-I converter, mirrors,
threshold divider, `Rv`, `Ccap`) is real sky130 devices. `Ccap` is a plain
capacitor and `Rleak` (1 Gohm) is a DC-path helper across it.

## Subcircuit contract (for the PLL child)

```
.subckt vco_dev vctrl out vdd vss nbias pbias
```

| Pin | Contract |
|---|---|
| `vctrl` | control input, gate only (no DC current). Validated 0.3-0.9 V (all 18 corners, plus 0.6 V midpoint). Below ~0.2 V the PMOS input pair and above 0.9 V at vdd = 1.62 V (tail headroom) are **not validated**. |
| `out` | buffered, non-inverting clock, rail-to-rail; rising edge when the timing node reaches `vth_hi`. Characterized with a 20 fF load. |
| `vdd`, `vss` | explicit supply/ground; no global source inside the block. |
| `nbias`, `pbias` | gate rails of 5 uA-referenced diode-connected mirrors (NMOS `W=10 L=1` / PMOS `W=40 nf=2 L=1`); the block expects these from outside, see the testbench. |

Start-up: no kick source. The block starts from a resolved latch; the first
output edge (after 0.2 us) appears within 3.75 us at every corner. The V-to-I
loop needs up to ~10 us to settle at ss/1.62 V/-40 C, so frequency is
measured in settled windows from 14 us.

## Measurement and limits

One transient (`5n 50u`) with three `vco_dev` instances at 0.3 / 0.6 / 0.9 V.
Per instance: `t_start` (first rising edge of `out` through 0.8 V after
0.2 us), period from edges 1 and 3 after 14 us (`t1`, `t3`) and again after
36 us (`tl1`, `tl3`), `freq = 2/(t3-t1)`, and `drift_pct` between the two
windows. A missing measurement is `status: error` and fails the request, so a
non-oscillating instance cannot pass (no edges means no `t_start`/`freq`).
`vs_err` (V-to-I regulation error) is an extra health check.

Variant-specific limits (chosen from design intent before the final run, not
fitted to the results; the frequency/sensitivity limits equal the behavioral
task's bands, i.e. **not relaxed**):

| Measurement | Limit |
|---|---|
| `t_start_*` | <= 5 us |
| `freq_lo` / `freq_mid` / `freq_hi` | 230-380 kHz / 450-650 kHz / 700-1150 kHz |
| `vco_sensitivity` (0.3 to 0.9 V) | 800-1250 kHz/V |
| `vco_sens_lo_mid`, `vco_sens_mid_hi` | 600-1000 / 800-1300 kHz/V |
| `ratio_mid_lo` / `ratio_hi_mid` | >= 1.5 / >= 1.2 (monotonic by margin) |
| `drift_*_pct` | +/-2 % |
| `vs_err_*_mv` | +/-25 mV |

## Reproduce

```
uv run python scripts/design_agent_benchmark.py validate   # scored assets unchanged (slow: runs every reference solution)
klt sim benchmarks/design-agent/reference/relaxation-vco/device-vco/sim_request.json
```

On the dispatch host `KLT_SIM_BACKEND=batch`, so the 18 corners go to the
Spot batch fleet; locally a single corner can be run by narrowing
`corners` and passing `--backend local` (~1 min).

Provenance (final run): `klt` 0.6.0+ge2ba44fa31ce, ngspice 46, `sky130A` open_pdks
`c6d73a35f524070e85faff4a6a9eef49553ebc2b`, `sky130.lib.spice` sha256
`17c208a6...`, netlist sha256 as recorded in the response. Backend
`aws-batch-fleet` (job `klt-sim-dc3043b78f27`, spot c7i.4xlarge). Wall
time 37 min for 18 corners; per-corner `runtime_s` 347-1237 s on the shared
fleet host (load-dependent; a quiet single-corner local run takes ~1 min for
a 36 us transient). Do not put this in default scoring CI.

## Results: 18/18 corners pass

| Corner | f(0.3 V) kHz | f(0.6 V) kHz | f(0.9 V) kHz | Hz/V (k) | max t_start us | max drift % | max vs_err mV |
|---|---|---|---|---|---|---|---|
| tt/1.620V/-40C | 273.9 | 534.9 | 802.2 | 881 | 3.69 | 0.60 | 9.4 |
| tt/1.620V/27C | 281.4 | 549.5 | 819.5 | 897 | 3.59 | 0.35 | 4.7 |
| tt/1.620V/125C | 279.6 | 546.7 | 812.1 | 887 | 3.55 | 0.31 | 6.5 |
| tt/1.980V/-40C | 272.6 | 535.2 | 796.1 | 873 | 3.75 | 0.60 | 6.3 |
| tt/1.980V/27C | 280.2 | 548.6 | 814.8 | 891 | 3.56 | 0.45 | 4.7 |
| tt/1.980V/125C | 278.3 | 540.8 | 797.4 | 865 | 3.58 | 0.13 | 5.8 |
| ss/1.620V/-40C | 269.6 | 528.5 | 785.4 | 860 | 0.96 | 0.56 | 6.5 |
| ss/1.620V/27C | 279.1 | 546.5 | 817.5 | 897 | 3.60 | 0.44 | 5.2 |
| ss/1.620V/125C | 277.4 | 543.7 | 808.5 | 885 | 3.58 | 0.36 | 6.0 |
| ss/1.980V/-40C | 271.3 | 533.2 | 791.3 | 867 | 3.68 | 0.58 | 6.7 |
| ss/1.980V/27C | 278.9 | 546.3 | 803.5 | 874 | 1.93 | 1.00 | 4.7 |
| ss/1.980V/125C | 277.3 | 538.1 | 795.3 | 863 | 3.60 | 0.18 | 5.6 |
| ff/1.620V/-40C | 273.2 | 539.8 | 805.6 | 887 | 3.21 | 0.35 | 7.1 |
| ff/1.620V/27C | 283.1 | 554.2 | 827.5 | 907 | 3.54 | 0.39 | 4.8 |
| ff/1.620V/125C | 282.7 | 548.8 | 816.0 | 889 | 3.52 | 0.40 | 7.5 |
| ff/1.980V/-40C | 274.1 | 540.4 | 801.6 | 879 | 3.61 | 0.77 | 7.0 |
| ff/1.980V/27C | 282.2 | 549.9 | 816.8 | 891 | 3.50 | 0.09 | 4.6 |
| ff/1.980V/125C | 280.0 | 543.5 | 805.8 | 876 | 3.57 | 0.23 | 6.0 |

Worst-case margins against the limits (corner in parentheses):

| Measurement | Worst value | Limit | Margin |
|---|---|---|---|
| `freq_lo` | 269.6 kHz (ss/1.62 V/-40 C) | >= 230 kHz | 39.6 kHz |
| `freq_hi` | 785.4 kHz (ss/1.62 V/-40 C) | >= 700 kHz | 85.4 kHz |
| `vco_sensitivity` | 859.7 kHz/V (ss/1.62 V/-40 C) | >= 800 kHz/V | 59.7 kHz/V |
| `t_start` | 3.75 us (tt/1.98 V/-40 C) | <= 5 us | 1.25 us |
| `drift` | 0.996 % (ss/1.98 V/27 C) | +/-2 % | 1.0 % |
| `vs_err` | 9.4 mV (tt/1.62 V/-40 C) | +/-25 mV | 15.6 mV |

Frequency rises with control voltage at every corner (min `ratio_mid_lo`
1.94, min `ratio_hi_mid` 1.47). Sensitivity spans 856-911 kHz/V across
corners, and both halves of the range (`vco_sens_lo_mid` 863-904,
`vco_sens_mid_hi` 856-911 kHz/V) agree, so the response is near-linear.

### Deviations from the behavioral task bands

None in the pass/fail sense: every corner is inside the behavioral task's
frequency and sensitivity bands. The behavioral reference sits mid-band by construction; this device
variant's tightest margins are 12% (`freq_hi`) and 7% (`vco_sensitivity`)
over the low limits, both at ss/1.62 V/-40 C.

## Caveats

- Only DC control points are measured (0.3/0.6/0.9 V); dynamic tuning
  (settling after a `vctrl` step) is not characterized: that is #2736's job.
- The first-edge time at some corners (e.g. ss/1.62 V/-40 C, 0.4-0.9 us) is
  an early oscillation at the not-yet-settled V-to-I current; the period is
  only judged in the settled windows.
- Transient only, no Monte Carlo mismatch, as with the device oscillator.
- Two attempts failed before the final run and are not hidden: with 900 s
  per-corner timeouts and a 60 us transient, 16/18 corners timed out on the
  loaded fleet; the request was shortened to 50 us and `timeout_s` raised
  to 2400. A first 36 us draft with 8 us/24 us windows failed 3 corners
  (ss/1.62 V at -40 C and 27 C, and ff/1.62 V/-40 C) because the V-to-I
  loop had not settled; the windows were moved later instead of loosening
  limits.
