# Spec-review reference: PLL / clock generation

**Reference date: 2026-07-31.** Ranges below are for a 130–180 nm-class open
PDK; ring-oscillator charge-pump PLLs unless noted (see KB entries
`kb/entries/cmos-ring-vco-current-starved.json`,
`kb/entries/sky130-lc-vco-cross-coupled.json`,
`kb/entries/pfd-charge-pump-tri-state.json`).

## Canonical spec-line checklist

1. **Output frequency range** — min/max, and the guaranteed range *across
   all corners* (the VCO must cover it at SS/hot AND FF/cold with margin).
2. **Reference input** — frequency (range), source type, and required
   reference quality (the PLL cannot clean up in-band reference noise).
3. **Multiplication ratio(s)** — integer range; if fractional-N, the
   frequency resolution and the fractional-spur row it drags in.
4. **Integrated RMS jitter** — ps rms **with the integration band stated**
   (e.g. 10 kHz–40 MHz); this is the headline row.
5. **Period jitter / cycle-to-cycle jitter** — for digital-clocking
   consumers; a different quantity from integrated jitter, not
   interchangeable.
6. **Phase noise at stated offsets** — dBc/Hz at e.g. 100 kHz and 1 MHz,
   at a stated output frequency (only if a consumer needs spectral shape;
   otherwise integrated jitter suffices).
7. **Reference spur** — dBc at the reference frequency offset.
8. **Loop bandwidth** — Hz, with the stability ratio to the reference
   (see Gardner pitfall below).
9. **Lock time** — to a stated criterion (within X ppm of target, or phase
   error < Y), from a stated starting condition.
10. **Power** — mW at a stated output frequency and supply.
11. **Supply sensitivity / pushing** — jitter or frequency vs. VDD ripple
    (ring VCOs are supply-jitter converters; this row forces the
    regulated-supply decision).
12. **Duty cycle** — % at the output divider.
13. **Area**, and **corner binding per line**.

## Typical published / achievable ranges (130–180 nm class)

| Parameter | Comfortable | Aggressive | Not credible without justification |
|---|---|---|---|
| Integrated jitter (ring PLL) | 3–15 ps rms | 1–3 ps rms | sub-ps rms from a ring VCO at mW power |
| Integrated jitter (LC PLL) | 0.5–2 ps rms | 100–500 fs rms | — (LC costs area: on-chip inductor) |
| Jitter–power FoM (ring) | −215 to −225 dB | −225 to −235 dB | better than −240 dB (ring) |
| Jitter–power FoM (LC) | −225 to −235 dB | −235 to −245 dB | beyond ≈−250 dB (any class) |
| Reference spur | −40 to −55 dBc | −60 to −70 dBc | ≲−80 dBc without sampling/SSPD techniques |
| Lock time (integer-N, kHz–MHz loop BW) | 10–100 µs | 1–10 µs | — |

The FoM row is the sharpest achievability test. Compute it from the draft's
own jitter and power rows:

FoM = 10·log10[ (σt / 1 s)² · (P / 1 mW) ]

and compare against the table. A draft whose implied FoM beats the best
published class for its oscillator type is *not credible* regardless of how
each individual row looks.

Anchors:

- **FoM definition and benchmarking**: X. Gao, E. A. M. Klumperink, P. F.
  J. Geraedts, B. Nauta, "Jitter analysis and a benchmarking
  figure-of-merit for phase-locked loops," *IEEE TCAS-II*, vol. 56, no. 2,
  pp. 117–121, Feb. 2009. Defines the σ²·P FoM above and the ~−250 dB
  practical frontier reasoning.
- **Ring vs. LC phase-noise floor**: A. Hajimiri and T. H. Lee, "A general
  theory of phase noise in electrical oscillators," *IEEE JSSC*, vol. 33,
  no. 2, pp. 179–194, Feb. 1998 (and its companion ring-oscillator jitter
  paper, JSSC June 1999). Grounds why ring oscillators sit ~20 dB behind
  LC at equal power — the reason the two rows in the table differ.
- **Charge-pump loop stability**: F. M. Gardner, "Charge-pump phase-lock
  loops," *IEEE Trans. Communications*, vol. 28, no. 11, pp. 1849–1858,
  Nov. 1980. Source of the discrete-time stability limit behind the
  "loop BW ≤ fref/10" rule used in the pitfalls below.
- **Classic integer-N clock-generator baseline**: I. A. Young, J. K.
  Greason, K. L. Wong, "A PLL clock generator with 5 to 110 MHz of lock
  range for microprocessors," *IEEE JSSC*, vol. 27, no. 11, pp. 1599–1607,
  Nov. 1992 — the canonical self-biased-era reference for what a plain
  CMOS clock-multiplier PLL owes its consumers.

## Known spec-writing pitfalls

- **Jitter row without an integration band** — integrated jitter over
  1 kHz–10 MHz vs. 10 kHz–40 MHz differ materially; and integrated RMS,
  period, and cycle-to-cycle jitter are three different quantities. Each
  consumer-facing jitter claim must name its kind and band.
- **VCO range without corner margin.** The tuning range row must show
  coverage of the required output range at *both* SS/hot and FF/cold with
  explicit margin (≥ ±20–30 % design margin is conventional for ring VCOs,
  whose free-running frequency spreads strongly with process).
- **Loop bandwidth vs. reference frequency unstated.** Continuous-time
  loop design silently assumes loop BW ≪ fref (rule of thumb ≤ fref/10,
  from Gardner's discrete-time analysis); a draft pairing a low reference
  with a wide loop BW is internally inconsistent.
- **No reference-spur row** on a PLL feeding an ADC/serializer — spurs
  alias into the consumer's band; "jitter only" specs hide them.
- **Lock time without a criterion** — "locks in 10 µs" is unverifiable
  until "to within ±50 ppm" (or a phase criterion) is attached.
- **No supply-sensitivity row for a ring PLL.** Ring VCO gain from VDD is
  first-order; without this row the (unwritten) requirement lands on an
  unspecified LDO. Cross-check against the LDO spec's PSRR-vs-frequency
  row at the loop bandwidth.
- **Corner binding.** Typical bindings: VCO range coverage binds at
  **SS/hot** (slow) and **FF/cold** (fast) simultaneously — both must be
  named; jitter typically binds at **SS/hot** (lowest oscillation
  amplitude/gain); power at **FF/cold**; lock time at whichever corner
  minimizes loop gain. Every row names its corner.
