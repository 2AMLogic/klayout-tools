# Draft spec: `demo-bgr` — 0.6 V bandgap reference (sky130)

> **Synthetic example.** This block, its numbers, its decision records, and
> its "measurements" are all invented to exercise the `spec-review` skill.
> It is not the spec of any real project.

Fictional always-on 0.6 V voltage reference for a housekeeping domain,
sky130, current-mode (Banba-style) core using `sky130_fd_pr__pnp_05v5`
parasitic PNPs.

## Draft spec table (v0.2, unratified)

| # | Parameter | Target | Conditions |
|----|-------------------------|--------------------------|--------------------|
| S1 | Output voltage | 0.600 V ±0.5 % (untrimmed, 3σ) | TT, 25 °C |
| S2 | Temperature coefficient | ≤ 15 ppm/°C (box) | 0–85 °C |
| S3 | Supply range | 1.62–1.98 V | all corners |
| S4 | PSRR | ≥ 60 dB | — |
| S5 | Quiescent current | ≤ 5 µA | TT, 25 °C |
| S6 | Line regulation | ≤ 1 mV/V | DC, 1.62–1.98 V |
| S7 | Area | ≤ 0.02 mm² | — |

## Decision records

- **DR-0001 — topology.** Current-mode (Banba-style) core chosen so the
  0.6 V output is settable independent of the ~1.2 V silicon bandgap
  voltage, leaving headroom margin at VDD(min) = 1.62 V. Diode area ratio
  1:8.
- **DR-0002 — no production trim.** Test cost budget for this block is
  zero trim seconds; the accuracy target must therefore be met untrimmed.

## Device-characterization evidence (from this repo's devchar runs)

- Mismatch characterization of the error-amplifier input pair candidate
  (sky130 1.8 V nfet, W/L = 4 µm/2 µm per finger): measured Pelgrom
  A_VT ≈ 3.5 mV·µm ⇒ input-referred offset σ(Vos) ≈ 1.2 mV at the chosen
  device area.
- PNP ΔVBE spread at 1:8 area ratio, TT, 25 °C: σ ≈ 0.4 mV across the
  characterization array.
- No flicker-noise or resistor-matching characterization data available
  yet.
