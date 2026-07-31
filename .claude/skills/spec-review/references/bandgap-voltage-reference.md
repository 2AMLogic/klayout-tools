# Spec-review reference: bandgap / voltage reference

**Reference date: 2026-07-31.** Ranges below are for a 130–180 nm-class open
PDK (sky130, gf180mcu) using parasitic vertical PNPs (e.g.
`sky130_fd_pr__pnp_05v5`) or subthreshold-MOS references. Refresh anchors
from current literature when web access allows; otherwise use as-is and note
the date in the review header.

## Canonical spec-line checklist

A ratifiable bandgap/reference spec has a row for each of these. Absence of
any row is a completeness finding.

1. **Output voltage** — nominal value + tolerance, split into *untrimmed*
   and *trimmed* rows, each with a σ level (3σ preferred) and sample basis.
2. **Trim strategy** — trim points (0/1/2-point), range, resolution (bits),
   and what the trim corrects (magnitude only vs. TC).
3. **Temperature coefficient** — ppm/°C, *box method*, over an explicit
   temperature range (e.g. −40 to +125 °C).
4. **Supply range** — min/max VDD, including the minimum headroom at the
   slow/cold corner where VBE is largest.
5. **Line regulation / supply sensitivity** — mV/V or %/V, DC.
6. **PSRR vs. frequency** — dB at stated frequencies (at least DC/100 Hz
   and one high-frequency point, e.g. 1 MHz), at stated load.
7. **Quiescent current** — µA, at stated VDD and temperature.
8. **Output noise** — integrated µVrms over a stated band (0.1–10 Hz
   flicker band is the reference-noise convention) and/or spot noise.
9. **Load capability** — max DC load and load regulation, or an explicit
   "unbuffered — high-Z load only" row.
10. **Startup** — startup time to within X% of final value, and an explicit
    statement that a startup circuit guarantees escape from the zero-current
    state at all corners.
11. **Long-term drift** (if the product cares) — ppm/√khr class statement,
    or an explicit "not specified" row.
12. **Area** — ceiling in mm² or µm².
13. **Corner binding per line** — see below.

## Typical published / achievable ranges (130–180 nm class)

| Parameter | Comfortable | Aggressive (near best practice) | Not credible without extraordinary justification |
|---|---|---|---|
| Untrimmed accuracy (3σ) | ±2–3 % | ±1 % | better than ±0.5 % untrimmed |
| Trimmed accuracy (3σ) | ±0.5–1 % (1-pt trim) | ±0.15 % (1-pt trim) | ±0.05 % class |
| TC (box), 1st-order | 20–100 ppm/°C | 10–20 ppm/°C | — |
| TC (box), curvature-corrected | 5–20 ppm/°C | 3–5 ppm/°C | ≪3 ppm/°C untrimmed |
| PSRR at DC | 40–60 dB | 60–80 dB (with pre-reg/cascoding) | — |
| Quiescent current | 1–50 µA | 100 nA–1 µA (subthreshold) | ≪100 nA with BJT core and fast startup |
| Supply minimum | ≥1.4 V (voltage-mode, VBE+headroom) | 0.85–1.0 V (current-mode, Banba-style) | sub-0.8 V with parasitic-PNP core |

Anchors (open literature, cited for restated facts only):

- **Sub-1 V operation**: H. Banba et al., "A CMOS bandgap reference circuit
  with sub-1-V operation," *IEEE JSSC*, vol. 34, no. 5, pp. 670–674, May
  1999 (doi:10.1109/4.760378). The current-domain-summing topology that
  makes Vref ≈ 0.5 V from a sub-1 V supply possible; a spec demanding
  Vref ≈ 1.2 V from a 1.0 V supply is self-contradictory. See also KB entry
  `kb/entries/sky130-bandgap-reference.json`.
- **Trimmed accuracy floor**: G. Ge, C. Zhang, G. Hoogzaad, K. A. A.
  Makinwa, "A single-trim CMOS bandgap reference with a 3σ inaccuracy of
  ±0.15 % from −40 °C to 125 °C," *IEEE JSSC*, vol. 46, no. 11, pp.
  2693–2701, Nov. 2011. This is best-practice territory *in a 0.16 µm
  process, with a trim, using chopping/DEM* — a draft spec claiming
  comparable accuracy untrimmed is not credible.
- **Why untrimmed accuracy is mismatch-bound**: M. J. M. Pelgrom, A. C. J.
  Duinmaijer, A. P. G. Welbers, "Matching properties of MOS transistors,"
  *IEEE JSSC*, vol. 24, no. 5, pp. 1433–1440, Oct. 1989. Amplifier offset
  is amplified onto Vref by roughly the same gain as ΔVBE (~×5–10); a
  1 mV σ offset alone is ~0.5–1 % of a 1.2 V-class reference at 3σ. Check
  the claimed untrimmed accuracy against the repo's measured Pelgrom
  coefficients — this is the single highest-value evidence check for this
  block class.

## Known spec-writing pitfalls

- **Accuracy row without trim status and σ level.** "±0.5 %" means nothing
  until it says untrimmed/trimmed and 3σ/typical. Untrimmed 3σ ±0.5 % is a
  *not credible* finding (see table); trimmed it is routine.
- **TC row without box method and range.** "10 ppm/°C" at 27–70 °C is a
  different (much easier) claim than over −40 to +125 °C. Require both.
- **PSRR without frequency.** DC PSRR is set by loop gain; MHz-range PSRR
  is set by parasitic coupling and is typically 20–40 dB worse. A single
  unqualified PSRR number always overstates the hard case.
- **Supply-range vs. topology contradiction.** Sub-1 V minimum supply
  requires a current-mode (Banba-class) topology and Vref ≈ 0.5–0.6 V;
  flag any spec pairing sub-1 V supply with a ~1.2 V output.
- **No startup row.** Bandgap cores are bistable (zero-current state is
  stable); a spec without a startup line has not thought about the failure
  that bricks the chip.
- **Noise row missing its bandwidth**, especially when the reference feeds
  an ADC — reference noise adds directly to the converter's noise floor
  (cross-check against the ADC spec's reference-source row).
- **Missing corner binding.** Typical bindings: minimum supply headroom
  binds at **SS/cold** (largest VBE); TC residual and accuracy typically
  bind at the temperature extremes; quiescent current binds at **FF/hot**
  (leakage). Every row should name its corner.
