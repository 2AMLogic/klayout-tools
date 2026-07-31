# Spec-review reference: temperature sensor + POR

**Reference date: 2026-07-31.** Ranges below are for a 130–180 nm-class open
PDK. Two block classes share this file because they ship together in most
housekeeping/always-on domains; review each against its own checklist.

---

## Temperature sensor

### Canonical spec-line checklist

1. **Temperature range** — e.g. −40 to +125 °C (industrial) — every other
   row is meaningless without it.
2. **Inaccuracy** — ±°C at a stated σ level (3σ preferred), split by trim
   status (untrimmed / 1-pt / 2-pt trim), over the full range.
3. **Trim strategy** — points, at what temperature(s), correcting what.
4. **Resolution** — °C (rms) at a stated conversion time.
5. **Conversion rate / time** — and energy per conversion (the resolution
   FoM below needs both).
6. **Supply sensitivity** — °C/V over the stated supply range.
7. **Supply range and current** — active + standby.
8. **Interface** — analog / duty-cycle / digital word, and the reference it
   is ratioed to (a digital output implies a reference row).
9. **Self-heating** — stated bound (°C) at the operating duty cycle.
10. **Area** and **corner binding per line**.

### Typical published / achievable ranges (130–180 nm class, BJT-based)

| Parameter | Comfortable | Aggressive | Not credible without justification |
|---|---|---|---|
| Untrimmed inaccuracy (3σ) | ±1.5–3 °C | ±0.5–1 °C | ≲±0.25 °C untrimmed |
| 1-pt-trim inaccuracy (3σ) | ±0.5–1 °C | ±0.1–0.2 °C | ≲±0.05 °C, 1-pt trim |
| Resolution | 0.1–0.5 °C | 0.01–0.05 °C class | — |
| Supply current (always-on sensing) | 1–100 µA | sub-µA duty-cycled | — |

Anchors:

- **Best-practice accuracy**: M. A. P. Pertijs, K. A. A. Makinwa, J. H.
  Huijsing, "A CMOS smart temperature sensor with a 3σ inaccuracy of
  ±0.1 °C from −55 °C to 125 °C," *IEEE JSSC*, vol. 40, no. 12, pp.
  2805–2815, Dec. 2005. That result required precision techniques (DEM,
  chopping, curvature correction) *and* trimming — it is the ceiling, not
  the norm.
- **Field survey (the calibration for "typical")**: K. A. A. Makinwa,
  "Smart Temperature Sensor Performance Survey," TU Delft, continuously
  updated, publicly available:
  <https://ei.ewi.tudelft.nl/docs/TSensor_survey.xls>. Use it (via
  WebSearch when available) to place any accuracy/energy claim among
  published sensors; the resolution FoM (energy/conversion × resolution²)
  comes from the survey's accompanying literature.
- Untrimmed BJT-sensor error is dominated by the same offset/mismatch
  budget as the bandgap (this is a bandgap front-end reading ΔVBE) —
  cross-check against the repo's measured Pelgrom data exactly as in
  `bandgap-voltage-reference.md`.

### Pitfalls

- Accuracy row without trim status, σ level, and range (same trap as the
  bandgap's accuracy row).
- Resolution conflated with accuracy — 0.01 °C resolution with ±2 °C
  accuracy is normal and must read that way.
- No self-heating row on an always-on sensor.
- Corner binding: accuracy binds at the **temperature extremes**; supply
  current at **FF/hot**; conversion rate (if oscillator-timed) at
  **SS/cold**.

---

## Power-on reset (POR) / brownout detector

### Canonical spec-line checklist

A POR spec without numbers on every line below is not ratifiable:

1. **Release threshold** (VPOR↑) — the VDD at which reset deasserts,
   min/typ/max across corners.
2. **Assert threshold** (VPOR↓) — the VDD at which reset re-asserts.
3. **Hysteresis** — VPOR↑ − VPOR↓, as an explicit numeric row (it is the
   anti-chatter guarantee, not an implementation detail).
4. **Reset delay / pulse width** — time from threshold crossing to release,
   and minimum guaranteed reset pulse.
5. **Supply ramp-rate envelope** — the slowest AND fastest VDD ramps for
   which correct reset generation is guaranteed (µs-to-ms fast ramps and
   the pathological 1 V/s slow ramp both break naive PORs).
6. **Brownout envelope** — minimum detectable dip depth/duration, or an
   explicit "no brownout detection" row.
7. **Quiescent current** — always-on block; state it at FF/hot.
8. **Relationship to the digital domain** — which rail is monitored, what
   the reset output's valid range is while VDD is below threshold
   (glitch-free-from-0V requirement).
9. **Threshold variation across corners** — the min/max columns of rows
   1–2 ARE this line; require them, not a single typical.
10. **Corner binding per line.**

### Typical achievable ranges (130–180 nm class)

- Threshold accuracy: untrimmed comparator+reference PORs hold thresholds
  to roughly **±5–10 %** across PVT; sub-±3 % needs a trimmed reference.
  RC/leakage-based "delay POR" thresholds vary far more (2× and worse
  across corners) and must not carry tight threshold rows.
- Hysteresis: 20–100 mV class is typical and cheap; a 0 mV row is a defect.
- Quiescent current: 10 nA–1 µA depending on architecture; nA-class is
  achievable with subthreshold design but then reaction to fast dips slows
  — the spec must bind dip-response and Iq at the same time.

Anchors:

- **Open-silicon reference implementation**: the Caravel harness POR
  (`simple_por`) in the efabless/ChipIgnite Caravel management SoC
  (Apache-2.0, sky130): <https://github.com/efabless/caravel>. An openly
  inspectable sky130 POR with threshold + delay structure; useful as a
  concrete, license-clean baseline for what a minimal sky130 POR does and
  does not guarantee.
- **Threshold-accuracy reasoning**: a POR threshold is (reference) ×
  (comparator trip ratio); its corner spread is bounded below by the
  untrimmed reference spread — reuse the bandgap reference table above.
  For MOS-VTH-referenced thresholds, VTH corner spread in 130–180 nm PDKs
  is order ±15 % (see the PDK's own corner models — open PDK data, e.g.
  sky130 `tt`/`ss`/`ff` model decks), which is why VTH-referenced PORs
  cannot carry tight threshold rows.

### Pitfalls

- **Thresholds/hysteresis as prose** ("resets reliably at power-up") — the
  four numbers (VPOR↑, VPOR↓, hysteresis, delay) are the spec; without
  them there is nothing to verify at S8/S9.
- **No slow-ramp row.** The classic field failure: VDD ramps slower than
  the POR's RC assumptions and reset releases early. The ramp-rate
  envelope row exists to force this analysis.
- **Brownout silently out of scope** — say "no brownout detection" if so;
  downstream digital assumes otherwise.
- **Threshold row with a single typical value** — the min across corners
  must still exceed the digital domain's minimum operating VDD, and the
  max must still be reachable under the worst-case supply; both columns
  are load-bearing.
- Corner binding: release threshold max binds at **SS/cold**; Iq at
  **FF/hot**; delay (if RC-based) spreads at both extremes — name each.
