# Device-level sky130A switched UP/DOWN charge pump (unscored)

Issue #2743, part of #2736 and epic #1779. **Not scored, not selected by the
benchmark harness, not a replacement** for the behavioral reference
`../pll.spice`. Same ship-alongside precedent as
`../../relaxation-vco/device-vco/` (#2735/#2742). The behavioral/XSPICE PFD
and divider are out of scope; closed-loop assembly is #2744.

Files: `charge_pump_device.spice` (reusable `cp_dev` subcircuit + standalone
testbench), `sim_request_pump.json` (independent 18-corner request),
this README. The topology is derived from the read-only
`examples/kb/pfd-charge-pump-tri-state/charge_pump.spice` (1:1 PMOS source /
NMOS sink mirrors, `L=1 W=8`); its fixed `ctrl_up`/`ctrl_dn` clamps are gone
and each branch gets a series switch.

## Subcircuit contract (for #2744)

```
.subckt cp_dev up dn out vdd vss nbias pbias
```

| Pin | Contract |
|---|---|
| `up`, `dn` | **active-high** logic, `0` = `vss`, `1` = `vdd` (levels must track the actual supply). `up=dn=0` is high-Z. `up=dn=1` is allowed (PFD reset overlap): net current is the source/sink difference. |
| `out` | current output for a passive loop filter. **Positive pump current flows out of `out`** (UP sources, DOWN sinks). Validated for held voltages 0.3, 0.45, 0.6, 0.75, 0.9 V at all 18 corners (covers the device VCO's 0.3-0.9 V `vctrl` range, endpoints and interior). Not validated outside 0.3-0.9 V. |
| `vdd`, `vss` | explicit supply/ground; no global source inside the block. |
| `nbias` | gate rail of an **external** diode-connected NMOS (`W=8 L=1 nf=1`, source at `vss`) fed with `icp` from `vdd`. |
| `pbias` | gate rail of an **external** diode-connected PMOS (`W=8 L=1 nf=1`, source at `vdd`) sinking `icp` to `vss`. |
| bias units | `icp` in amps; pump current = `icp` x 1 (1:1 mirrors); nominal 20 uA. Other `icp` values are not characterized. |

Inside the block: one inverter makes `upb` from `up`; `XMUP` (PMOS mirror) ->
`XSUP` (PMOS switch, gate `upb`) -> `out`; `XMDN` (NMOS mirror) -> `XSDN`
(NMOS switch, gate `dn`) -> `out`. Switches are `L=1` (`XSUP W=8`, `XSDN
W=4`). Everything is real sky130 devices; there is no stimulus, clamp or
ideal source in the block.

**Loading without the testbench.** The reusable region is exactly
`.subckt cp_dev ... .ends cp_dev` in `charge_pump_device.spice`. The file
below the "Standalone testbench" banner (supply, bias references and bias
diodes, schedules, clamps, load cap, `.ic`) is stimulus; copy the subcircuit
region (plus the two bias diodes and `Iref` from the testbench for the bias
rails) rather than instantiating the testbench.

### Ideal elements (testbench only; none inside `cp_dev`)

| Element | Role |
|---|---|
| `Vdd` | DC supply, swept by `corners.supply_v.vdd` |
| `Iref`, `Irefp` (20 uA) | explicit bias references, mirrored by the real `XM1n`/`XM1p` diodes |
| `Vsu/Vsd/Vcu/Vcd` + `Bup/Bdn/Bcu/Bcd` | PWL 0/1 schedules; the B sources multiply them by `v(vdd)` so the logic-high level is the actual swept supply, not a fixed PWL level |
| `Vcl*` (0.3/0.45/0.6/0.75/0.9 V) | ideal DC clamps on five pump instances, for current measurement only |
| `Cc` (2 pF), `.ic v(oc)=0.6` | ideal capacitive load / initial condition for the transient case |

## Characterization

One transient (`500p 10.5u`) per corner containing six pump instances
(five clamped, one on `Cc`).

Clamped instances share one schedule (400 ns windows, averaged over the last
150 ns of each): UP only (0.5-1.0 us), DOWN only (1.5-2.0 us), both off
(2.5-3.0 us), UP and DOWN both on (3.5-4.0 us). Capacitive instance:
sequential events on 2 pF starting at 0.6 V, each measured 0.1 us before and
0.3 us after: UP 10 ns, DOWN 10 ns, UP 2 ns, DOWN 2 ns, UP+DOWN overlap 10 ns,
DOWN 30 ns, UP 30 ns, then a 1.9 us both-off hold (8.6-10.4 us).

Metric definitions, signs and limits. Limits were set from design intent
(20 uA nominal, +/-30 % absolute band, +/-15 % matching, a leakage budget
that keeps a loop-filter node within 2 mV over a 2 us hold on 2 pF) **before**
the final sweep, not fitted to it:

| Metric | Definition | Limit |
|---|---|---|
| `iup_<V>`, `idn_<V>` | magnitude of sourced / sunk current at held `out`=V (uA), absolute, not relative to each other. A near-zero pair cannot pass. | 14-26 uA each |
| `mm_<V>_pct` | `100*(iup-idn)/icp` (normalized to the nominal 20 uA, **not** to their average) | +/-15 % |
| `iup_span_pct`, `idn_span_pct` | compliance: `100*(max-min over 0.3-0.9 V)/I(0.6 V)` | <= 15 % |
| `ioff_<V>_na` | both-off current magnitude at held V (nA) | <= 2 nA |
| `iov_<V>` | net current with UP and DOWN both on (uA, positive = sourcing) | +/-3 uA |
| `q_<dir><w>_ratio` | delivered charge `C*dV` over ideal `icp*w`, signed so correct direction is positive | 10 ns: 0.7-1.15; 2 ns: 0.3-1.3; 30 ns: 0.85-1.1 |
| `tloss_<dir>_ns` | dead time from a 10 ns / 30 ns two-point fit: `10 ns - dv10/slope` (positive = lost pulse width) | <= 3 ns |
| `dv_ov10_abs_mv` | `|dV|` for a 10 ns UP+DOWN overlap on 2 pF | <= 30 mV (30 % of one 100 mV pulse) |
| `hold_drift_mv` | `|dV|` over the 1.9 us both-off hold on 2 pF | <= 2 mV |

Steady mismatch (`mm`, `iov`) is distinct from switching charge error (`q_*`,
`tloss`, `dv_ov10`). Minimum effective pulse width: a 2 ns pulse delivers
0.835-1.054 of its ideal charge at every corner and the fitted dead time is at
most 0.08 ns (negative values mean the pulse delivers slightly *more* than
ideal because the mirror node dumps stored charge), so effective pulse width
is <= 2 ns. The oversized-looking `q` bands for 2 ns are intentional.

Every measurement is a `.meas` in the request; a missing edge/sample is
`status: error` and fails the request (`klt sim` status `error`).

## Reproduce

```
klt sim benchmarks/design-agent/reference/charge-pump-pll/device-pll/sim_request_pump.json \
    --backend local-parallel --max-workers 6 --format json
```

`--backend local` runs the 18 corners serially (about 3-5 min each on the
host used, ~1.2 h total). `options.timeout_s` is 1800 and
`keep_artifacts` false (no raw files retained). `klt sim` prints an advisory
"implausible timeout" preflight warning for the timepoint count; it is a
coarse heuristic, and the slowest corner finished in 267 s. Do not put this in
default scoring CI. Verify scored assets are untouched with
`git diff --stat origin/main -- benchmarks/design-agent/tasks benchmarks/design-agent/reference/charge-pump-pll/{models.lib,pll.spice,sim_request.json,eval_descriptor.json}`
(empty).

Provenance (final run, 2026-10-05): `klt` 0.5.0, KLayout 0.30.12, ngspice 46,
`sky130A` open_pdks `c6d73a35f524070e85faff4a6a9eef49553ebc2b`
(`~/.volare`), `sky130.lib.spice` sha256 `17c208a6...`, netlist sha256
`fc8a3b7a4d0aef0ab2dc74868767089b08425c91d692837d9fdc3092af6979da`. Backend
`local-parallel`, 6 workers on a shared macOS workstation; wall time 12 min
16 s for 18 corners; per-corner `runtime_s` 207-267 s (load-dependent).

## Results: 18/18 corners pass

Per corner (current in uA for held V = 0.3 / 0.6 / 0.9; `mm` and `ioff` are
the worst over all five held voltages):

| Corner | iup | idn | max abs mm % | max ioff nA | max abs iov uA | q_up2 | q_dn2 | dv_ov mV | hold mV |
|---|---|---|---|---|---|---|---|---|---|
| tt/1.620V/-40C | 18.54/18.52/18.51 | 19.01/19.67/19.90 | 6.97 | 0.001 | 1.39 | 0.88 | 1.05 | 7.62 | 0.000 |
| tt/1.620V/27C | 18.90/18.90/18.88 | 19.06/19.73/19.97 | 5.45 | 0.001 | 1.09 | 0.90 | 1.05 | 5.77 | 0.000 |
| tt/1.620V/125C | 19.20/19.19/19.18 | 19.02/19.77/20.04 | 4.30 | 0.037 | 0.86 | 0.92 | 1.05 | 4.30 | 0.031 |
| tt/1.980V/-40C | 19.44/19.44/19.44 | 19.03/19.72/20.18 | 3.70 | 0.001 | 0.74 | 0.97 | 1.03 | 1.74 | 0.001 |
| tt/1.980V/27C | 19.54/19.54/19.54 | 19.10/19.78/20.19 | 3.26 | 0.001 | 0.65 | 0.98 | 1.02 | 1.17 | 0.001 |
| tt/1.980V/125C | 19.66/19.66/19.66 | 19.08/19.83/20.20 | 2.92 | 0.037 | 0.58 | 0.98 | 1.00 | 0.45 | 0.031 |
| ss/1.620V/-40C | 17.93/17.91/17.88 | 18.99/19.63/19.83 | 9.75 | 0.001 | 1.95 | 0.84 | 1.05 | 10.83 | 0.000 |
| ss/1.620V/27C | 18.56/18.54/18.52 | 19.03/19.69/19.90 | 6.89 | 0.001 | 1.38 | 0.87 | 1.05 | 7.72 | 0.000 |
| ss/1.620V/125C | 19.02/19.01/19.00 | 18.96/19.73/19.97 | 4.86 | 0.028 | 0.97 | 0.90 | 1.05 | 5.34 | 0.023 |
| ss/1.980V/-40C | 19.39/19.39/19.39 | 19.01/19.69/20.13 | 3.70 | 0.001 | 0.74 | 0.97 | 1.03 | 1.94 | 0.001 |
| ss/1.980V/27C | 19.49/19.49/19.48 | 19.07/19.74/20.14 | 3.26 | 0.001 | 0.65 | 0.97 | 1.02 | 1.37 | 0.001 |
| ss/1.980V/125C | 19.60/19.60/19.60 | 19.03/19.79/20.15 | 2.87 | 0.027 | 0.57 | 0.97 | 1.00 | 0.72 | 0.023 |
| ff/1.620V/-40C | 18.85/18.84/18.83 | 19.04/19.72/19.98 | 5.75 | 0.001 | 1.15 | 0.90 | 1.05 | 5.96 | 0.000 |
| ff/1.620V/27C | 19.09/19.09/19.08 | 19.10/19.77/20.05 | 4.84 | 0.001 | 0.97 | 0.92 | 1.05 | 4.77 | 0.000 |
| ff/1.620V/125C | 19.32/19.31/19.30 | 19.07/19.82/20.11 | 4.04 | 0.051 | 0.81 | 0.94 | 1.05 | 3.67 | 0.043 |
| ff/1.980V/-40C | 19.49/19.49/19.49 | 19.06/19.76/20.23 | 3.73 | 0.001 | 0.75 | 0.98 | 1.03 | 1.59 | 0.000 |
| ff/1.980V/27C | 19.59/19.59/19.59 | 19.13/19.82/20.24 | 3.27 | 0.001 | 0.65 | 0.98 | 1.02 | 1.02 | 0.000 |
| ff/1.980V/125C | 19.72/19.71/19.71 | 19.12/19.87/20.25 | 2.97 | 0.050 | 0.59 | 0.99 | 1.00 | 0.25 | 0.043 |

Worst-case margins (corner in parentheses):

| Metric | Worst value | Limit | Margin |
|---|---|---|---|
| `iup` (min over V) | 17.88 uA (ss/1.62 V/-40 C, 0.9 V) | >= 14 uA | 3.88 uA |
| `idn` (max over V) | 20.25 uA (ff/1.98 V/125 C, 0.9 V) | <= 26 uA | 5.75 uA |
| `mm` | -9.75 % (ss/1.62 V/-40 C, 0.9 V) | +/-15 % | 5.25 points |
| `iup_span_pct` / `idn_span_pct` | 0.28 % / 5.95 % (ss/1.62 V/-40 C; ff/1.98 V/-40 C) | <= 15 % | 9.05 points on DOWN |
| `ioff` | 0.051 nA (ff/1.62 V/125 C, 0.9 V) | <= 2 nA | 39x |
| `iov` | -1.95 uA (ss/1.62 V/-40 C, 0.9 V) | +/-3 uA | 1.05 uA |
| `q_up10_ratio` | 0.888 (ss/1.62 V/-40 C) | 0.7-1.15 | 0.188 |
| `q_up2_ratio` | 0.835 (ss/1.62 V/-40 C) | 0.3-1.3 | 0.535 |
| `q_up30_ratio` | 0.893 (ss/1.62 V/-40 C) | 0.85-1.1 | 0.043 |
| `dv_ov10_abs_mv` | 10.83 mV (ss/1.62 V/-40 C) | <= 30 mV | 19.2 mV |
| `tloss_up_ns` / `tloss_dn_ns` | 0.08 / -0.53 ns | <= 3 ns | 2.92 ns |
| `hold_drift_mv` | 0.043 mV (ff/1.62 V/125 C) | <= 2 mV | 46x |

Observations: sourcing is flat across 0.3-0.9 V (<0.3 % variation) while
sinking rises ~4-6 % with `out`, so UP < DOWN at the high end and the sign of
`mm` is negative at high V; the weakest corner is ss/1.62 V/-40 C where the
PMOS branch loses ~10 % (switch + mirror headroom). UP current is ~0.3-0.9 uA
lower at 1.62 V than 1.98 V at all temperatures. `q_up30` has the thinnest
margin (4 points, same corner).

### Negative checks

A single-corner (tt/1.98 V/27 C) variant of the request was run to confirm
the failure paths (not committed): ending the transient at 4.5 us, before the
capacitive events, made `vb_up10` and the other cap-load measurements
`status: error` and the request status `error`; tightening `mm_06_pct` to
+/-0.5 % and `ioff_09_na` to <= 1e-6 nA produced `fail` with the measured values
(-1.20 %, 7.7e-5 nA) reported.

## Design iteration (not hidden)

The first draft used minimum-length switches (`XSUP L=0.15 W=4`,
`XSDN L=0.15 W=2`). It failed 7 of 18 corners on off-state leakage, all in the
hot / ff corners: up to 300 nA (ff/1.98 V/125 C, `out`=0.3 V) against the 2 nA
limit, with the hold drift reaching 71 mV and 2 ns DOWN charge falling to 0.33
of ideal. Limits were not touched; the switches were lengthened to `L=1`
(variants `L=0.5` and `L=1` were compared at ff/1.98 V/125 C; `L=1` had the
lowest leakage and charge closest to ideal) and the full sweep rerun.

## Caveats

- Only DC held-output compliance points and one capacitive transient; no
  closed-loop PLL behavior, loop-filter settling, or noise (that is #2744).
- Transient only, no Monte Carlo mismatch; the bias diodes are matched
  identically to the branch mirrors so systematic, not random, mismatch is
  reported (real PFD/CP mismatch would add to the 10 % worst case).
- The pump is characterized at `icp` = 20 uA only, `out` 0.3-0.9 V only, and
  an ideal 100 ps logic edge; real PFD edges and the loop-filter series
  resistor are not modeled.
- The 2 ns pulse charge ratio is measured on a 2 pF load with ideal edges;
  it is a switching-charge indicator, not a jitter or spur prediction.
