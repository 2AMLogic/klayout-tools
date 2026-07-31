# Spec-review reference: LDO regulator

**Reference date: 2026-07-31.** Ranges below are for a 130–180 nm-class open
PDK, PMOS-pass LDOs in the 1–100 mA class (see KB entry
`kb/entries/ldo-pmos-pass-error-amp.json`). Refresh from current literature
when web access allows.

## Canonical spec-line checklist

1. **Input voltage range** — min/max VIN.
2. **Output voltage** — nominal + accuracy (%, 3σ, over line/load/temp;
   state whether it includes the reference's error or is regulator-only).
3. **Maximum load current** — and the *minimum* load current at which the
   LDO is stable (0 µA is a claim, not a default).
4. **Dropout voltage** — mV at max load, at the stated corner.
5. **Quiescent current** — at no load AND at full load (pass-device gate
   drive and buffer currents differ enormously between the two).
6. **Load regulation** — mV (or %) for a stated DC load range.
7. **Line regulation** — mV/V over the stated VIN range.
8. **PSRR vs. frequency** — dB at DC/1 kHz, ~100 kHz, and ~1 MHz, at a
   stated load current (PSRR collapses at light load for many topologies).
9. **Load transient** — ΔVOUT (undershoot/overshoot) and recovery time for
   a stated load step (ΔI, edge time) with the stated output capacitor.
10. **Stability / compensation** — the output-capacitor row: either an
    explicit COUT range **with a numeric ESR window** (external-cap
    design), or an explicit "capacitor-free, stable for COUT ≤ X" row
    (capless design). Phase-margin target across the full load range.
11. **Current limit** — limit value + behavior (foldback/brickwall), and
    short-circuit survival statement.
12. **Startup** — soft-start time, inrush limit, monotonicity.
13. **Output noise** — µVrms over a stated band (10 Hz–100 kHz is the
    datasheet convention).
14. **Thermal** — max power dissipation at dropout × Imax, junction rise.
15. **Enable/shutdown current** (if the block has an enable).
16. **Corner binding per line** — see below.

## Typical published / achievable ranges (130–180 nm class)

| Parameter | Comfortable | Aggressive | Not credible without justification |
|---|---|---|---|
| Dropout @ 10–100 mA | 200–500 mV | 100–200 mV | <100 mV at full load with modest pass-device area |
| Quiescent current | 10–100 µA | 1–10 µA (full perf.) | sub-µA Iq *and* fast transient response together |
| PSRR @ DC–1 kHz | 50–60 dB | 60–80 dB | — |
| PSRR @ 1 MHz | 10–25 dB | 30–40 dB | >50 dB at 1 MHz without cascaded regulation |
| Load transient (capless, ~1 mA→max step) | 100–300 mV | tens of mV | <10 mV for a full-range step, capless |
| Output accuracy (3σ, all conditions) | ±2–3 % | ±1 % | sub-±0.5 % including reference error, untrimmed |

Anchors:

- **Classic architecture + Iq/transient trade-off**: G. A. Rincon-Mora and
  P. E. Allen, "A low-voltage, low quiescent current, low drop-out
  regulator," *IEEE JSSC*, vol. 33, no. 1, pp. 36–44, Jan. 1998. The
  foundational treatment of why low Iq and fast load response fight each
  other (slew at the pass gate); a spec pairing sub-µA Iq with tens-of-mV
  full-step transient response contradicts it and needs an adaptive-bias
  story.
- **Capless (on-chip-cap-only) feasibility**: R. J. Milliken, J.
  Silva-Martínez, E. Sánchez-Sinencio, "Full on-chip CMOS low-dropout
  voltage regulator," *IEEE TCAS-I*, vol. 54, no. 9, pp. 1879–1890, Sep.
  2007. Establishes the capacitor-free 50 mA class and its compensation
  cost; use it to sanity-check any capless claim's transient row.
- **ESR-window stability doctrine**: G. A. Rincon-Mora, *Analog IC Design
  with Low-Dropout Regulators*, McGraw-Hill (2nd ed. 2014) — cited by
  reference for the standard result that an external-cap PMOS LDO relies on
  the ESR zero and is stable only inside a *bounded* ESR window (order
  tens of mΩ to a few Ω, design-specific). A stability row that names a
  capacitor but no ESR window is incomplete by construction.

## Known spec-writing pitfalls

- **Stability row without a numeric ESR window** (external-cap designs).
  "Stable with 1 µF" is unverifiable; the window (e.g. "COUT = 1 µF ±20 %,
  ESR 30 mΩ–1.5 Ω") is the actual spec. Capless designs must instead bound
  the *maximum* parasitic COUT they tolerate.
- **Iq quoted only at no load** — full-load Iq (buffer + gate-drive) is
  routinely 5–20× higher and is what the power budget sees.
- **PSRR without frequency and load current.** Single-number PSRR always
  reports the easy point.
- **Load-transient row missing step size, edge rate, or COUT** — the same
  circuit's ΔV varies by an order of magnitude across plausible readings.
- **No minimum-load row.** Many pass stages are conditionally stable at
  zero load; if the block can genuinely see 0 µA, the spec must say so and
  the design must own it (bleeder current counted in Iq).
- **No current-limit row.** An LDO spec without a current limit / short
  behavior line has not been through a failure-mode pass.
- **Dropout at the wrong corner.** Dropout binds at **SS/cold** (worst
  PMOS on-resistance at max load) and low VIN; Iq binds at **FF/hot**;
  stability margin typically binds at light load, **FF/cold**. Every row
  should name its corner.
- **Accuracy row that silently excludes the reference.** State whether the
  bandgap's error is inside or outside the LDO's accuracy number —
  double-counting (or omitting) it corrupts the system error budget.
